"""Phase 4 integration tests against a dedicated Postgres database + Redis DB 15.

Never touches the project database ``shopqa`` (real data), Redis DB 0 (worker keys) or the
running api/worker containers: everything is created in ``shopqa_phase4_test`` and flushed
after each test. Covers the SQL-backed agent tools, the F2 cosine threshold, F4
``no_products``, the Lua compare-and-set in ``JobEvents`` (G1/G2) and chat persistence.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
import redis.asyncio as aioredis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.agent.citations import CitationRegistry
from app.agent.tools import (
    AgentContext,
    _run_get_product,
    _run_query_products,
    _run_search_product_docs,
    _run_search_reviews,
    _run_summarize_reviews,
    build_agent_tools,
)
from app.agent.tools import (
    GetProductArgs as ArgsGetProduct,
)
from app.agent.tools import (
    QueryProductsArgs as ArgsQueryProducts,
)
from app.agent.tools import (
    SearchProductDocsArgs as ArgsSearchProductDocs,
)
from app.agent.tools import (
    SearchReviewsArgs as ArgsSearchReviews,
)
from app.agent.tools import (
    SummarizeReviewsArgs as ArgsSummarizeReviews,
)
from app.api.deps import (
    get_embedder_override,
    get_job_events,
    get_job_queue,
    get_registry,
    get_secret_box,
    get_session,
    get_session_factory,
    get_settings,
    get_url_resolver,
)
from app.core.config import Settings
from app.db.models import ChatMessage, ChatSession, Chunk, LLMProfile, Product, Review, Shop, Source
from app.ingest.chunking import compute_content_hash
from app.ingest.embedding import NullEmbedder
from app.ingest.pipeline import ingest_source
from app.ingest.search import hybrid_search
from app.jobs.events import JobEvents
from app.llm.registry import LLMClientRegistry
from app.main import create_app
from app.security.secrets import SecretBox
from tests.ingest_fakes import FakeEmbedder, RecordingJobEvents, RecordingJobQueue
from tests.mock_openai import MockBehavior, build_mock_app

pytestmark = pytest.mark.integration

BACKEND_DIR = Path(__file__).resolve().parents[1]
SECRET = "vN5y6rM0J9h2xL8qK3pW1zB4cF7dG0tH2sV5mN8wX4Q="

PG_HOST = os.environ.get("TEST_PHASE4_PG_HOST", "localhost")
PG_PORT = int(os.environ.get("TEST_PHASE4_PG_PORT", "5433"))
PG_USER = os.environ.get("TEST_PHASE4_PG_USER", "shopqa")
PG_PASSWORD = os.environ.get("TEST_PHASE4_PG_PASSWORD", "shopqa")
PG_MAINTENANCE_DB = os.environ.get("TEST_PHASE4_PG_MAINTENANCE_DB", "shopqa")
TEST_DB_NAME = os.environ.get("TEST_PHASE4_DB_NAME", "shopqa_phase4_test")
TEST_REDIS_DB = int(os.environ.get("TEST_PHASE4_REDIS_DB", "15"))
PROJECT_DB_NAME = "shopqa"

MAINTENANCE_DSN = f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{PG_MAINTENANCE_DB}"
TEST_DATABASE_URL = (
    f"postgresql+asyncpg://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{TEST_DB_NAME}"
)
TEST_REDIS_URL = f"redis://{PG_HOST}:6380/{TEST_REDIS_DB}"
DIM = 1024


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "SECRET_KEY": SECRET,
        "DATABASE_URL": TEST_DATABASE_URL,
        "REDIS_URL": TEST_REDIS_URL,
        "EMBEDDING_BACKEND": "none",
        "AGENT_MAX_STEPS": 4,
        "AGENT_CRAWL_MAX_PER_TURN": 1,
        "AGENT_CRAWL_MAX_PER_SESSION": 3,
        "SEARCH_MIN_COSINE_SIMILARITY": 0.6,
    }
    values.update(overrides)
    return Settings(**values)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


async def _ensure_test_database() -> None:
    conn = await asyncpg.connect(MAINTENANCE_DSN)
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", TEST_DB_NAME)
        if not exists:
            await conn.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def phase4_database_url() -> str:
    assert TEST_DB_NAME != PROJECT_DB_NAME, "integration tests must not target the project DB"
    try:
        asyncio.run(_ensure_test_database())
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable at {MAINTENANCE_DSN}: {exc}")

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
async def session_factory(phase4_database_url: str) -> AsyncIterator[Any]:
    engine = create_async_engine(phase4_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            current = (await conn.execute(text("SELECT current_database()"))).scalar_one()
            assert current == TEST_DB_NAME, f"refusing to truncate {current}"
            await conn.execute(
                text(
                    "TRUNCATE chat_messages, chat_sessions, review_summaries, chunks, "
                    "reviews, products, shops, sources CASCADE"
                )
            )
        yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
async def session(session_factory: Any) -> AsyncIterator[AsyncSession]:
    async with session_factory() as active_session:
        yield active_session


@pytest.fixture
async def redis_client() -> AsyncIterator[Any]:
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


class MappedEmbedder(FakeEmbedder):
    """FakeEmbedder with explicit query→vector mappings for known cosine values."""

    def __init__(self, mapping: dict[str, list[float]] | None = None) -> None:
        super().__init__(dim=DIM)
        self.mapping = mapping or {}

    def vector(self, text: str) -> list[float]:
        return self.mapping.get(text) or super().vector(text)

    @staticmethod
    def basis(index: int) -> list[float]:
        vec = [0.0] * DIM
        vec[index] = 1.0
        return vec


def _normalized_pair(cosine: float, index: int = 0) -> tuple[list[float], list[float]]:
    """Return (chunk_vector, query_vector) whose cosine similarity is exactly ``cosine``."""
    chunk = [0.0] * DIM
    chunk[0] = 1.0
    query = [0.0] * DIM
    query[0] = cosine
    query[1] = (1 - cosine**2) ** 0.5
    return chunk, query


# ---------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------


async def seed_catalog(session: AsyncSession) -> dict[str, Any]:
    shop = Shop(
        id=uuid.uuid4(),
        platform="tiki",
        external_id="seller-p4",
        name="Shop Phase 4",
        url="https://tiki.vn/cua-hang/shop-phase-4",
        raw={},
    )
    session.add(shop)
    await session.flush()

    def product(
        external_id: str,
        title: str,
        *,
        price: str | None = "125000",
        list_price: str | None = None,
        sold: int | None = 10,
        rating: float | None = 4.5,
        rating_count: int | None = 5,
        reviews: int | None = 5,
        positive: int | None = 3,
        negative: int | None = 1,
        neutral: int | None = 1,
        brand: str | None = "Harmony",
        category: str | None = "Thực phẩm",
        crawled_at: datetime | None = None,
    ) -> Product:
        from decimal import Decimal

        return Product(
            id=uuid.uuid4(),
            shop_id=shop.id,
            platform="tiki",
            external_id=external_id,
            title=title,
            url=f"https://tiki.vn/{external_id}.html",
            price=Decimal(price) if price is not None else None,
            list_price=Decimal(list_price) if list_price is not None else None,
            currency="VND",
            sold_count=sold,
            rating_avg=rating,
            rating_count=rating_count,
            review_count=reviews,
            positive_review_count=positive,
            negative_review_count=negative,
            neutral_review_count=neutral,
            brand=brand,
            category=category,
            short_description=f"{title} chính hãng.",
            specs={"Xuất xứ": "Việt Nam", "Khối lượng": "5kg"},
            images=[],
            raw={},
            crawled_at=crawled_at or datetime(2026, 10, 1, tzinfo=UTC),
        )

    products = [
        product(
            "2001",
            "Gạo ST25 5kg",
            price="125000",
            list_price="150000",
            sold=500,
            positive=4,
        ),
        product("2002", "Gạo ST24 5kg", price="99000", list_price="99000", sold=900),
        product(
            "2003",
            "Nếp thơm 5kg",
            price="150000",
            list_price=None,
            sold=None,
            rating=None,
            rating_count=None,
            reviews=None,
            positive=None,
            negative=None,
            neutral=None,
        ),
        product(
            "2004",
            "Gạo lứt 1kg",
            price="45000",
            list_price="50000",
            sold=100,
            rating=4.8,
            rating_count=1000,
        ),
        product(
            "2005",
            "Gạo Nhật 2kg",
            price=None,
            sold=50,
            rating=4.8,
            rating_count=10,
            brand="Khác",
            category="Gạo nhập khẩu",
        ),
    ]
    session.add_all(products)
    await session.flush()

    reviews: list[Review] = []
    review_specs = [
        ("r1", products[0], 5, "positive", "Cơm dẻo thơm, giao nhanh", 10, ["vị ngon"]),
        ("r2", products[0], 4, "positive", "Đóng gói cẩn thận", 3, ["đóng gói"]),
        ("r3", products[0], 2, "negative", "Bao bì rách, giao chậm", 7, ["đóng gói"]),
        ("r4", products[0], 3, "neutral", "Bình thường", 1, []),
        ("r5", products[0], 1, "negative", "Gạo bị mốc", 9, ["chất lượng"]),
        ("r6", products[1], 5, "positive", "Rẻ mà ngon", 2, ["giá"]),
        ("r7", products[3], 4, None, "Chưa phân tích", 0, []),
    ]
    for ext, prod, rating, sentiment, content, helpful, aspects in review_specs:
        review = Review(
            id=uuid.uuid4(),
            product_id=prod.id,
            external_id=ext,
            rating=rating,
            title=None,
            content=content,
            author="khách",
            created_at_source=datetime(2026, 9, int(ext[1:]) % 27 + 1, tzinfo=UTC),
            helpful_count=helpful,
            sentiment=sentiment,
            sentiment_source="rating" if sentiment else None,
            aspects=[{"aspect": item, "sentiment": sentiment or "positive"} for item in aspects],
            content_hash=compute_content_hash(content),
            images=[],
            raw={},
        )
        reviews.append(review)
    session.add_all(reviews)

    embedder = MappedEmbedder()
    chunk_specs = [
        (products[0], "description", "0", "Gạo ST25 thơm dẻo, nấu cơm rất ngon", embedder.basis(5)),
        (products[0], "specs", "specs", "Xuất xứ Việt Nam, khối lượng 5kg", None),
        (products[0], "meta", "meta", "Gạo ST25 5kg giá 125000", embedder.basis(6)),
        (products[3], "description", "0", "Gạo lứt giàu chất xơ", embedder.basis(7)),
    ]
    for idx, review in enumerate(reviews):
        chunk_specs.append(
            (
                next(p for p in products if p.id == review.product_id),
                "review",
                review.external_id,
                review.content or "",
                embedder.basis(idx),
            )
        )

    for prod, source_type, source_ref, body, embedding in chunk_specs:
        session.add(
            Chunk(
                id=uuid.uuid4(),
                product_id=prod.id,
                shop_id=shop.id,
                source_type=source_type,
                source_ref=source_ref,
                text=body,
                content_hash=compute_content_hash(body),
                embedding=embedding,
                embedding_model="fake-embedder" if embedding is not None else None,
                dim=len(embedding) if embedding is not None else None,
            )
        )

    source = Source(
        id=uuid.uuid4(),
        url=shop.url,
        url_type="shop",
        platform="tiki",
        status="done",
        stats={"ingest": {"products_ingested": 5}},
        external_ref={"shop_id": str(shop.id), "seller_id": shop.external_id},
        crawled_at=datetime(2026, 10, 1, tzinfo=UTC),
    )
    session.add(source)
    await session.commit()
    return {
        "shop": shop,
        "products": products,
        "reviews": reviews,
        "embedder": embedder,
        "source": source,
    }


def make_ctx(
    session_factory: Any,
    catalog: dict[str, Any],
    *,
    llm_client: Any = None,
    settings: Settings | None = None,
) -> AgentContext:
    return AgentContext(
        session_factory=session_factory,
        source=catalog["source"],
        shop=catalog["shop"],
        products=list(catalog["products"]),
        embedder=catalog["embedder"],
        llm_client=llm_client,
        profile=None,
        settings=settings or make_settings(),
        citations=CitationRegistry(),
    )


# ---------------------------------------------------------------------------
# query_products (real SQL)
# ---------------------------------------------------------------------------


async def test_query_products_orders_by_every_whitelisted_column(
    session: AsyncSession, session_factory: Any
) -> None:
    catalog = await seed_catalog(session)
    ctx = make_ctx(session_factory, catalog)

    sold = await _run_query_products(ctx, ArgsQueryProducts(sort_by="sold_count"))
    assert [item["external_id"] for item in sold.data["items"]][:2] == ["2002", "2001"]
    assert sold.data["items"][-1]["external_id"] == "2003", "NULL sold_count sorts last"
    assert sold.data["total_in_scope"] == 5
    assert sold.data["total_matching"] == 5

    rating = await _run_query_products(ctx, ArgsQueryProducts(sort_by="rating_avg"))
    assert [item["external_id"] for item in rating.data["items"]][:2] == ["2004", "2005"]
    assert rating.data["items"][-1]["external_id"] == "2003", "NULL rating_avg sorts last"

    price_asc = await _run_query_products(
        ctx, ArgsQueryProducts(sort_by="price", order="asc", limit=3)
    )
    assert [item["external_id"] for item in price_asc.data["items"]][:2] == ["2004", "2002"]

    discount = await _run_query_products(ctx, ArgsQueryProducts(sort_by="discount_percent"))
    top = discount.data["items"][0]
    assert top["external_id"] == "2001"
    assert top["discount_percent"] == pytest.approx(16.7, abs=0.1)

    reviews = await _run_query_products(ctx, ArgsQueryProducts(sort_by="positive_review_count"))
    assert reviews.data["items"][0]["external_id"] == "2001"


async def test_query_products_rating_tie_breaks_on_rating_count(
    session: AsyncSession, session_factory: Any
) -> None:
    catalog = await seed_catalog(session)
    ctx = make_ctx(session_factory, catalog)

    result = await _run_query_products(ctx, ArgsQueryProducts(sort_by="rating_avg", limit=2))

    # 2004 and 2005 share 4.8 but 2004 has 1000 ratings → first.
    assert [item["external_id"] for item in result.data["items"]] == ["2004", "2005"]


async def test_query_products_applies_filters_and_reports_totals(
    session: AsyncSession, session_factory: Any
) -> None:
    catalog = await seed_catalog(session)
    ctx = make_ctx(session_factory, catalog)

    cheap = await _run_query_products(ctx, ArgsQueryProducts(filters={"max_price": 100000}))
    assert {item["external_id"] for item in cheap.data["items"]} == {"2002", "2004"}
    assert cheap.data["total_matching"] == 2
    assert cheap.data["total_in_scope"] == 5

    by_brand = await _run_query_products(ctx, ArgsQueryProducts(filters={"brand": "Harmony"}))
    assert by_brand.data["total_matching"] == 4

    by_title = await _run_query_products(ctx, ArgsQueryProducts(filters={"title_contains": "st25"}))
    assert [item["external_id"] for item in by_title.data["items"]] == ["2001"]

    by_rating = await _run_query_products(ctx, ArgsQueryProducts(filters={"min_rating": 4.8}))
    assert {item["external_id"] for item in by_rating.data["items"]} == {"2004", "2005"}

    by_reviews = await _run_query_products(ctx, ArgsQueryProducts(filters={"min_reviews": 5}))
    assert {item["external_id"] for item in by_reviews.data["items"]} == {
        "2001",
        "2002",
        "2004",
        "2005",
    }

    by_category = await _run_query_products(
        ctx, ArgsQueryProducts(filters={"category_contains": "nhập khẩu"})
    )
    assert [item["external_id"] for item in by_category.data["items"]] == ["2005"]


async def test_query_products_rejects_inverted_price_range(
    session: AsyncSession, session_factory: Any
) -> None:
    catalog = await seed_catalog(session)
    ctx = make_ctx(session_factory, catalog)

    result = await _run_query_products(
        ctx, ArgsQueryProducts(filters={"min_price": 200000, "max_price": 100000})
    )

    assert result.ok is False
    assert "min_price" in (result.error or "")


async def test_query_products_respects_limit_bounds_and_scope(
    session: AsyncSession, session_factory: Any
) -> None:
    catalog = await seed_catalog(session)
    ctx = make_ctx(session_factory, catalog)
    ctx.products = ctx.products[:2]
    ctx.__post_init__()

    result = await _run_query_products(ctx, ArgsQueryProducts(limit=20))

    assert result.data["total_in_scope"] == 2
    assert len(result.data["items"]) == 2
    assert {item["external_id"] for item in result.data["items"]} == {"2001", "2002"}


async def test_query_products_refs_are_reusable_across_tools(
    session: AsyncSession, session_factory: Any
) -> None:
    catalog = await seed_catalog(session)
    ctx = make_ctx(session_factory, catalog)

    listing = await _run_query_products(ctx, ArgsQueryProducts(limit=2))
    first_ref = listing.data["items"][0]["ref"]

    detail = await _run_get_product(ctx, ArgsGetProduct(product_id=first_ref))

    assert detail.ok is True
    assert detail.data["ref"] == first_ref
    assert detail.data["external_id"] == listing.data["items"][0]["external_id"]


# ---------------------------------------------------------------------------
# get_product / search_product_docs
# ---------------------------------------------------------------------------


async def test_get_product_reads_real_row_and_trims_specs(
    session: AsyncSession, session_factory: Any
) -> None:
    catalog = await seed_catalog(session)
    target = catalog["products"][0]
    target.specs = {f"K{i}": f"V{i}" for i in range(30)}
    await session.commit()
    ctx = make_ctx(session_factory, catalog)

    result = await _run_get_product(ctx, ArgsGetProduct(product_id=target.external_id))

    assert result.ok is True
    assert result.data["title"] == target.title
    assert len(result.data["specs"]) == 20
    assert result.data["discount_percent"] == pytest.approx(16.7, abs=0.1)


async def test_search_product_docs_returns_description_chunks_with_doc_refs(
    session: AsyncSession, session_factory: Any
) -> None:
    catalog = await seed_catalog(session)
    query = "gạo thơm dẻo"
    catalog["embedder"].mapping[query] = catalog["embedder"].basis(5)
    ctx = make_ctx(session_factory, catalog)

    result = await _run_search_product_docs(
        ctx, ArgsSearchProductDocs(query=query, product_id="2001", limit=5)
    )

    assert result.ok is True
    items = result.data["items"]
    assert items
    assert all(item["ref"].startswith("D") for item in items)
    assert all(item["product_ref"] == "P1" for item in items)
    assert all(item["text"].startswith("<<<UNTRUSTED_START>>>") for item in items)
    assert any(item["source_type"] == "description" for item in items)


async def test_search_product_docs_rejects_out_of_scope_product(
    session: AsyncSession, session_factory: Any
) -> None:
    catalog = await seed_catalog(session)
    out_of_scope = catalog["products"][1]
    catalog["products"] = catalog["products"][:1]
    ctx = make_ctx(session_factory, catalog)

    result = await build_agent_tools()["search_product_docs"].run(
        ctx, {"query": "gạo", "product_id": str(out_of_scope.id)}
    )

    assert result.ok is False
    assert "outside the current source scope" in (result.error or "")


# ---------------------------------------------------------------------------
# search_reviews
# ---------------------------------------------------------------------------


async def test_search_reviews_filters_before_truncating(
    session: AsyncSession, session_factory: Any
) -> None:
    catalog = await seed_catalog(session)
    query = "đóng gói"
    # r3 (external_id "r3") is the negative review about packaging → chunk basis(2).
    catalog["embedder"].mapping[query] = catalog["embedder"].basis(2)
    ctx = make_ctx(session_factory, catalog)

    result = await _run_search_reviews(
        ctx, ArgsSearchReviews(query=query, product_id="2001", sentiment="negative", limit=1)
    )

    assert result.ok is True
    assert result.result_count == 1
    item = result.data["items"][0]
    assert item["sentiment"] == "negative"
    assert "Bao bì rách" in item["content"]
    assert item["ref"].startswith("R")
    assert item["similarity"] == pytest.approx(1.0, abs=1e-6)


async def test_search_reviews_without_query_orders_by_helpful_count(
    session: AsyncSession, session_factory: Any
) -> None:
    catalog = await seed_catalog(session)
    ctx = make_ctx(session_factory, catalog)

    result = await _run_search_reviews(ctx, ArgsSearchReviews(product_id="2001", limit=3))

    contents = [item["content"] for item in result.data["items"]]
    # Ordered by helpful_count desc: r1(10), r5(9), r3(7).
    assert contents[0].startswith("<<<UNTRUSTED_START>>>")
    assert "Cơm dẻo thơm" in contents[0]
    assert "Gạo bị mốc" in contents[1]
    assert "Bao bì rách" in contents[2]
    assert all("[R" not in item["ref"] for item in result.data["items"])


async def test_search_reviews_applies_rating_range_and_sentiment(
    session: AsyncSession, session_factory: Any
) -> None:
    catalog = await seed_catalog(session)
    ctx = make_ctx(session_factory, catalog)

    result = await _run_search_reviews(
        ctx, ArgsSearchReviews(product_id="2001", min_rating=4, max_rating=5)
    )

    assert {item["rating"] for item in result.data["items"]} == {4, 5}

    inverted = await _run_search_reviews(
        ctx.clone_for_test() if hasattr(ctx, "clone_for_test") else ctx,
        ArgsSearchReviews(product_id="2001", min_rating=5, max_rating=2),
    )
    assert inverted.ok is False


# ---------------------------------------------------------------------------
# summarize_reviews (deterministic SQL + LLM cache)
# ---------------------------------------------------------------------------


class SummaryLLM:
    """Minimal LLM double: counts calls and returns a fixed JSON summary."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail
        self.last_messages: list[dict[str, Any]] = []

    async def chat(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("summarize_reviews must use complete_json → chat")

    async def close(self) -> None:
        return None


class _StubLLM:
    """Object with the ``config`` attributes ``summarize_reviews`` reads."""

    def __init__(self) -> None:
        from types import SimpleNamespace

        self.config = SimpleNamespace(chat_model="stub-chat", small_model=None, api_key=None)


async def _summarize(
    ctx: AgentContext,
    *,
    product_id: str = "2001",
) -> Any:
    return await _run_summarize_reviews(ctx, ArgsSummarizeReviews(product_id=product_id))


async def test_summarize_reviews_deterministic_part_matches_the_rows(
    session: AsyncSession, session_factory: Any, monkeypatch: Any
) -> None:
    import app.agent.tools as tools_module

    catalog = await seed_catalog(session)
    ctx = make_ctx(session_factory, catalog, llm_client=_StubLLM())

    async def _llm_summary(*_args: Any, **_kwargs: Any) -> Any:
        from app.agent.tools import ReviewLLMSummary

        return ReviewLLMSummary(pros=["thơm"], cons=["bao bì"], verdict="ổn")

    monkeypatch.setattr(tools_module, "complete_json", _llm_summary)

    result = await _summarize(ctx)

    assert result.ok is True
    data = result.data
    assert data["star_distribution"] == {"1": 1, "2": 1, "3": 1, "4": 1, "5": 1}
    assert data["sentiment_counts"]["negative"] == 2
    assert data["total_reviews_collected"] == 5
    assert data["top_negative_aspects"][0]["aspect"] in {"đóng gói", "chất lượng"}
    assert data["llm_summary"] == {"pros": ["thơm"], "cons": ["bao bì"], "verdict": "ổn"}
    assert data["summary_cached"] is False


async def test_summarize_reviews_caches_by_fingerprint(
    session: AsyncSession, session_factory: Any, monkeypatch: Any
) -> None:
    import app.agent.tools as tools_module

    catalog = await seed_catalog(session)
    ctx = make_ctx(session_factory, catalog, llm_client=_StubLLM())
    calls = {"n": 0}

    async def _llm_summary(*_args: Any, **_kwargs: Any) -> Any:
        from app.agent.tools import ReviewLLMSummary

        calls["n"] += 1
        return ReviewLLMSummary(pros=["a"], cons=[], verdict=f"lần {calls['n']}")

    monkeypatch.setattr(tools_module, "complete_json", _llm_summary)

    first = await _summarize(ctx)
    second = await _summarize(ctx)

    assert calls["n"] == 1, "the second turn must hit the cache"
    assert first.data["summary_cached"] is False
    assert second.data["summary_cached"] is True
    assert second.data["llm_summary"] == first.data["llm_summary"]


async def test_summarize_reviews_recomputes_when_a_review_changes(
    session: AsyncSession, session_factory: Any, monkeypatch: Any
) -> None:
    import app.agent.tools as tools_module

    catalog = await seed_catalog(session)
    ctx = make_ctx(session_factory, catalog, llm_client=_StubLLM())
    calls = {"n": 0}

    async def _llm_summary(*_args: Any, **_kwargs: Any) -> Any:
        from app.agent.tools import ReviewLLMSummary

        calls["n"] += 1
        return ReviewLLMSummary(pros=[f"lần {calls['n']}"], cons=[], verdict="v")

    monkeypatch.setattr(tools_module, "complete_json", _llm_summary)
    await _summarize(ctx)

    review = catalog["reviews"][0]
    review.content = "Nội dung đã đổi"
    review.content_hash = compute_content_hash(review.content)
    await session.commit()

    second = await _summarize(ctx)

    assert calls["n"] == 2
    assert second.data["summary_cached"] is False


async def test_summarize_reviews_keeps_deterministic_part_when_the_llm_fails(
    session: AsyncSession, session_factory: Any, monkeypatch: Any
) -> None:
    import app.agent.tools as tools_module

    catalog = await seed_catalog(session)
    ctx = make_ctx(session_factory, catalog, llm_client=_StubLLM())

    async def _explode(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("provider down")

    monkeypatch.setattr(tools_module, "complete_json", _explode)

    result = await _summarize(ctx)

    assert result.ok is True
    assert result.data["llm_summary"] is None
    assert result.data["total_reviews_collected"] == 5
    assert result.data["star_distribution"]["5"] == 1


async def test_summarize_reviews_reports_no_reviews(
    session: AsyncSession, session_factory: Any
) -> None:
    catalog = await seed_catalog(session)
    ctx = make_ctx(session_factory, catalog)

    result = await _summarize(ctx, product_id="2002")
    # 2002 has a single review (r6); 2003 has none.
    assert result.data["total_reviews_collected"] == 1

    empty = await _summarize(ctx, product_id="2003")
    assert empty.data["total_reviews_collected"] == 0
    assert empty.data["llm_summary"] is None
    assert empty.data["star_distribution"] == {str(i): 0 for i in range(1, 6)}


# ---------------------------------------------------------------------------
# F2 — cosine similarity threshold
# ---------------------------------------------------------------------------


async def test_hybrid_search_threshold_drops_low_similarity_vector_hits(
    session: AsyncSession, session_factory: Any
) -> None:
    catalog = await seed_catalog(session)
    embedder = catalog["embedder"]
    chunk_vector, query_vector = _normalized_pair(0.61)
    chunk_vector_low, query_vector_low = _normalized_pair(0.59)
    query = "truy vấn cosine"
    embedder.mapping[query] = query_vector
    embedder.mapping["truy vấn cosine thấp"] = query_vector_low
    target = catalog["products"][0]
    await session.execute(
        text(
            "UPDATE chunks SET embedding = :vec "
            "WHERE product_id = :pid AND source_type = 'description'"
        ),
        {"vec": str(chunk_vector), "pid": target.id},
    )
    await session.commit()

    above = await hybrid_search(
        session,
        query,
        product_ids=[target.id],
        source_types=["description"],
        min_similarity=0.6,
        embedder=embedder,
    )
    assert above, "0.61 >= 0.6 must be kept"
    assert above[0].similarity == pytest.approx(0.61, abs=1e-6)

    below = await hybrid_search(
        session,
        "truy vấn cosine thấp",
        product_ids=[target.id],
        source_types=["description"],
        min_similarity=0.6,
        embedder=embedder,
    )
    assert all(hit.vector_rank is None for hit in below), "0.59 < 0.6 must be dropped"

    unfiltered = await hybrid_search(
        session,
        "truy vấn cosine thấp",
        product_ids=[target.id],
        source_types=["description"],
        min_similarity=0,
        embedder=embedder,
    )
    assert unfiltered, "min_similarity=0 disables the filter"
    assert unfiltered[0].similarity == pytest.approx(0.59, abs=1e-6)


async def test_hybrid_search_defaults_to_the_configured_threshold(
    session: AsyncSession, session_factory: Any, monkeypatch: Any
) -> None:
    import app.ingest.search as search_module

    catalog = await seed_catalog(session)
    embedder = catalog["embedder"]
    target = catalog["products"][0]
    chunk_vector, query_vector = _normalized_pair(0.5)
    embedder.mapping["ngưỡng mặc định"] = query_vector
    await session.execute(
        text(
            "UPDATE chunks SET embedding = :vec "
            "WHERE product_id = :pid AND source_type = 'description'"
        ),
        {"vec": str(chunk_vector), "pid": target.id},
    )
    await session.commit()

    high = await hybrid_search(
        session,
        "ngưỡng mặc định",
        product_ids=[target.id],
        source_types=["description"],
        embedder=embedder,
    )
    assert all(hit.vector_rank is None for hit in high), "default 0.6 must drop a 0.5 hit"

    low_settings = make_settings(SEARCH_MIN_COSINE_SIMILARITY=0.4)
    monkeypatch.setattr(search_module, "get_settings", lambda: low_settings)
    permissive = await hybrid_search(
        session,
        "ngưỡng mặc định",
        product_ids=[target.id],
        source_types=["description"],
        embedder=embedder,
    )
    assert permissive and permissive[0].similarity == pytest.approx(0.5, abs=1e-6)


async def test_hybrid_search_without_embedder_is_full_text_only(
    session: AsyncSession, session_factory: Any
) -> None:
    catalog = await seed_catalog(session)

    hits = await hybrid_search(
        session, "gạo st25", product_ids=[catalog["products"][0].id], embedder=NullEmbedder()
    )

    assert hits
    assert all(hit.vector_rank is None for hit in hits)
    assert all(hit.similarity is None for hit in hits)


# ---------------------------------------------------------------------------
# F4 — a source with no products fails with ``no_products``
# ---------------------------------------------------------------------------


async def test_ingest_source_without_products_fails_explicitly(
    session: AsyncSession, session_factory: Any
) -> None:
    shop = Shop(
        id=uuid.uuid4(),
        platform="tiki",
        external_id="seller-empty",
        name="Shop rỗng",
        url="https://tiki.vn/cua-hang/empty",
        raw={},
    )
    source = Source(
        id=uuid.uuid4(),
        url=shop.url,
        url_type="shop",
        platform="tiki",
        status="done",
        stats={},
        external_ref={"shop_id": str(shop.id)},
        crawled_at=datetime.now(UTC),
    )
    session.add_all([shop, source])
    await session.commit()

    result = await ingest_source(
        source.id,
        session_factory=session_factory,
        embedder=FakeEmbedder(),
        llm_client=None,
        settings=make_settings(),
    )

    assert result.status == "failed"
    assert result.error == "no_products"
    await session.refresh(source)
    assert source.status == "failed"
    assert source.error == "no_products"


# ---------------------------------------------------------------------------
# G1/G2 with real Redis (Lua CAS) and real Postgres
# ---------------------------------------------------------------------------


async def test_lua_cas_keeps_terminal_status(redis_client: Any) -> None:
    events = JobEvents(redis_client)
    await events.create_job("job-lua", url="https://tiki.vn/x")
    assert await events.set_status("job-lua", "done") is True

    assert await events.set_status("job-lua", "running") is False
    status = await events.get_status("job-lua")
    assert status is not None and status["status"] == "done"

    transitioned, latest = await events.mark_stale("job-lua")
    assert transitioned is False
    assert latest is not None and latest["status"] == "done"


async def test_lua_cas_heartbeat_rejects_terminal_job(redis_client: Any) -> None:
    from app.core.errors import JobCancelledError

    events = JobEvents(redis_client)
    await events.create_job("job-hb", source_id=str(uuid.uuid4()))
    await events.set_status("job-hb", "failed", error="worker_timeout: gone")

    with pytest.raises(JobCancelledError):
        await events.touch_heartbeat("job-hb")


async def test_mark_stale_flips_the_source_row_to_failed(
    session: AsyncSession, session_factory: Any, redis_client: Any
) -> None:
    catalog = await seed_catalog(session)
    source = catalog["source"]
    events = JobEvents(redis_client, session_factory=session_factory)
    await events.create_job("job-src", source_id=str(source.id))

    transitioned, latest = await events.mark_stale("job-src", error="worker_timeout: no heartbeat")

    assert transitioned is True
    assert latest is not None and latest["status"] == "failed"
    await session.refresh(source)
    assert source.status == "failed"
    assert source.error == "worker_timeout: no heartbeat"


async def test_mark_stale_is_idempotent_under_the_lua_script(redis_client: Any) -> None:
    events = JobEvents(redis_client)
    await events.create_job("job-twice")

    first, _ = await events.mark_stale("job-twice")
    second, latest = await events.mark_stale("job-twice")

    assert first is True and second is False
    assert latest is not None and latest["status"] == "failed"


# ---------------------------------------------------------------------------
# Chat API persistence against the real schema
# ---------------------------------------------------------------------------


@pytest.fixture
async def chat_client(
    session: AsyncSession,
    session_factory: Any,
    secret_box: SecretBox,
    monkeypatch: Any,
) -> AsyncIterator[tuple[httpx.AsyncClient, dict[str, Any]]]:
    behavior = MockBehavior()
    behavior.tool_call_script = [
        [{"name": "query_products", "args": {"sort_by": "sold_count", "limit": 2}}],
        [],
    ]
    behavior.scripted_texts = ["Top 2 gồm Gạo ST24 5kg và Gạo ST25 5kg [P1][P2]."]
    mock_app = build_mock_app(behavior)
    http_clients: list[httpx.AsyncClient] = []

    def _factory() -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=mock_app), base_url="http://mock"
        )
        http_clients.append(client)
        return client

    registry = LLMClientRegistry()
    registry.set_http_client_factory(_factory)

    catalog = await seed_catalog(session)
    profile = LLMProfile(
        id=uuid.uuid4(),
        user_id="local",
        name=f"p4-chat-{uuid.uuid4().hex[:6]}",
        base_url="http://mock/v1",
        api_key_enc=secret_box.encrypt("sk-integration-chat"),
        chat_model="mock-chat",
        small_model=None,
        embedding_model=None,
        max_context_tokens=32000,
        capabilities={"chat": True, "tool_calling": True, "streaming": False},
    )
    session.add(profile)
    await session.commit()
    behavior.expected_api_key = "sk-integration-chat"

    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_registry] = lambda: registry
    app.dependency_overrides[get_secret_box] = lambda: secret_box
    app.dependency_overrides[get_settings] = lambda: make_settings()
    app.dependency_overrides[get_job_events] = lambda: RecordingJobEvents()
    app.dependency_overrides[get_job_queue] = lambda: RecordingJobQueue()
    app.dependency_overrides[get_url_resolver] = lambda: None
    app.dependency_overrides[get_embedder_override] = lambda: catalog["embedder"]

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=15.0
    )
    try:
        yield client, {"catalog": catalog, "profile": profile, "behavior": behavior}
    finally:
        await client.aclose()
        await registry.aclose_all()
        for http_client in http_clients:
            if not http_client.is_closed:
                await http_client.aclose()


