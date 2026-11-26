"""Unit tests for the per-turn citation registry and answer post-processing (Phase 4 B3).

Covers C1: grouped square-bracket refs (``[P1, P2]``), parenthesized refs (``(P2)``,
``(P1, R3)``) are accepted and normalized to adjacent ``[P1][P2]``, while a bare ``P1``
token is *not* a citation.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.agent.citations import (
    CitationRegistry,
    compute_data_as_of,
    process_citations,
)
from app.db.models import Product, Review

BASE_TIME = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def make_product(
    *,
    title: str = "Sản phẩm A",
    crawled_at: datetime | None = BASE_TIME,
    price: float | None = 100_000,
    **overrides: object,
) -> Product:
    product = Product(
        id=overrides.pop("id", uuid.uuid4()),
        platform="tiki",
        external_id=overrides.pop("external_id", f"ext-{uuid.uuid4().hex[:6]}"),
        title=title,
        url=f"https://tiki.vn/{title.replace(' ', '-')}.html",
        currency="VND",
        price=price,
        list_price=None,
        rating_avg=4.5,
        sold_count=10,
        crawled_at=crawled_at,
        raw={},
        **overrides,
    )
    return product


def make_review(product: Product, *, content: str = "Rất tốt") -> Review:
    return Review(
        id=uuid.uuid4(),
        product_id=product.id,
        external_id=f"rev-{uuid.uuid4().hex[:6]}",
        rating=5,
        content=content,
        images=[],
        raw={},
    )


def build_registry() -> tuple[CitationRegistry, Product, Product, Review]:
    registry = CitationRegistry()
    first = make_product(title="Sản phẩm A")
    second = make_product(title="Sản phẩm B")
    review = make_review(first)
    registry.register_product(first)
    registry.register_product(second)
    registry.register_review(review, first)
    # Two more reviews so grouped markers such as ``[P1, R3]`` reference real entries.
    registry.register_review(make_review(first, content="Khá ổn"), first)
    registry.register_review(make_review(first, content="Tạm được"), first)
    return registry, first, second, review


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_assigns_stable_refs_per_type_and_dedupes() -> None:
    registry, first, second, review = build_registry()

    assert registry.get_product_ref(first.id) == "P1"
    assert registry.get_product_ref(second.id) == "P2"
    # Registering the same product again must not consume a new ref.
    assert registry.register_product(first) == "P1"
    assert registry.resolve_product_ref("p1") == first.id
    assert registry.resolve_product_ref("R1") is None

    review_entry = registry.get("R1")
    assert review_entry is not None
    assert review_entry.review_id == review.id
    assert review_entry.product_id == first.id
    # A review always makes its product citable.
    assert review_entry.title == first.title


def test_registry_doc_refs_are_stable_and_typed() -> None:
    registry = CitationRegistry()
    product = make_product()
    chunk_id = uuid.uuid4()

    assert registry.register_doc(chunk_id, product, snippet="Mô tả") == "D1"
    assert registry.register_doc(chunk_id, product, snippet="Mô tả khác") == "D1"
    entry = registry.get("D1")
    assert entry is not None
    assert entry.type == "doc"
    assert entry.chunk_id == chunk_id
    assert entry.snippet == "Mô tả"  # first registration wins


def test_registry_product_snippet_summarizes_price_rating_and_sales() -> None:
    registry = CitationRegistry()
    entry = registry.get(registry.register_product(make_product()))

    assert entry is not None
    assert "Giá" in entry.snippet
    assert "4.5/5" in entry.snippet
    assert "Đã bán: 10" in entry.snippet


# ---------------------------------------------------------------------------
# C1 — accepted marker shapes and normalization
# ---------------------------------------------------------------------------


def test_grouped_square_brackets_are_normalized_to_adjacent_markers() -> None:
    registry, _first, _second, _review = build_registry()

    result = process_citations("Cả hai đều tốt [P1, P2].", registry, [])

    assert result.content == "Cả hai đều tốt [P1][P2]."
    assert [c["ref"] for c in result.citations] == ["P1", "P2"]
    assert result.invalid_citations == 0


@pytest.mark.parametrize(
    ("raw", "normalized", "refs"),
    [
        ("Giá tốt (P2).", "Giá tốt [P2].", ["P2"]),
        ("Ổn (P1, R3).", "Ổn [P1][R3].", ["P1", "R3"]),
        ("Bán chạy [P1 P2]", "Bán chạy [P1][P2]", ["P1", "P2"]),
        ("Đã dùng [ P1 ; P2 ]", "Đã dùng [P1][P2]", ["P1", "P2"]),
    ],
)
def test_grouped_and_parenthesized_markers_are_accepted(
    raw: str, normalized: str, refs: list[str]
) -> None:
    registry, _first, _second, _review = build_registry()

    result = process_citations(raw, registry, [])

    assert result.content == normalized
    assert [c["ref"] for c in result.citations] == refs
    assert result.invalid_citations == 0


@pytest.mark.parametrize(
    "raw",
    [
        "Sản phẩm P1 rất tốt.",
        "Xem mã P1 để biết thêm.",
        "(mã: P1) là sản phẩm đầu tiên.",
        "(P1 là sản phẩm đầu tiên)",
        "ref P2 không phải trích dẫn",
    ],
)
def test_bare_or_prose_embedded_tokens_are_not_citations(raw: str) -> None:
    registry, _first, _second, _review = build_registry()

    result = process_citations(raw, registry, [])

    assert result.content == raw, "Non-citation text must be left untouched"
    assert result.citations == []
    assert result.invalid_citations == 0


def test_unknown_markers_are_stripped_counted_and_not_cited() -> None:
    registry, _first, _second, _review = build_registry()

    result = process_citations("Một [P1] hai [P9] ba [R7, P2].", registry, [])

    assert result.content == "Một [P1] hai ba [P2]."
    assert [c["ref"] for c in result.citations] == ["P1", "P2"]
    assert result.invalid_citations == 2
    assert result.invalid_refs == ["P9", "R7"]


def test_citations_are_ordered_by_first_appearance_and_deduped() -> None:
    registry, _first, _second, _review = build_registry()

    result = process_citations("[P2] ... [P1] ... [P2, R3]", registry, [])

    assert [c["ref"] for c in result.citations] == ["P2", "P1", "R3"]


def test_citation_payload_exposes_ids_and_snippet() -> None:
    registry, first, _second, review = build_registry()

    result = process_citations("[R1]", registry, [])
    payload = result.citations[0]

    assert payload["type"] == "review"
    assert payload["review_id"] == str(review.id)
    assert payload["product_id"] == str(first.id)
    assert payload["url"].startswith("https://tiki.vn/")
    assert "chunk_id" not in payload


# ---------------------------------------------------------------------------
# data_as_of
# ---------------------------------------------------------------------------


def test_data_as_of_uses_earliest_cited_product() -> None:
    older = make_product(title="Cũ", crawled_at=BASE_TIME - timedelta(days=5))
    newer = make_product(title="Mới", crawled_at=BASE_TIME)
    registry = CitationRegistry()
    registry.register_product(older)
    registry.register_product(newer)

    result = process_citations("Chỉ nhắc [P2].", registry, [older, newer])

    assert result.data_as_of == newer.crawled_at


def test_data_as_of_falls_back_to_scope_when_nothing_is_cited() -> None:
    older = make_product(title="Cũ", crawled_at=BASE_TIME - timedelta(days=5))
    newer = make_product(title="Mới", crawled_at=BASE_TIME)
    registry = CitationRegistry()

    result = process_citations("Không trích dẫn gì.", registry, [newer, older])

    assert result.data_as_of == older.crawled_at


def test_data_as_of_is_none_without_crawled_timestamps() -> None:
    assert compute_data_as_of(set(), [make_product(crawled_at=None)]) is None
    assert compute_data_as_of({uuid.uuid4()}, []) is None
