"""Phase 3 integration tests: dedicated Postgres DB + dedicated Redis DB.

The project database (``shopqa``) holds real crawled data and the compose stack
runs against it, so these tests create and use ``shopqa_phase3_test`` (never
truncating ``shopqa``) and Redis database 15 (never touching the worker's keys
in database 0). Both skip automatically when the services are unreachable.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
import redis.asyncio as aioredis
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.ingest.pipeline as pipeline_module
from app.core.config import Settings
from app.crawl.adapters.base import AdapterRegistry
from app.crawl.repository import (
    create_source,
    update_source,
)
from app.crawl.service import crawl_url
from app.db.models import Chunk, LLMProfile, Product, Review, Shop, Source
from app.ingest.chunking import compute_content_hash
from app.ingest.embedding import NullEmbedder
from app.ingest.pipeline import ingest_source, resolve_source_scope
from app.ingest.search import hybrid_search
from app.ingest.sentiment import analyze_product_reviews
from app.jobs.events import JobEvents
from app.jobs.queue import JobQueue
from app.jobs.tasks import run_ingest
from tests.crawl_fakes import FakeAdapter, make_product, make_review, make_shop
from tests.ingest_fakes import (
    FakeEmbedder,
    RecordingJobEvents,
    RecordingJobQueue,
    build_ingest_app,
)

pytestmark = pytest.mark.integration

BACKEND_DIR = Path(__file__).resolve().parents[1]
SECRET = "vN5y6rM0J9h2xL8qK3pW1zB4cF7dG0tH2sV5mN8wX4Q="

PG_HOST = os.environ.get("TEST_PHASE3_PG_HOST", "localhost")
# docker-compose maps the pgvector container to host port 5433 (Redis → 6380).
PG_PORT = int(os.environ.get("TEST_PHASE3_PG_PORT", "5433"))
PG_USER = os.environ.get("TEST_PHASE3_PG_USER", "shopqa")
PG_PASSWORD = os.environ.get("TEST_PHASE3_PG_PASSWORD", "shopqa")
PG_MAINTENANCE_DB = os.environ.get("TEST_PHASE3_PG_MAINTENANCE_DB", "shopqa")
TEST_DB_NAME = os.environ.get("TEST_PHASE3_DB_NAME", "shopqa_phase3_test")
TEST_REDIS_DB = int(os.environ.get("TEST_PHASE3_REDIS_DB", "15"))

PROJECT_DB_NAME = "shopqa"  # never touched: holds real crawl data

MAINTENANCE_DSN = f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{PG_MAINTENANCE_DB}"
TEST_DATABASE_URL = (
    f"postgresql+asyncpg://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{TEST_DB_NAME}"
)
TEST_REDIS_URL = f"redis://{PG_HOST}:6380/{TEST_REDIS_DB}"

DIM = 1024  # default embedding dimension (BAAI/bge-m3) → exercises the HNSW index


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "SECRET_KEY": SECRET,
        "DATABASE_URL": TEST_DATABASE_URL,
        "REDIS_URL": TEST_REDIS_URL,
        "EMBEDDING_BACKEND": "none",
        "CRAWL_CACHE_TTL_HOURS": 48,
    }
    values.update(overrides)
    return Settings(**values)


# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------


def _skip_if_postgres_unreachable() -> None:
    async def _ping() -> None:
        conn = await asyncio.wait_for(asyncpg.connect(MAINTENANCE_DSN), timeout=5)
        await conn.close()

    try:
        asyncio.run(_ping())
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable at {MAINTENANCE_DSN}: {exc}")


async def _ensure_test_database() -> None:
    conn = await asyncpg.connect(MAINTENANCE_DSN)
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", TEST_DB_NAME)
        if not exists:
            await conn.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def phase3_database_url() -> str:
    """Create the isolated database if needed and migrate it to head."""
    _skip_if_postgres_unreachable()
    assert TEST_DB_NAME != PROJECT_DB_NAME, "integration tests must not run against the project DB"

    asyncio.run(_ensure_test_database())

    env = {**os.environ, "DATABASE_URL": TEST_DATABASE_URL}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"alembic failed:\n{result.stdout}\n{result.stderr}"
    return TEST_DATABASE_URL


@pytest.fixture
async def session_factory(phase3_database_url: str) -> AsyncIterator[Any]:
    """Function-scoped engine with clean crawl/ingest tables in the test database."""
    engine = create_async_engine(phase3_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            current = (await conn.execute(text("SELECT current_database()"))).scalar_one()
            assert current == TEST_DB_NAME
            await conn.execute(text("TRUNCATE chunks, reviews, products, shops, sources CASCADE"))
        yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
async def session(session_factory: Any) -> AsyncIterator[AsyncSession]:
    async with session_factory() as active_session:
        yield active_session


@pytest.fixture
async def redis_client() -> AsyncIterator[Any]:
    """Real Redis, but on a dedicated database index that is flushed per test."""
    client = aioredis.from_url(TEST_REDIS_URL, decode_responses=True)
    try:
        await client.ping()
    except Exception as exc:  # pragma: no cover - environment dependent
        await client.aclose()
        pytest.skip(f"Redis not reachable at {TEST_REDIS_URL}: {exc}")

    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


class MappedEmbedder(FakeEmbedder):
    """FakeEmbedder with explicit query→vector mappings (for deterministic cosine)."""

    def __init__(self, mapping: dict[str, list[float]] | None = None) -> None:
        super().__init__(dim=DIM)
        self.mapping = mapping or {}

    def vector(self, text: str) -> list[float]:
        if text in self.mapping:
            return self.mapping[text]
        return super().vector(text)

    @staticmethod
    def basis(index: int) -> list[float]:
        vec = [0.0] * DIM
        vec[index] = 1.0
        return vec


async def seed_chunk(
    session: AsyncSession,
    *,
    product_id: uuid.UUID,
    shop_id: uuid.UUID | None,
    source_type: str,
    source_ref: str,
    text: str,
    embedding: list[float] | None = None,
    embedding_model: str = "fake-embedder",
) -> Chunk:
    chunk = Chunk(
        id=uuid.uuid4(),
        product_id=product_id,
        shop_id=shop_id,
        source_type=source_type,
        source_ref=source_ref,
        text=text,
        content_hash=compute_content_hash(text),
        embedding=embedding,
        embedding_model=embedding_model if embedding is not None else None,
        dim=len(embedding) if embedding is not None else None,
    )
    session.add(chunk)
    return chunk


async def seed_catalog(session: AsyncSession) -> dict[str, Any]:
    """Insert 2 shops / 3 products / 3 chunks used by the search tests."""
    shop_a = Shop(
        id=uuid.uuid4(),
        platform="tiki",
        external_id="seller-a",
        name="Shop A",
        url="https://tiki.vn/cua-hang/shop-a",
        raw={},
    )
    shop_b = Shop(
        id=uuid.uuid4(),
        platform="tiki",
        external_id="seller-b",
        name="Shop B",
        url="https://tiki.vn/cua-hang/shop-b",
        raw={},
    )
    session.add_all([shop_a, shop_b])

    product_1 = Product(
        id=uuid.uuid4(),
        shop_id=shop_a.id,
        platform="tiki",
        external_id="1001",
        title="Gạo ST25 túi 5kg",
        url="https://tiki.vn/gao-st25-p1001.html",
        images=[],
        specs={"Xuất xứ": "Sóc Trăng"},
        raw={},
    )
    product_2 = Product(
        id=uuid.uuid4(),
        shop_id=shop_b.id,
        platform="tiki",
        external_id="1002",
        title="Nồi cơm điện",
        url="https://tiki.vn/noi-com-dien-p1002.html",
        images=[],
        specs={},
        raw={},
    )
    session.add_all([product_1, product_2])

    embedder = MappedEmbedder()
    review_chunk = await seed_chunk(
        session,
        product_id=product_1.id,
        shop_id=shop_a.id,
        source_type="review",
        source_ref="r1",
        text="Sản phẩm rất đáng tiền",
        embedding=embedder.basis(0),
    )
    description_chunk = await seed_chunk(
        session,
        product_id=product_1.id,
        shop_id=shop_a.id,
        source_type="description",
        source_ref="0",
        text="Giao hàng rất nhanh và đóng gói cẩn thận",
        embedding=embedder.basis(1),
    )
    meta_chunk = await seed_chunk(
        session,
        product_id=product_2.id,
        shop_id=shop_b.id,
        source_type="meta",
        source_ref="meta",
        text="Gạo thơm dẻo, chất lượng cao",
        embedding=embedder.basis(0),
    )
    await session.commit()

    return {
        "shop_a": shop_a,
        "shop_b": shop_b,
        "product_1": product_1,
        "product_2": product_2,
        "review_chunk": review_chunk,
        "description_chunk": description_chunk,
        "meta_chunk": meta_chunk,
        "embedder": embedder,
    }


# ---------------------------------------------------------------------------
# Migration head
# ---------------------------------------------------------------------------


async def test_migration_head_is_0004(session: AsyncSession) -> None:
    revision = (await session.execute(text("SELECT version_num FROM alembic_version"))).scalar()

    # Phase 4 (A1) adds ``0004_chat``; the ingest tables from 0003 are asserted below.
    assert revision == "0004_chat"


async def test_migration_0003_creates_unaccent_and_immutable_wrapper(session: AsyncSession) -> None:
    extensions = {row[0] for row in await session.execute(text("SELECT extname FROM pg_extension"))}
    volatility = (
        await session.execute(
            text(
                "SELECT p.provolatile::text FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE p.proname = 'f_unaccent' AND n.nspname = 'public'"
            )
        )
    ).scalar()
    accentless = (
        await session.execute(text("SELECT f_unaccent('Giao hàng nhanh, đóng gói kỹ')"))
    ).scalar()

    assert "unaccent" in extensions
    assert volatility == "i", "f_unaccent must be IMMUTABLE to be usable in generated columns"
    assert accentless == "Giao hang nhanh, dong goi ky"


async def test_migration_0003_generated_tsv_column_matches_f_unaccent(
    session: AsyncSession,
) -> None:
    catalog = await seed_catalog(session)

    row = (
        await session.execute(
            text(
                "SELECT tsv::text AS tsv, "
                "to_tsvector('simple', f_unaccent(text))::text AS expected "
                "FROM chunks WHERE id = :chunk_id"
            ),
            {"chunk_id": catalog["description_chunk"].id},
        )
    ).one()

    assert "'giao':1" in row.tsv
    assert row.tsv == row.expected


async def test_migration_0003_creates_partial_hnsw_index_and_gin_index(
    session: AsyncSession,
) -> None:
    indexes = {
        row[0]: row[1]
        for row in await session.execute(
            text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'chunks'")
        )
    }

    hnsw = indexes["ix_chunks_embedding_hnsw_1024"]
    assert "USING hnsw" in hnsw
    assert "vector_cosine_ops" in hnsw
    assert "WHERE (dim = 1024)" in hnsw
    assert "USING gin (tsv)" in indexes["ix_chunks_tsv"]


async def test_migration_0003_adds_sentiment_and_counter_columns(session: AsyncSession) -> None:
    columns: dict[str, set[str]] = {}
    for table in ("reviews", "products", "chunks"):
        columns[table] = {
            row[0]
            for row in await session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = :table"
                ),
                {"table": table},
            )
        }
    constraints = {
        row[0]
        for row in await session.execute(
            text(
                "SELECT conname FROM pg_constraint WHERE conrelid::regclass::text IN "
                "('reviews', 'products', 'chunks')"
            )
        )
    }

    assert {
        "sentiment",
        "sentiment_source",
        "aspects",
        "analyzed_at",
        "content_hash",
    } <= columns["reviews"]
    assert {
        "positive_review_count",
        "negative_review_count",
        "neutral_review_count",
        "ingested_at",
    } <= columns["products"]
    assert {"tsv", "embedding", "embedding_model", "dim", "content_hash"} <= columns["chunks"]
    assert {
        "ck_reviews_sentiment",
        "ck_reviews_sentiment_source",
        "ck_chunks_source_type",
        "uq_chunks_product_source_hash",
    } <= constraints


async def test_chunk_rows_are_deleted_with_their_product(session: AsyncSession) -> None:
    catalog = await seed_catalog(session)

    await session.execute(
        text("DELETE FROM products WHERE id = :pid"),
        {"pid": catalog["product_1"].id},
    )
    await session.commit()

    remaining = (
        await session.execute(
            select(func.count())
            .select_from(Chunk)
            .where(Chunk.product_id == catalog["product_1"].id)
        )
    ).scalar_one()
    assert remaining == 0


# ---------------------------------------------------------------------------
# Hybrid search (FTS + vector + RRF)
# ---------------------------------------------------------------------------


async def test_full_text_search_is_accent_insensitive(session: AsyncSession) -> None:
    catalog = await seed_catalog(session)

    hits = await hybrid_search(session, "giao hang nhanh", embedder=None)

    assert hits
    assert hits[0].chunk_id == catalog["description_chunk"].id
    assert hits[0].fts_rank == 1
    assert hits[0].vector_rank is None
    assert hits[0].text.startswith("Giao hàng rất nhanh")


async def test_full_text_search_accepts_a_diacritics_query_too(session: AsyncSession) -> None:
    catalog = await seed_catalog(session)

    hits = await hybrid_search(session, "giao hàng nhanh", embedder=None)

    assert [hit.chunk_id for hit in hits] == [catalog["description_chunk"].id]


async def test_vector_only_matches_are_returned_without_fts_hits(session: AsyncSession) -> None:
    catalog = await seed_catalog(session)
    embedder = catalog["embedder"]
    embedder.mapping["hương vị tuyệt vời"] = embedder.basis(0)

    hits = await hybrid_search(session, "hương vị tuyệt vời", embedder=embedder, min_similarity=0)

    # Vector search keeps every embedded chunk as a candidate when ``min_similarity=0``
    # (Phase 4 F2 applies the ``SEARCH_MIN_COSINE_SIMILARITY=0.6`` cut-off by default, which
    # would drop the orthogonal description chunk), ordered by cosine distance.
    assert len(hits) == 3
    assert all(hit.fts_rank is None for hit in hits)
    assert all(hit.vector_rank is not None for hit in hits)
    assert {hits[0].chunk_id, hits[1].chunk_id} == {
        catalog["review_chunk"].id,
        catalog["meta_chunk"].id,
    }
    assert hits[2].chunk_id == catalog["description_chunk"].id
    assert {hit.vector_rank for hit in hits[:2]} == {1, 2}


async def test_rrf_fuses_both_rankings_for_the_same_chunk(session: AsyncSession) -> None:
    catalog = await seed_catalog(session)
    embedder = catalog["embedder"]
    embedder.mapping["giao hang nhanh"] = embedder.basis(1)

    hits = await hybrid_search(session, "giao hang nhanh", embedder=embedder)

    fused = hits[0]
    assert fused.chunk_id == catalog["description_chunk"].id
    assert fused.fts_rank == 1
    assert fused.vector_rank == 1
    # 1/(60+1) + 1/(60+1); rounded to 8 decimals by the implementation.
    assert fused.score == pytest.approx(2 / 61, abs=1e-8)


async def test_rrf_ranks_chunks_present_in_both_lists_above_single_list_hits(
    session: AsyncSession,
) -> None:
    catalog = await seed_catalog(session)
    embedder = catalog["embedder"]
    # "gạo" matches the review and meta chunks lexically; the vector branch only
    # ranks the embedded ones, so both appear in both lists.
    embedder.mapping["gạo"] = embedder.basis(0)

    hits = await hybrid_search(session, "gạo", embedder=embedder, min_similarity=0)

    # ``min_similarity=0`` keeps the orthogonal description chunk in the vector branch: the
    # Phase 4 default (0.6) would drop it, leaving only the two chunks that appear in both
    # lists and defeating the point of this RRF ordering test.
    assert len(hits) == 3
    fused = [hit for hit in hits if hit.fts_rank is not None and hit.vector_rank is not None]
    single = [hit for hit in hits if hit.fts_rank is None or hit.vector_rank is None]
    assert [hit.chunk_id for hit in fused] == [catalog["meta_chunk"].id]
    assert {hit.chunk_id for hit in single} == {
        catalog["review_chunk"].id,
        catalog["description_chunk"].id,
    }
    assert hits[0].score == pytest.approx(
        1 / (60 + hits[0].fts_rank) + 1 / (60 + hits[0].vector_rank), abs=1e-8
    )
    assert hits[0].score > max(hit.score for hit in single)
    assert all(hit.fts_rank is None for hit in single)
    assert all(hit.vector_rank is not None for hit in single)


async def test_search_filters_by_product_shop_and_source_type(session: AsyncSession) -> None:
    catalog = await seed_catalog(session)
    product_1 = catalog["product_1"]
    shop_b = catalog["shop_b"]

    by_product = await hybrid_search(session, "nhanh", product_ids=[product_1.id], embedder=None)
    by_shop = await hybrid_search(session, "gạo", shop_id=shop_b.id, embedder=None)
    by_type = await hybrid_search(session, "gạo", source_types=["meta"], embedder=None)
    cross_product = await hybrid_search(session, "gạo", product_ids=[product_1.id], embedder=None)

    assert [hit.chunk_id for hit in by_product] == [catalog["description_chunk"].id]
    assert [hit.chunk_id for hit in by_shop] == [catalog["meta_chunk"].id]
    assert [hit.chunk_id for hit in by_type] == [catalog["meta_chunk"].id]
    assert all(hit.source_type == "meta" for hit in by_type)
    # "gạo" only matches product 2's meta chunk, so scoping to product 1 yields nothing.
    assert cross_product == []


async def test_search_respects_k_and_empty_filters(session: AsyncSession) -> None:
    catalog = await seed_catalog(session)

    assert len(await hybrid_search(session, "gạo", k=1, embedder=None)) == 1
    assert await hybrid_search(session, "gạo", product_ids=[], embedder=None) == []
    assert await hybrid_search(session, "gạo", source_types=[], embedder=None) == []
    assert await hybrid_search(session, "   ", embedder=None) == []
    assert await hybrid_search(session, "gạo", k=0, embedder=None) == []
    assert catalog["meta_chunk"].id  # catalog still intact


async def test_search_without_embedder_never_returns_vector_ranks(session: AsyncSession) -> None:
    await seed_catalog(session)

    hits = await hybrid_search(session, "gạo", embedder=None)

    assert hits
    assert all(hit.vector_rank is None for hit in hits)


# ---------------------------------------------------------------------------
# Sentiment analysis against real rows (mock provider, real Postgres)
# ---------------------------------------------------------------------------


async def _seed_product_with_reviews(
    session: AsyncSession,
    *,
    ratings: list[int],
) -> tuple[Product, list[Review]]:
    product = Product(
        id=uuid.uuid4(),
        platform="tiki",
        external_id=f"p-{uuid.uuid4().hex[:8]}",
        title="Gạo ST25",
        url="https://tiki.vn/gao-p1.html",
        images=[],
        specs={},
        raw={},
    )
    session.add(product)
    reviews = [
        Review(
            id=uuid.uuid4(),
            product_id=product.id,
            external_id=f"r{idx}",
            rating=rating,
            title=f"Tiêu đề {idx}",
            content=f"Nội dung đánh giá số {idx} đủ dài",
            images=[],
            raw={},
        )
        for idx, rating in enumerate(ratings)
    ]
    session.add_all(reviews)
    await session.commit()
    return product, reviews


async def test_sentiment_llm_and_rating_fallback_are_persisted(
    session: AsyncSession,
    llm_client: Any,
    behavior: Any,
) -> None:
    import json

    product, reviews = await _seed_product_with_reviews(session, ratings=[5, 4, 1])
    behavior.scripted_texts = [
        json.dumps(
            {
                "items": [
                    {
                        "id": str(reviews[0].id),
                        "sentiment": "negative",
                        "aspects": [{"aspect": "giao hàng", "sentiment": "negative"}],
                    }
                ]
            }
        )
    ]

    summary = await analyze_product_reviews(
        session, product.id, llm_client=llm_client, batch_size=20
    )
    await session.commit()

    rows = {
        row.external_id: row
        for row in (await session.execute(select(Review).where(Review.product_id == product.id)))
        .scalars()
        .all()
    }
    refreshed = await session.get(Product, product.id)

    assert summary.analyzed == 3
    assert rows["r0"].sentiment == "negative"
    assert rows["r0"].sentiment_source == "llm"
    assert rows["r0"].aspects == [{"aspect": "giao hàng", "sentiment": "negative"}]
    assert rows["r1"].sentiment == "positive" and rows["r1"].sentiment_source == "rating"
    assert rows["r2"].sentiment == "negative" and rows["r2"].sentiment_source == "rating"
    assert refreshed is not None
    assert (
        refreshed.positive_review_count,
        refreshed.negative_review_count,
        refreshed.neutral_review_count,
    ) == (1, 2, 0)

    # Second pass must skip every already-analysed review.
    again = await analyze_product_reviews(session, product.id, llm_client=llm_client, batch_size=20)
    assert again.analyzed == 0
    assert again.skipped == 3


# ---------------------------------------------------------------------------
# Pipeline (ingest_source)
# ---------------------------------------------------------------------------


def product_registry(*, reviews: int = 3) -> AdapterRegistry:
    adapter = FakeAdapter(
        default_product=make_product("1001"),
        default_reviews=[make_review(f"r{idx}", rating=4 + idx % 2) for idx in range(reviews)],
    )
    registry = AdapterRegistry()
    registry.register(adapter)
    return registry


async def crawl_product(session_factory: Any) -> uuid.UUID:
    result = await crawl_url(
        "https://tiki.vn/gao-st25-p1001.html",
        session_factory=session_factory,
        registry=product_registry(),
    )
    assert result.status == "done"
    assert result.source_id is not None
    return result.source_id


async def test_pipeline_ingests_reviews_chunks_and_embeddings(
    session_factory: Any,
) -> None:
    source_id = await crawl_product(session_factory)
    embedder = FakeEmbedder(dim=DIM)

    result = await ingest_source(
        source_id,
        session_factory=session_factory,
        embedder=embedder,
        settings=make_settings(),
    )

    assert result.status == "done"
    assert result.stats.products_ingested == 1
    assert result.stats.products_failed == 0
    assert result.stats.reviews_analyzed == 3
    assert result.stats.reviews_rating_fallback == 3  # no LLM profile configured
    assert result.stats.chunks_total >= 4  # meta + description + specs + reviews
    assert result.stats.chunks_created == result.stats.chunks_total
    assert result.stats.chunks_embedded == result.stats.chunks_total
    assert result.stats.embedding_model == embedder.name
    assert result.stats.embedding_dim == DIM
    assert len(embedder.embedded_texts) == result.stats.chunks_total

    async with session_factory() as session:
        source = await session.get(Source, source_id)
        product = (
            await session.execute(select(Product).where(Product.external_id == "1001"))
        ).scalar_one()
        chunks = (
            await session.execute(select(Chunk).where(Chunk.product_id == product.id))
        ).scalars()
        chunk_rows = list(chunks.all())
        review_rows = list(
            (await session.execute(select(Review).where(Review.product_id == product.id)))
            .scalars()
            .all()
        )

    assert source is not None
    assert source.status == "done"
    assert source.stats["ingest"]["chunks_total"] == result.stats.chunks_total
    assert source.stats["ingest"]["embedding_model"] == embedder.name
    assert product.ingested_at is not None
    assert product.positive_review_count == 3
    assert {chunk.source_type for chunk in chunk_rows} == {"meta", "description", "specs", "review"}
    assert all(chunk.embedding is not None and chunk.dim == DIM for chunk in chunk_rows)
    assert all(chunk.tsv is not None for chunk in chunk_rows)
    assert all(review.sentiment_source == "rating" for review in review_rows)


async def test_pipeline_reingest_is_idempotent_and_does_not_re_embed(
    session_factory: Any,
) -> None:
    source_id = await crawl_product(session_factory)
    first_embedder = FakeEmbedder(dim=DIM)
    first = await ingest_source(
        source_id,
        session_factory=session_factory,
        embedder=first_embedder,
        settings=make_settings(),
    )
    second_embedder = FakeEmbedder(dim=DIM)

    second = await ingest_source(
        source_id,
        session_factory=session_factory,
        embedder=second_embedder,
        settings=make_settings(),
    )

    assert second.status == "done"
    assert second.stats.chunks_created == 0
    assert second.stats.chunks_embedded == 0
    assert second.stats.chunks_deleted == 0
    assert second.stats.chunks_kept == first.stats.chunks_total
    assert second.stats.reviews_skipped == first.stats.reviews_analyzed
    assert second_embedder.calls == [], "unchanged chunks must not be embedded again"

    async with session_factory() as session:
        total = (await session.execute(select(func.count()).select_from(Chunk))).scalar_one()
    assert total == first.stats.chunks_total


async def test_pipeline_isolates_a_failing_product_and_reports_partial(
    session_factory: Any,
    monkeypatch: Any,
) -> None:
    adapter = FakeAdapter(
        shop=make_shop(),
        listings=[make_product("2001"), make_product("2002")],
        products={"2001": make_product("2001"), "2002": make_product("2002")},
        default_reviews=[make_review("r1")],
    )
    registry = AdapterRegistry()
    registry.register(adapter)
    crawl = await crawl_url(
        "https://tiki.vn/cua-hang/tiki-trading",
        session_factory=session_factory,
        registry=registry,
    )
    assert crawl.status == "done" and crawl.source_id is not None

    original = pipeline_module.build_product_chunks

    def _explode(product: Product, *args: Any, **kwargs: Any) -> Any:
        if product.external_id == "2002":
            raise RuntimeError("chunking exploded")
        return original(product, *args, **kwargs)

    monkeypatch.setattr(pipeline_module, "build_product_chunks", _explode)

    result = await ingest_source(
        crawl.source_id,
        session_factory=session_factory,
        embedder=FakeEmbedder(dim=DIM),
        settings=make_settings(),
    )

    assert result.status == "partial"
    assert result.stats.products_total == 2
    assert result.stats.products_ingested == 1
    assert result.stats.products_failed == 1
    assert result.stats.errors and "chunking exploded" in result.stats.errors[0]["error"]
    assert result.error is not None and "1/2" in result.error

    async with session_factory() as session:
        source = await session.get(Source, crawl.source_id)
        ingested = list(
            (await session.execute(select(Product).where(Product.ingested_at.is_not(None))))
            .scalars()
            .all()
        )
    assert source is not None and source.status == "partial"
    assert len(ingested) == 1


async def test_pipeline_raises_for_unknown_source(session_factory: Any) -> None:
    with pytest.raises(ValueError, match="not found"):
        await ingest_source(
            uuid.uuid4(),
            session_factory=session_factory,
            embedder=FakeEmbedder(dim=DIM),
            settings=make_settings(),
        )


# ---------------------------------------------------------------------------
# Jobs: JobEvents over real Redis, SSE endpoint, run_ingest end-to-end
# ---------------------------------------------------------------------------


async def test_job_events_persist_and_publish_over_real_redis(redis_client: Any) -> None:
    events = JobEvents(redis_client)
    job_id = f"job-{uuid.uuid4()}"

    await events.create_job(job_id, url="https://tiki.vn/a-p1.html")
    event = await events.emit(job_id, stage="started", done=0, total=2)
    await events.set_status(job_id, "running", source_id="src-1")

    assert await redis_client.lrange(JobEvents.events_key(job_id), 0, -1)
    assert await redis_client.ttl(JobEvents.status_key(job_id)) > 0
    assert await redis_client.ttl(JobEvents.events_key(job_id)) > 0
    status = await events.get_status(job_id)
    assert status is not None and status["status"] == "running"
    listed = await events.list_events(job_id)
    assert [item["seq"] for item in listed] == [event["seq"]]


async def test_sse_endpoint_replays_and_resumes_against_real_redis(redis_client: Any) -> None:
    events = JobEvents(redis_client)
    job_id = f"job-{uuid.uuid4()}"
    await events.create_job(job_id)
    await events.emit(job_id, stage="started")
    await events.emit(job_id, stage="sentiment", done=1, total=2)
    await events.set_status(job_id, "done", source_id="src-9")
    await events.emit(job_id, stage="done", done=2, total=2)

    app = build_ingest_app(
        session=object(),
        settings=make_settings(),
        job_events=events,
        job_queue=RecordingJobQueue(),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        full = await client.get(f"/api/jobs/{job_id}/events")
        resumed = await client.get(
            f"/api/jobs/{job_id}/events",
            headers={"Last-Event-ID": "2"},
        )
        status_response = await client.get(f"/api/jobs/{job_id}")

    assert full.status_code == 200
    assert full.headers["content-type"].startswith("text/event-stream")
    assert full.text.count("event: progress") == 3
    assert "event: done" in full.text
    assert "id: 1" in full.text and "id: 3" in full.text

    assert resumed.text.count("event: progress") == 1
    assert "id: 3" in resumed.text
    assert "event: done" in resumed.text

    assert status_response.status_code == 200
    assert status_response.json()["status"] == "done"
    assert status_response.json()["source_id"] == "src-9"


async def test_sse_endpoint_reports_failed_jobs_as_error(redis_client: Any) -> None:
    events = JobEvents(redis_client)
    job_id = f"job-{uuid.uuid4()}"
    await events.create_job(job_id)
    await events.emit(job_id, stage="failed", message="crawl boom")
    await events.set_status(job_id, "failed", error="crawl boom")

    app = build_ingest_app(
        session=object(),
        settings=make_settings(),
        job_events=events,
        job_queue=RecordingJobQueue(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get(f"/api/jobs/{job_id}/events")

    assert "event: error" in response.text
    assert "crawl boom" in response.text
    assert "event: done" not in response.text


async def test_run_ingest_crawls_and_ingests_end_to_end(
    session_factory: Any,
    redis_client: Any,
) -> None:
    events = JobEvents(redis_client)
    job_id = f"job-{uuid.uuid4()}"
    await events.create_job(job_id, url="https://tiki.vn/gao-st25-p1001.html")
    embedder = FakeEmbedder(dim=DIM)
    ctx = {
        "session_factory": session_factory,
        "registry": product_registry(),
        "embedder": embedder,
        "job_events": events,
        "settings": make_settings(),
    }

    result = await run_ingest(ctx, job_id, "https://tiki.vn/gao-st25-p1001.html")

    assert result["status"] == "done"
    assert result["source_id"]
    status = await events.get_status(job_id)
    assert status is not None and status["status"] == "done"
    stages = [event["stage"] for event in await events.list_events(job_id)]
    assert stages[0] == "started"
    assert "crawl_done" in stages
    assert "sentiment" in stages and "embed" in stages
    assert stages[-1] == "done"

    async with session_factory() as session:
        source = await session.get(Source, uuid.UUID(result["source_id"]))
        chunk_count = (await session.execute(select(func.count()).select_from(Chunk))).scalar_one()
    assert source is not None
    assert source.stats["ingest"]["chunks_total"] == chunk_count
    assert chunk_count > 0
    assert embedder.embedded_texts, "the task must embed the ingested chunks"


async def test_run_ingest_marks_the_job_failed_when_the_crawl_fails(
    session_factory: Any,
    redis_client: Any,
) -> None:
    events = JobEvents(redis_client)
    job_id = f"job-{uuid.uuid4()}"
    await events.create_job(job_id)
    adapter = FakeAdapter(default_product=RuntimeError("upstream exploded"))
    registry = AdapterRegistry()
    registry.register(adapter)

    result = await run_ingest(
        {"session_factory": session_factory, "registry": registry, "job_events": events},
        job_id,
        "https://tiki.vn/gao-st25-p1001.html",
    )

    assert result["status"] == "failed"
    status = await events.get_status(job_id)
    assert status is not None and status["status"] == "failed"
    assert "upstream exploded" in (status["error"] or "")
    stages = [event["stage"] for event in await events.list_events(job_id)]
    assert stages[-1] == "failed"


# ---------------------------------------------------------------------------
# POST /api/ingest cache semantics against real rows
# ---------------------------------------------------------------------------


async def _app_with_db(session_factory: Any, *, events: Any = None, embedder: Any = None) -> Any:
    async def _session_dependency() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app = build_ingest_app(
        session=object(),
        settings=make_settings(),
        job_events=events or RecordingJobEvents(),
        job_queue=RecordingJobQueue(),
        embedder=embedder,
    )
    from app.api.deps import get_session

    app.dependency_overrides[get_session] = _session_dependency
    return app


async def test_api_ingest_enqueues_when_only_crawl_stats_exist(
    session_factory: Any,
) -> None:
    """A crawl-only source (no ``stats['ingest']``) must not be a cache hit."""
    url = "https://tiki.vn/gao-st25-p1001.html"
    async with session_factory() as session:
        source = await create_source(session, url=url, status="done")
        await update_source(
            session,
            source.id,
            status="done",
            stats={"products_succeeded": 1, "duration_seconds": 1.2},
            external_ref={"product_id": "1001", "canonical_url": url},
            crawled_at=datetime.now(UTC),
        )
        await session.commit()
        source_id = source.id

    events = RecordingJobEvents()
    app = await _app_with_db(session_factory, events=events)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post("/api/ingest", json={"url": url})

    assert response.status_code == 202, "crawl-only sources must be re-enqueued"
    assert response.json()["cached"] is False
    assert response.json()["job_id"]
    assert events.created and events.created[0]["url"] == url
    assert source_id  # row untouched


async def test_api_ingest_returns_cached_after_an_ingested_source(
    session_factory: Any,
) -> None:
    url = "https://tiki.vn/gao-st25-p1001.html"
    crawled_at = datetime.now(UTC) - timedelta(hours=2)
    async with session_factory() as session:
        source = await create_source(session, url=url, status="done")
        await update_source(
            session,
            source.id,
            status="done",
            stats={"ingest": {"chunks_total": 4}},
            external_ref={"product_id": "1001", "canonical_url": url},
            crawled_at=crawled_at,
        )
        await session.commit()
        source_id = source.id

    events = RecordingJobEvents()
    app = await _app_with_db(session_factory, events=events)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        cached = await client.post("/api/ingest", json={"url": url})
        legacy_force = await client.post("/api/ingest", json={"url": url, "force": True})

    assert cached.status_code == 200
    body = cached.json()
    assert body["source_id"] == str(source_id)
    assert body["cached"] is True
    assert datetime.fromisoformat(body["crawled_at"]) == crawled_at
    assert datetime.fromisoformat(body["refresh_after"]) == crawled_at + timedelta(hours=48)
    assert events.created == [], "a cache hit must not create a job"

    assert legacy_force.status_code == 422, "there is no API path to bypass the cache"
    assert events.created == []


@pytest.mark.parametrize(
    ("age", "expected_status"),
    [
        (timedelta(hours=47, minutes=59), 200),
        (timedelta(hours=48, minutes=1), 202),
    ],
)
async def test_api_ingest_cache_window_boundary(
    session_factory: Any,
    age: timedelta,
    expected_status: int,
) -> None:
    url = "https://tiki.vn/gao-st25-p1001.html"
    async with session_factory() as session:
        source = await create_source(session, url=url, status="done")
        await update_source(
            session,
            source.id,
            status="done",
            stats={"ingest": {"chunks_total": 4}},
            external_ref={"product_id": "1001", "canonical_url": url},
            crawled_at=datetime.now(UTC) - age,
        )
        await session.commit()

    events = RecordingJobEvents()
    app = await _app_with_db(session_factory, events=events)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post("/api/ingest", json={"url": url})

    assert response.status_code == expected_status
    assert response.json()["cached"] is (expected_status == 200)
    assert bool(events.created) is (expected_status == 202)


@pytest.mark.parametrize("status", ["failed", "partial"])
async def test_api_ingest_never_serves_failed_or_partial_sources_from_cache(
    session_factory: Any,
    status: str,
) -> None:
    """Only ``done`` sources inside the TTL are reusable; retrying after a failure re-crawls."""
    url = "https://tiki.vn/gao-st25-p1001.html"
    async with session_factory() as session:
        source = await create_source(session, url=url, status=status)
        await update_source(
            session,
            source.id,
            status=status,
            stats={"ingest": {"chunks_total": 4}},
            external_ref={"product_id": "1001", "canonical_url": url},
            crawled_at=datetime.now(UTC),
        )
        await session.commit()

    events = RecordingJobEvents()
    app = await _app_with_db(session_factory, events=events)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post("/api/ingest", json={"url": url})

    assert response.status_code == 202
    assert response.json()["cached"] is False
    assert len(events.created) == 1


async def test_api_ingest_ignores_stale_ingested_sources(session_factory: Any) -> None:
    url = "https://tiki.vn/gao-st25-p1001.html"
    old = datetime(2020, 1, 1, tzinfo=UTC)
    async with session_factory() as session:
        source = await create_source(session, url=url, status="done")
        await update_source(
            session,
            source.id,
            status="done",
            stats={"ingest": {"chunks_total": 4}},
            external_ref={"product_id": "1001", "canonical_url": url},
            crawled_at=old,
        )
        await session.commit()

    app = await _app_with_db(session_factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post("/api/ingest", json={"url": url})

    assert response.status_code == 202


# ---------------------------------------------------------------------------
# GET /api/sources/{id} and POST /api/search against ingested data
# ---------------------------------------------------------------------------


async def test_sources_summary_reports_counts_and_sentiment(session_factory: Any) -> None:
    source_id = await crawl_product(session_factory)
    await ingest_source(
        source_id,
        session_factory=session_factory,
        embedder=FakeEmbedder(dim=DIM),
        settings=make_settings(),
    )

    app = await _app_with_db(session_factory, embedder=FakeEmbedder(dim=DIM))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get(f"/api/sources/{source_id}")
        missing = await client.get(f"/api/sources/{uuid.uuid4()}")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "done"
    assert body["products_count"] == 1
    assert body["reviews_count"] == 3
    assert body["sentiment_distribution"]["positive"] == 3
    assert body["sentiment_distribution"]["unanalyzed"] == 0
    assert body["chunks_count"] > 0
    assert set(body["chunks_by_type"]) == {"meta", "description", "specs", "review"}
    assert body["ingested_at"] is not None
    assert body["stats"]["ingest"]["chunks_total"] == body["chunks_count"]

    assert missing.status_code == 404


async def test_search_endpoint_uses_the_embedder_override(session_factory: Any) -> None:
    source_id = await crawl_product(session_factory)
    await ingest_source(
        source_id,
        session_factory=session_factory,
        embedder=FakeEmbedder(dim=DIM),
        settings=make_settings(),
    )

    embedder = FakeEmbedder(dim=DIM)
    app = await _app_with_db(session_factory, embedder=embedder)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/search",
            json={"query": "giao hàng nhanh", "source_id": str(source_id), "k": 3},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == len(body["hits"])
    assert body["total"] >= 1
    assert embedder.calls, "the override embedder must be used for the vector branch"
    assert any(hit["source_type"] == "description" for hit in body["hits"])


# ---------------------------------------------------------------------------
# Pipeline: LLM profile, missing products, null embedder, scope resolution
# ---------------------------------------------------------------------------


async def test_pipeline_uses_the_llm_profile_for_sentiment(
    session_factory: Any,
    llm_client: Any,
    behavior: Any,
) -> None:
    import json

    source_id = await crawl_product(session_factory)
    async with session_factory() as session:
        profile = LLMProfile(
            name="mock provider",
            base_url="http://mock/v1",
            chat_model="mock-chat",
        )
        session.add(profile)
        await session.commit()
        await session.refresh(profile)
        profile_id = profile.id
        review_ids = [
            row.id
            for row in (await session.execute(select(Review).order_by(Review.external_id)))
            .scalars()
            .all()
        ]

    behavior.scripted_texts = [
        json.dumps(
            {
                "items": [
                    {
                        "id": str(review_id),
                        "sentiment": "positive",
                        "aspects": [{"aspect": "chất lượng", "sentiment": "positive"}],
                    }
                    for review_id in review_ids
                ]
            }
        )
    ]

    result = await ingest_source(
        source_id,
        llm_profile_id=profile_id,
        llm_client=llm_client,
        session_factory=session_factory,
        embedder=FakeEmbedder(dim=DIM),
        settings=make_settings(),
    )

    assert result.status == "done"
    assert result.stats.reviews_llm == 3
    assert result.stats.reviews_rating_fallback == 0
    assert behavior.chat_calls >= 1
    async with session_factory() as session:
        sources = [
            row.sentiment_source for row in (await session.execute(select(Review))).scalars().all()
        ]
    assert set(sources) == {"llm"}


async def test_pipeline_falls_back_to_ratings_when_the_profile_is_missing(
    session_factory: Any,
) -> None:
    source_id = await crawl_product(session_factory)

    result = await ingest_source(
        source_id,
        llm_profile_id=uuid.uuid4(),  # not in the database
        session_factory=session_factory,
        embedder=FakeEmbedder(dim=DIM),
        settings=make_settings(),
    )

    assert result.status == "done"
    assert result.stats.reviews_llm == 0
    assert result.stats.reviews_rating_fallback == 3


async def test_pipeline_fails_when_every_product_fails(
    session_factory: Any,
    monkeypatch: Any,
) -> None:
    source_id = await crawl_product(session_factory)

    def _explode(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("chunking exploded")

    monkeypatch.setattr(pipeline_module, "build_product_chunks", _explode)

    result = await ingest_source(
        source_id,
        session_factory=session_factory,
        embedder=FakeEmbedder(dim=DIM),
        settings=make_settings(),
    )

    assert result.status == "failed"
    assert result.stats.products_failed == 1
    assert result.error is not None and "All 1" in result.error
    assert "chunking exploded" in result.stats.errors[0]["error"]

    async with session_factory() as session:
        source_row = await session.get(Source, source_id)
    assert source_row is not None
    assert source_row.status == "failed"
    assert source_row.stats["ingest"]["products_failed"] == 1


async def test_pipeline_with_null_embedder_keeps_chunks_text_only(session_factory: Any) -> None:
    source_id = await crawl_product(session_factory)

    result = await ingest_source(
        source_id,
        session_factory=session_factory,
        embedder=NullEmbedder(),
        settings=make_settings(),
    )

    assert result.status == "done"
    assert result.stats.embedding_model is None
    assert result.stats.chunks_embedded == 0
    async with session_factory() as session:
        chunks = list((await session.execute(select(Chunk))).scalars().all())
    assert chunks
    assert all(chunk.embedding is None and chunk.dim is None for chunk in chunks)


async def _seed_shop_scope(
    session_factory: Any,
    *,
    external_ref: dict[str, Any],
    url_type: str = "shop",
) -> dict[str, Any]:
    async with session_factory() as session:
        shop = Shop(
            platform="tiki",
            external_id="seller-x",
            name="Shop X",
            url="https://tiki.vn/cua-hang/shop-x",
            raw={},
        )
        session.add(shop)
        await session.flush()
        products = [
            Product(
                platform="tiki",
                external_id=f"90{idx}",
                title=f"Sản phẩm {idx}",
                url=f"https://tiki.vn/san-pham-p90{idx}.html",
                shop_id=shop.id,
                images=[],
                specs={},
                raw={},
            )
            for idx in range(2)
        ]
        session.add_all(products)
        await session.flush()
        source = await create_source(session, url="https://tiki.vn/cua-hang/shop-x", status="done")
        await update_source(
            session,
            source.id,
            status="done",
            url_type=url_type,
            platform="tiki",
            external_ref=external_ref,
            crawled_at=datetime.now(UTC),
        )
        await session.commit()
        return {"source_id": source.id, "shop_id": shop.id, "product_ids": [p.id for p in products]}


async def _resolve_scope(session_factory: Any, source_id: Any) -> tuple[Shop | None, list[Product]]:
    async with session_factory() as session:
        source = await session.get(Source, source_id)
        assert source is not None
        return await resolve_source_scope(session, source)


@pytest.mark.parametrize(
    "external_ref",
    [
        {"seller_id": "seller-x"},
        {"canonical_url": "https://tiki.vn/cua-hang/shop-x"},
        {"shop_slug": "shop-x"},
    ],
)
async def test_resolve_source_scope_finds_the_shop_for_shop_sources(
    session_factory: Any,
    external_ref: dict[str, Any],
) -> None:
    seeded = await _seed_shop_scope(session_factory, external_ref=external_ref)

    shop, products = await _resolve_scope(session_factory, seeded["source_id"])

    assert shop is not None and shop.id == seeded["shop_id"]
    assert {product.id for product in products} == set(seeded["product_ids"])


async def test_resolve_source_scope_looks_up_products_by_external_id(
    session_factory: Any,
) -> None:
    seeded = await _seed_shop_scope(
        session_factory,
        external_ref={"product_id": "900"},
        url_type="product",
    )

    shop, products = await _resolve_scope(session_factory, seeded["source_id"])

    assert [product.external_id for product in products] == ["900"]
    # No shop in external_ref: it is derived from the product row.
    assert shop is not None and shop.id == seeded["shop_id"]


async def test_resolve_source_scope_returns_empty_for_unrelated_sources(
    session_factory: Any,
) -> None:
    async with session_factory() as session:
        source = await create_source(session, url="https://tiki.vn/khong-ro", status="done")
        await update_source(
            session,
            source.id,
            status="done",
            url_type="product",
            platform="tiki",
            external_ref={"product_id": "does-not-exist"},
            crawled_at=datetime.now(UTC),
        )
        await session.commit()
        source_id = source.id

    shop, products = await _resolve_scope(session_factory, source_id)

    assert shop is None
    assert products == []


async def test_job_queue_enqueues_into_real_redis(redis_client: Any) -> None:
    queue = JobQueue()
    queue.init(TEST_REDIS_URL)
    job_id = f"job-{uuid.uuid4()}"
    try:
        job = await queue.enqueue_ingest(
            job_id=job_id,
            url="https://tiki.vn/gao-st25-p1001.html",
            max_products=1,
            max_reviews=5,
        )
        keys = [key async for key in redis_client.scan_iter(match="*")]
    finally:
        await queue.close()

    assert job is not None
    assert job.job_id == job_id
    assert any(job_id in key for key in keys), f"no arq job key for {job_id}: {keys}"