async def test_chat_end_to_end_persists_messages_and_citations(
    chat_client: Any, session: AsyncSession
) -> None:
    client, info = chat_client
    catalog = info["catalog"]
    profile = info["profile"]
    source = catalog["source"]

    response = await client.post(
        "/api/chat",
        json={
            "message": "Top 2 sản phẩm bán chạy?",
            "source_id": str(source.id),
            "llm_profile_id": str(profile.id),
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    body = response.text
    for event in ("session", "token", "citations", "done"):
        assert f"event: {event}" in body
    assert "sk-integration-chat" not in body

    sessions = list((await session.execute(select(ChatSession))).scalars().all())
    assert len(sessions) == 1
    stored = sessions[0]
    assert stored.source_id == source.id
    assert stored.llm_profile_id == profile.id
    assert stored.title == "Top 2 sản phẩm bán chạy?"

    messages = list(
        (
            await session.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == stored.id)
                .order_by(ChatMessage.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    assert [m.role for m in messages] == ["user", "assistant"]
    assistant = messages[1]
    assert assistant.citations[0]["ref"] in {"P1", "P2"}
    assert assistant.usage and assistant.usage["total_tokens"] > 0
    assert assistant.error is None

    detail = await client.get(f"/api/chat/sessions/{stored.id}")
    assert detail.status_code == 200
    assert len(detail.json()["messages"]) == 2

    history = await client.get("/api/chat/sessions", params={"source_id": str(source.id)})
    assert [row["id"] for row in history.json()] == [str(stored.id)]

    delete = await client.delete(f"/api/chat/sessions/{stored.id}")
    assert delete.status_code == 204
    remaining = list((await session.execute(select(ChatMessage))).scalars().all())
    assert remaining == [], "messages must cascade with the session"


async def test_chat_rejects_source_without_ingest_stats(
    chat_client: Any, session: AsyncSession
) -> None:
    client, info = chat_client
    catalog = info["catalog"]
    profile = info["profile"]
    pending = Source(
        id=uuid.uuid4(),
        url="https://tiki.vn/cua-hang/pending",
        url_type="shop",
        platform="tiki",
        status="running",
        stats={},
        external_ref={"shop_id": str(catalog["shop"].id)},
    )
    session.add(pending)
    await session.commit()

    response = await client.post(
        "/api/chat",
        json={
            "message": "hi",
            "source_id": str(pending.id),
            "llm_profile_id": str(profile.id),
        },
    )

    assert response.status_code == 409
    assert response.json()["error"] == "source_not_ready"


async def test_migration_0004_creates_chat_tables(session: AsyncSession) -> None:
    revision = (await session.execute(text("SELECT version_num FROM alembic_version"))).scalar()
    assert revision == "0004_chat"

    tables = set(
        (
            await session.execute(
                text(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
                )
            )
        )
        .scalars()
        .all()
    )
    assert {"chat_sessions", "chat_messages", "review_summaries"} <= tables

    indexes = set(
        (
            await session.execute(
                text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
            )
        )
        .scalars()
        .all()
    )
    assert any("chat_messages" in name and "session" in name for name in indexes)
    assert any("chat_sessions" in name and "source" in name for name in indexes)

    columns = set(
        (
            await session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'chat_messages'"
                )
            )
        )
        .scalars()
        .all()
    )
    assert {"id", "session_id", "role", "content", "tool_calls", "citations", "usage", "error"} <= (
        columns
    )
