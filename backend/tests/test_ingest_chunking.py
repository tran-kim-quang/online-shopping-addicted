"""Unit tests for ``app.ingest.chunking``: splitting, prefixes, hashing, resync."""

from __future__ import annotations

import hashlib
import uuid

import pytest

from app.db.models import Chunk
from app.ingest.chunking import (
    ChunkDraft,
    build_product_chunks,
    chunk_description,
    chunk_meta,
    chunk_reviews,
    chunk_specs,
    compute_content_hash,
    sync_product_chunks,
)
from app.ingest.embedding import NullEmbedder
from tests.ingest_fakes import (
    ChunkSession,
    FakeEmbedder,
    WrongLengthEmbedder,
    make_product_row,
    make_review_row,
    make_shop_row,
)

TITLE = "Gạo ST25"


def _paragraph(seed: str, length: int = 150) -> str:
    base = f"{seed} " * (length // (len(seed) + 1) + 2)
    return base[:length].strip()


# ---------------------------------------------------------------------------
# Description chunking
# ---------------------------------------------------------------------------


def test_description_chunk_is_prefixed_with_the_product_title() -> None:
    drafts = chunk_description(TITLE, "Mô tả ngắn về sản phẩm.")

    assert len(drafts) == 1
    assert drafts[0].source_type == "description"
    assert drafts[0].text == f"[{TITLE}] Mô tả ngắn về sản phẩm."


def test_blank_description_yields_no_chunks() -> None:
    assert chunk_description(TITLE, None) == []
    assert chunk_description(TITLE, "   \n\n ") == []


def test_description_is_packed_up_to_max_chars() -> None:
    paragraphs = [_paragraph(f"doan{i}") for i in range(6)]
    drafts = chunk_description(
        TITLE,
        "\n\n".join(paragraphs),
        max_chars=400,
        overlap_chars=100,
    )

    assert len(drafts) >= 2
    assert all(len(draft.text) <= 400 for draft in drafts)
    assert all(draft.text.startswith(f"[{TITLE}] ") for draft in drafts)
    # Every paragraph is preserved somewhere in the chunk stream.
    joined = "\n".join(draft.text for draft in drafts)
    for paragraph in paragraphs:
        assert paragraph in joined


def _has_suffix_prefix_overlap(previous: str, following: str, *, minimum: int = 20) -> bool:
    """True when ``following`` starts with a suffix of ``previous`` (overlap carry)."""
    limit = min(len(previous), len(following))
    for size in range(limit, minimum - 1, -1):
        if previous[-size:] == following[:size]:
            return True
    return False


def test_consecutive_description_chunks_overlap() -> None:
    paragraphs = [_paragraph(f"doan{i}") for i in range(6)]
    drafts = chunk_description(
        TITLE,
        "\n\n".join(paragraphs),
        max_chars=400,
        overlap_chars=150,
    )

    assert len(drafts) >= 3
    bodies = [draft.text[len(f"[{TITLE}] ") :] for draft in drafts]
    for previous, following in zip(bodies, bodies[1:], strict=False):
        assert _has_suffix_prefix_overlap(previous, following), (
            f"no overlap carry between:\n{previous!r}\nand\n{following!r}"
        )


def test_oversized_paragraph_is_split_without_breaking_the_limit() -> None:
    long_line = " ".join(f"tu{i}" for i in range(400))
    drafts = chunk_description(TITLE, long_line, max_chars=300, overlap_chars=50)

    assert len(drafts) > 1
    assert all(len(draft.text) <= 300 for draft in drafts)
    bodies = [draft.text[len(f"[{TITLE}] ") :] for draft in drafts]
    for previous, following in zip(bodies, bodies[1:], strict=False):
        assert _has_suffix_prefix_overlap(previous, following, minimum=10)


def test_description_chunk_refs_are_stable_indices() -> None:
    drafts = chunk_description(
        TITLE,
        "\n\n".join(_paragraph(f"p{i}") for i in range(8)),
        max_chars=400,
        overlap_chars=100,
    )

    assert [draft.source_ref for draft in drafts] == [str(idx) for idx in range(len(drafts))]


# ---------------------------------------------------------------------------
# Specs / meta / reviews
# ---------------------------------------------------------------------------


def test_specs_chunk_is_one_key_value_line_per_spec() -> None:
    product = make_product_row(specs={"Xuất xứ": "Việt Nam", "Trọng lượng": "5kg"})

    draft = chunk_specs(product)

    assert draft is not None
    assert draft.source_type == "specs"
    assert draft.text == "Xuất xứ: Việt Nam\nTrọng lượng: 5kg"
    assert draft.source_ref == "specs"


def test_specs_chunk_skips_empty_values_and_missing_specs() -> None:
    assert chunk_specs(make_product_row(specs={})) is None
    assert chunk_specs(make_product_row(specs={"Xuất xứ": "", "Hạn dùng": None})) is None
    assert chunk_specs(make_product_row(specs={"Xuất xứ": "  Việt Nam  "})) is not None


def test_meta_chunk_summarizes_the_product() -> None:
    shop = make_shop_row(name="Tiki Trading")
    product = make_product_row(
        title="Gạo ST25 túi 5kg",
        brand="Soc Trang",
        category="Thực phẩm",
        price=125000,
        rating_avg=4.6,
        rating_count=12,
        sold_count=50,
        shop_id=shop.id,
    )

    draft = chunk_meta(product, shop=shop)

    assert draft.source_type == "meta"
    assert draft.source_ref == "meta"
    for needle in (
        "Sản phẩm: Gạo ST25 túi 5kg",
        "Thương hiệu: Soc Trang",
        "Danh mục: Thực phẩm",
        "Giá: 125,000 VND",
        "Đánh giá: 4.60/5 (12 lượt đánh giá)",
        "Đã bán: 50",
        "Cửa hàng: Tiki Trading",
    ):
        assert needle in draft.text


def test_meta_chunk_falls_back_to_sold_text_and_omits_missing_fields() -> None:
    product = make_product_row(
        sold_count=None,
        sold_text="Đã bán 1,2k",
        brand=None,
        category=None,
        price=None,
        rating_avg=None,
    )

    text = chunk_meta(product).text

    assert "Đã bán: Đã bán 1,2k" in text
    assert "Thương hiệu" not in text
    assert "Danh mục" not in text
    assert "Giá" not in text
    assert "Đánh giá" not in text


def test_review_chunks_are_one_per_review_with_rating_prefix() -> None:
    reviews = [
        make_review_row("r1", rating=5, title="Hài lòng", content="Giao hàng rất nhanh"),
        make_review_row("r2", rating=2, title="Tệ", content="Hàng bị móp hộp"),
    ]

    drafts = chunk_reviews(reviews)

    assert [draft.source_ref for draft in drafts] == ["r1", "r2"]
    assert drafts[0].text == "Đánh giá 5★: Hài lòng. Giao hàng rất nhanh"
    assert drafts[1].text.startswith("Đánh giá 2★: ")


def test_review_chunks_skip_short_and_empty_reviews() -> None:
    reviews = [
        make_review_row("short", title=None, content="Tốt"),
        make_review_row("empty", title=None, content=None),
        make_review_row("blank", title="   ", content="   "),
        make_review_row("ok", title=None, content="Sản phẩm rất đáng tiền"),
    ]

    drafts = chunk_reviews(reviews)

    assert [draft.source_ref for draft in drafts] == ["ok"]


def test_review_chunk_reuses_title_sentence_separator() -> None:
    drafts = chunk_reviews([make_review_row("r1", title="Rất tốt!", content="Sẽ mua lại")])

    assert drafts[0].text == "Đánh giá 5★: Rất tốt! Sẽ mua lại"


# ---------------------------------------------------------------------------
# Assembly + hashing
# ---------------------------------------------------------------------------


def test_build_product_chunks_orders_meta_specs_description_reviews() -> None:
    product = make_product_row(
        description_text="Mô tả sản phẩm đủ dài để tạo chunk.",
        specs={"Xuất xứ": "Việt Nam"},
    )
    reviews = [make_review_row("r1", content="Giao hàng nhanh và đóng gói kỹ")]

    drafts = build_product_chunks(product, reviews)

    assert [draft.source_type for draft in drafts] == ["meta", "specs", "description", "review"]


def test_build_product_chunks_deduplicates_identical_drafts() -> None:
    product = make_product_row(description_text=None, specs={})
    duplicate = make_review_row("r1", content="Nội dung giống hệt nhau")
    other = make_review_row("r2", content="Nội dung giống hệt nhau")

    drafts = build_product_chunks(product, [duplicate, other])

    keys = [(d.source_type, d.source_ref, d.content_hash) for d in drafts]
    assert len(keys) == len(set(keys))
    assert len(drafts) == 3  # meta + 2 reviews (different source_ref)


@pytest.mark.parametrize("text", ["", "Gạo ST25", "Đánh giá 5★: rất tốt"])
def test_content_hash_is_a_stable_sha256(text: str) -> None:
    assert compute_content_hash(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert len(compute_content_hash(text)) == 64
    assert compute_content_hash(text) == compute_content_hash(text)


# ---------------------------------------------------------------------------
# sync_product_chunks — hash-based, idempotent reconciliation
# ---------------------------------------------------------------------------


def _draft(source_type: str, source_ref: str, text: str) -> ChunkDraft:
    return ChunkDraft(
        source_type=source_type,  # type: ignore[arg-type]
        source_ref=source_ref,
        text=text,
        content_hash=compute_content_hash(text),
    )


def _existing_chunk(
    product_id: uuid.UUID,
    draft: ChunkDraft,
    *,
    shop_id: uuid.UUID | None = None,
    embedding: list[float] | None = None,
    embedding_model: str | None = None,
) -> Chunk:
    return Chunk(
        id=uuid.uuid4(),
        product_id=product_id,
        shop_id=shop_id,
        source_type=draft.source_type,
        source_ref=draft.source_ref,
        text=draft.text,
        content_hash=draft.content_hash,
        embedding=embedding,
        embedding_model=embedding_model,
        dim=len(embedding) if embedding else None,
    )


async def test_sync_creates_and_embeds_new_chunks() -> None:
    product = make_product_row()
    embedder = FakeEmbedder()
    session = ChunkSession()
    drafts = [_draft("meta", "meta", "meta text"), _draft("review", "r1", "review text")]

    stats = await sync_product_chunks(session, product, drafts, embedder=embedder)

    assert (stats.total, stats.created, stats.kept, stats.deleted, stats.embedded) == (
        2,
        2,
        0,
        0,
        2,
    )
    assert len(session.added) == 2
    assert all(chunk.embedding_model == embedder.name for chunk in session.added)
    assert all(chunk.dim == embedder.dim for chunk in session.added)
    assert embedder.embedded_texts == ["meta text", "review text"]
    assert session.flushes == 1


async def test_sync_is_idempotent_and_does_not_re_embed_unchanged_chunks() -> None:
    product = make_product_row()
    embedder = FakeEmbedder()
    drafts = [_draft("meta", "meta", "meta text"), _draft("review", "r1", "review text")]
    stored = [
        _existing_chunk(
            product.id,
            draft,
            embedding=embedder.vector(draft.text),
            embedding_model=embedder.name,
        )
        for draft in drafts
    ]
    session = ChunkSession(stored)

    stats = await sync_product_chunks(session, product, drafts, embedder=embedder)

    assert (stats.created, stats.kept, stats.deleted, stats.embedded) == (0, 2, 0, 0)
    assert session.added == []
    assert session.deleted == []
    assert embedder.calls == []


async def test_sync_replaces_changed_chunks_and_drops_obsolete_ones() -> None:
    product = make_product_row()
    embedder = FakeEmbedder()
    old_review = _draft("review", "r1", "review text cũ")
    stale_review = _draft("review", "r2", "review đã bị xoá")
    stored = [
        _existing_chunk(
            product.id,
            old_review,
            embedding=embedder.vector(old_review.text),
            embedding_model=embedder.name,
        ),
        _existing_chunk(
            product.id,
            stale_review,
            embedding=embedder.vector(stale_review.text),
            embedding_model=embedder.name,
        ),
    ]
    new_review = _draft("review", "r1", "review text mới dài hơn")
    session = ChunkSession(stored)

    stats = await sync_product_chunks(
        session,
        product,
        [new_review],
        embedder=embedder,
    )

    assert (stats.created, stats.kept, stats.deleted, stats.embedded) == (1, 0, 2, 1)
    assert [chunk.source_ref for chunk in session.deleted] == ["r1", "r2"]
    assert embedder.embedded_texts == ["review text mới dài hơn"]


async def test_sync_re_embeds_when_the_embedding_model_changed() -> None:
    product = make_product_row()
    embedder = FakeEmbedder(name="new-model")
    draft = _draft("meta", "meta", "meta text")
    stored = [_existing_chunk(product.id, draft, embedding=[0.1, 0.2], embedding_model="old-model")]
    session = ChunkSession(stored)

    stats = await sync_product_chunks(session, product, [draft], embedder=embedder)

    assert (stats.kept, stats.embedded) == (1, 1)
    assert stored[0].embedding_model == "new-model"
    assert stored[0].dim == embedder.dim


async def test_sync_updates_shop_id_on_kept_chunks() -> None:
    product = make_product_row()
    embedder = FakeEmbedder()
    draft = _draft("meta", "meta", "meta text")
    stored = [
        _existing_chunk(
            product.id,
            draft,
            embedding=embedder.vector(draft.text),
            embedding_model=embedder.name,
        )
    ]
    product.shop_id = uuid.uuid4()
    session = ChunkSession(stored)

    await sync_product_chunks(session, product, [draft], embedder=embedder)

    assert stored[0].shop_id == product.shop_id


async def test_sync_without_embedder_stores_text_only() -> None:
    product = make_product_row()
    session = ChunkSession()

    stats = await sync_product_chunks(
        session,
        product,
        [_draft("meta", "meta", "meta text")],
        embedder=NullEmbedder(),
    )

    assert stats.embedded == 0
    assert session.added[0].embedding is None
    assert session.added[0].embedding_model is None


async def test_sync_raises_when_embedder_returns_too_few_vectors() -> None:
    product = make_product_row()
    session = ChunkSession()
    drafts = [_draft("meta", "meta", "a text"), _draft("review", "r1", "b text")]

    with pytest.raises(RuntimeError, match="returned 1 vectors for 2 chunks"):
        await sync_product_chunks(
            session,
            product,
            drafts,
            embedder=WrongLengthEmbedder(),
        )


async def test_sync_with_no_drafts_deletes_everything() -> None:
    product = make_product_row()
    draft = _draft("meta", "meta", "meta text")
    session = ChunkSession([_existing_chunk(product.id, draft)])

    stats = await sync_product_chunks(session, product, [], embedder=FakeEmbedder())

    assert (stats.total, stats.created, stats.deleted) == (0, 0, 1)
    assert session.deleted
