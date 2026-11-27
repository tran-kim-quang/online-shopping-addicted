"""Phase 6 integration tests: dedicated Postgres (``shopqa_phase6_test``) + Redis DB 15.

Covers the SQL-only half of Phần B:
* migration ``0005_data_providers`` up/down (and the CHECK constraint 0.05–5.00),
* the stratified report contract (C2 ``sold_total`` null, empty distribution, null coverage,
  warnings, platform tags) while Tiki stays unchanged,
* a full Shopee ingest from the sanitized Apify fixtures (products/reviews/chunks/sampling),
* agent SQL that prefers ``quality_score`` and reports ``review_sample``.

Never touches ``shopqa``, Redis DB 0, or the running containers.
"""

from __future__ import annotations

import asyncio
import csv
import io
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.agent.citations import CitationRegistry
from app.agent.tools import (
    AgentContext,
    QueryProductsArgs,
    SearchReviewsArgs,
    SummarizeReviewsArgs,
    _run_get_product,
    _run_query_products,
    _run_search_reviews,
    _run_summarize_reviews,
)
from app.crawl.service import crawl_url
from app.db.models import Chunk, DataProviderProfile, Product, Review, Shop, Source
from app.ingest.embedding import NullEmbedder
from app.ingest.pipeline import ingest_source
from app.reports.csv_export import iter_reviews_csv
from app.reports.source_report import build_source_report
from app.security.secrets import SecretBox
from tests.ingest_fakes import FakeEmbedder
from tests.shopee_fakes import ScriptedApify, detail_items, low_star_items, make_run, shop_items

pytestmark = pytest.mark.integration

BACKEND_DIR = Path(__file__).resolve().parents[1]
SECRET = "vN5y6rM0J9h2xL8qK3pW1zB4cF7dG0tH2sV5mN8wX4Q="

PG_HOST = os.environ.get("TEST_PHASE6_PG_HOST", "localhost")
PG_PORT = int(os.environ.get("TEST_PHASE6_PG_PORT", "5433"))
PG_USER = os.environ.get("TEST_PHASE6_PG_USER", "shopqa")
PG_PASSWORD = os.environ.get("TEST_PHASE6_PG_PASSWORD", "shopqa")
PG_MAINTENANCE_DB = os.environ.get("TEST_PHASE6_PG_MAINTENANCE_DB", "shopqa")
TEST_DB_NAME = os.environ.get("TEST_PHASE6_DB_NAME", "shopqa_phase6_test")
TEST_REDIS_DB = int(os.environ.get("TEST_PHASE6_REDIS_DB", "15"))
PROJECT_DB_NAME = "shopqa"

MAINTENANCE_DSN = f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{PG_MAINTENANCE_DB}"
TEST_DATABASE_URL = (
    f"postgresql+asyncpg://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{TEST_DB_NAME}"
)
TEST_REDIS_URL = f"redis://{PG_HOST}:6380/{TEST_REDIS_DB}"
NOW = datetime(2026, 11, 27, 3, 0, tzinfo=UTC)
SHOPEE_PRODUCT_URL = "https://shopee.vn/product/1937969974/48571216193"


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


def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DATABASE_URL": TEST_DATABASE_URL}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="session")
def phase6_database_url() -> str:
    assert TEST_DB_NAME != PROJECT_DB_NAME, "integration tests must not target the project DB"
    try:
        asyncio.run(_ensure_test_database())
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable at {MAINTENANCE_DSN}: {exc}")

    result = _alembic("upgrade", "head")
    assert result.returncode == 0, f"alembic failed:\n{result.stdout}\n{result.stderr}"
    return TEST_DATABASE_URL


@pytest.fixture
async def session_factory(phase6_database_url: str) -> AsyncIterator[Any]:
    engine = create_async_engine(phase6_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            current = (await conn.execute(text("SELECT current_database()"))).scalar_one()
            assert current == TEST_DB_NAME, f"refusing to truncate {current}"
            await conn.execute(
                text(
                    "TRUNCATE chunks, review_summaries, reviews, products, shops, sources, "
                    "data_provider_profiles CASCADE"
                )
            )
        yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
async def session(session_factory: Any) -> AsyncIterator[AsyncSession]:
    async with session_factory() as active:
        yield active


# ---------------------------------------------------------------------------
# Seeds
# ---------------------------------------------------------------------------


STRATIFIED_SAMPLING: dict[str, Any] = {
    "provider": "apify",
    "strategy": "stratified",
    "window_months": 12,
    "groups": [
        {
            "name": "low",
            "stars": [1, 2, 3],
            "with_media_only": False,
            "max": 150,
            "delivered": 65,
            "delivered_new": 65,
            "coverage": "shopee_limit",
        },
        {
            "name": "high",
            "stars": [4, 5],
            "with_media_only": True,
            "max": 30,
            "delivered": 15,
            "delivered_new": 15,
            "coverage": "budget",
        },
    ],
    "cost_usd": 0.25017,
}


async def seed_stratified_source(session: AsyncSession) -> dict[str, Any]:
    shop = Shop(
        id=uuid.uuid4(),
        platform="shopee",
        external_id="1937969974",
        name="Mavis _ Tech _ Shop",
        url="https://shopee.vn/shop/1937969974",
        rating=4.81,
        raw={"isOfficialShop": True},
    )
    session.add(shop)
    await session.flush()

    p1 = Product(
        id=uuid.uuid4(),
        platform="shopee",
        external_id="48571216193",
        title="Tai Nghe JBL Tune 230NC",
        url=SHOPEE_PRODUCT_URL,
        price=Decimal("298000"),
        currency="VND",
        sold_count=None,
        rating_avg=4.87,
        rating_count=54,
        review_count=None,
        shop_id=shop.id,
        raw={},
    )
    p2 = Product(
        id=uuid.uuid4(),
        platform="shopee",
        external_id="27551742889",
        title="Biore UV Smooth Velvet",
        url="https://shopee.vn/product/158660528/27551742889",
        price=Decimal("195000"),
        currency="VND",
        sold_count=None,
        rating_avg=None,
        rating_count=None,
        review_count=None,
        shop_id=shop.id,
        raw={},
    )
    session.add_all([p1, p2])
    await session.flush()

    source = Source(
        id=uuid.uuid4(),
        url="https://shopee.vn/shop/1937969974",
        url_type="shop",
        platform="shopee",
        status="done",
        external_ref={"shop_id": str(shop.id), "product_ids": [str(p1.id), str(p2.id)]},
        stats={"sampling": STRATIFIED_SAMPLING, "reviews_count": 5},
        crawled_at=NOW - timedelta(hours=1),
        created_at=NOW - timedelta(hours=2),
        updated_at=NOW - timedelta(hours=1),
    )
    session.add(source)

    reviews = [
        Review(
            id=uuid.uuid4(),
            product_id=p1.id,
            external_id="r1",
            rating=1,
            content="Hàng lỗi, pin tụt nhanh, rất thất vọng",
            sentiment="negative",
            sentiment_source="llm",
            tags=["chất lượng"],
            sub_ratings={"product_quality": 1},
            variant="Màu Đen",
            media_count=2,
            quality_score=0.8,
            helpful_count=3,
        ),
        Review(
            id=uuid.uuid4(),
            product_id=p1.id,
            external_id="r2",
            rating=2,
            content="Tạm được nhưng giao chậm",
            sentiment="negative",
            sentiment_source="llm",
            tags=["giao hàng"],
            quality_score=0.4,
        ),
        Review(
            id=uuid.uuid4(),
            product_id=p1.id,
            external_id="r3",
            rating=3,
            content="Bình thường",
            sentiment="neutral",
            sentiment_source="llm",
            tags=["chất lượng"],
            quality_score=0.2,
        ),
        Review(
            id=uuid.uuid4(),
            product_id=p1.id,
            external_id="r4",
            rating=5,
            content="Rất hay, âm bass căng, pin trâu, đóng gói kỹ, giao nhanh",
            sentiment="positive",
            sentiment_source="llm",
            tags=["chất lượng", "giao hàng"],
            quality_score=0.9,
            media_count=1,
        ),
        Review(
            id=uuid.uuid4(),
            product_id=p2.id,
            external_id="r5",
            rating=5,
            content="Kem ổn, thấm nhanh",
            sentiment="positive",
            sentiment_source="llm",
            tags=["chất lượng"],
            quality_score=0.5,
        ),
    ]
    session.add_all(reviews)
    await session.commit()
    return {"source": source, "shop": shop, "products": [p1, p2], "reviews": reviews}


async def seed_natural_source(session: AsyncSession) -> dict[str, Any]:
    source = Source(
        id=uuid.uuid4(),
        url="https://tiki.vn/cua-hang/nha-sach-fahasa",
        url_type="shop",
        platform="tiki",
        status="done",
        external_ref={},
        stats={"sampling": {"provider": None, "strategy": "natural"}},
        crawled_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    session.add(source)
    product = Product(
        id=uuid.uuid4(),
        platform="tiki",
        external_id="2001",
        title="Combo HSK 1",
        url="https://tiki.vn/combo-hsk-1-p2001.html",
        price=Decimal("191000"),
        currency="VND",
        sold_count=None,
        rating_avg=4.8,
        rating_count=40,
        review_count=60,
        raw={},
    )
    session.add(product)
    await session.flush()
    source.external_ref = {"product_ids": [str(product.id)]}
    session.add_all(
        [
            Review(
                id=uuid.uuid4(),
                product_id=product.id,
                external_id=f"t{i}",
                rating=rating,
                content="Nội dung đủ dài để không bị coi là review rỗng trong bài kiểm thử.",
                sentiment="positive" if rating >= 4 else "negative",
                sentiment_source="rating",
                quality_score=0.3,
            )
            for i, rating in enumerate([5, 4, 3, 2, 1], start=1)
        ]
    )
    await session.commit()
    return {"source": source, "product": product}


# ---------------------------------------------------------------------------
# Migration 0005
# ---------------------------------------------------------------------------


async def test_migration_head_is_0006_and_keeps_the_phase6_columns(session: AsyncSession) -> None:
    revision = (await session.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
    # 0006 adds ``products.rating_breakdown``; 0005's columns must still be there.
    assert revision == "0006_product_rating_breakdown"

    review_columns = {
        row[0]
        for row in (
            await session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'reviews'"
                )
            )
        ).all()
    }
    assert {"variant", "tags", "sub_ratings", "seller_reply", "media_count", "quality_score"} <= (
        review_columns
    )

    provider_columns = {
        row[0]
        for row in (
            await session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'data_provider_profiles'"
                )
            )
        ).all()
    }
    assert {
        "id",
        "user_id",
        "provider",
        "name",
        "api_token_enc",
        "max_cost_usd_per_link",
        "account_info",
        "created_at",
        "updated_at",
    } == provider_columns


async def test_provider_ceiling_check_constraint_is_enforced(session: AsyncSession) -> None:
    from sqlalchemy.exc import IntegrityError

    profile = DataProviderProfile(
        id=uuid.uuid4(),
        user_id="local",
        provider="apify",
        name="too cheap",
        api_token_enc="x",
        max_cost_usd_per_link=0.01,
    )
    session.add(profile)
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()

    ok = DataProviderProfile(
        id=uuid.uuid4(),
        user_id="local",
        provider="apify",
        name="ok",
        api_token_enc="x",
        max_cost_usd_per_link=0.05,
    )
    session.add(ok)
    await session.commit()
    assert (await session.get(DataProviderProfile, ok.id)) is not None


def test_migration_0005_downgrades_and_upgrades_again(phase6_database_url: str) -> None:
    """Run downgrade/upgrade with alembic's own connection (never inside the test session)."""
    assert phase6_database_url == TEST_DATABASE_URL
    down = _alembic("downgrade", "0004_chat")
    assert down.returncode == 0, f"downgrade failed:\n{down.stdout}\n{down.stderr}"

    async def _columns() -> set[str]:
        conn = await asyncpg.connect(
            f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{TEST_DB_NAME}"
        )
        try:
            rows = await conn.fetch(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'reviews'"
            )
            tables = await conn.fetch(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_name = 'data_provider_profiles'"
            )
            return {r["column_name"] for r in rows} | {f"table:{r['table_name']}" for r in tables}
        finally:
            await conn.close()

    after_down = asyncio.run(_columns())
    assert "variant" not in after_down
    assert "quality_score" not in after_down
    assert "table:data_provider_profiles" not in after_down

    up = _alembic("upgrade", "head")
    assert up.returncode == 0, f"upgrade failed:\n{up.stdout}\n{up.stderr}"
    after_up = asyncio.run(_columns())
    assert {"variant", "quality_score"} <= after_up
    assert "table:data_provider_profiles" in after_up


# ---------------------------------------------------------------------------
# Report: stratified contract (C2/C3 backend, A7)
# ---------------------------------------------------------------------------


async def test_stratified_report_nulls_derived_metrics(session: AsyncSession) -> None:
    seeded = await seed_stratified_source(session)

    report = await build_source_report(session, seeded["source"], now=NOW)

    # C2: no product reports sold -> totals.sold_total must be null on every platform.
    assert report.totals.sold_total is None
    # Stratified samples never produce a star distribution or a coverage/positive ratio.
    assert report.rating_distribution == []
    assert report.totals.coverage_pct is None
    assert all(p.coverage_pct is None for p in report.products)
    assert all(p.positive_pct is None for p in report.products)
    # Rating comes only from what the platform reported (4.87 × 54), never from reviews.
    assert report.totals.rating_avg_weighted == pytest.approx(4.87, abs=1e-6)
    assert report.totals.review_count_reported == 0
    assert report.totals.reviews_collected == 5
    assert report.source.platform_display_name == "Shopee"
    assert report.source.sampling == STRATIFIED_SAMPLING

    codes = [w.code for w in report.warnings]
    assert "stratified_sample" in codes
    assert "shopee_limit" in codes
    assert "review_budget_reached" in codes
    assert "missing_rating" in codes
    # The natural-sample warnings must not appear for stratified sources.
    assert "partial_reviews" not in codes
    assert "reported_count_lower" not in codes

    stratified = next(w for w in report.warnings if w.code == "stratified_sample")
    assert stratified.params == {"low": 150, "high": 30, "window_months": 12}
    assert "12" in stratified.message
    missing = next(w for w in report.warnings if w.code == "missing_rating")
    assert missing.product_ids == [seeded["products"][1].id]
    assert "Shopee không công khai" in missing.message


async def test_stratified_report_exposes_platform_tags(session: AsyncSession) -> None:
    seeded = await seed_stratified_source(session)

    report = await build_source_report(session, seeded["source"], now=NOW)

    tags = {tag.tag: tag.count for tag in report.aspects.platform_tags}
    assert tags == {"chất lượng": 4, "giao hàng": 2}
    # LLM aspects stay separate from the platform tags.
    assert isinstance(report.aspects.top_positive, list)
    assert isinstance(report.aspects.top_negative, list)


async def test_natural_report_keeps_the_old_shape(session: AsyncSession) -> None:
    seeded = await seed_natural_source(session)

    report = await build_source_report(session, seeded["source"], now=NOW)

    assert [b.stars for b in report.rating_distribution] == [5, 4, 3, 2, 1]
    assert report.totals.coverage_pct == pytest.approx(round(5 / 60 * 100, 1), abs=1e-6)
    # C2 applies to every platform: no sold_count -> null (not 0).
    assert report.totals.sold_total is None
    assert report.products[0].positive_pct is not None
    codes = [w.code for w in report.warnings]
    assert "stratified_sample" not in codes

    product = report.products[0]
    assert product.rating_avg == 4.8
    assert product.review_count_reported == 60


async def test_missing_rating_warning_is_platform_specific(session: AsyncSession) -> None:
    seeded = await seed_natural_source(session)
    seeded["product"].rating_avg = None
    await session.commit()

    report = await build_source_report(session, seeded["source"], now=NOW)

    missing = next(w for w in report.warnings if w.code == "missing_rating")
    assert "Tiki" in missing.message
    assert "Shopee" not in missing.message


async def test_reviews_csv_contains_the_new_shopee_columns(session: AsyncSession) -> None:
    seeded = await seed_stratified_source(session)

    chunks = [
        chunk async for chunk in iter_reviews_csv(session, [seeded["products"][0].id], batch_size=2)
    ]
    payload = b"".join(chunks)
    text_ = payload.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text_), strict=True))
    header = rows[0]

    assert header[:5] == [
        "product_external_id",
        "product_title",
        "review_external_id",
        "rating",
        "sentiment",
    ]
    for column in ("variant", "tags", "sub_ratings", "seller_reply", "media_count"):
        assert column in header

    by_id = {row[header.index("review_external_id")]: row for row in rows[1:]}
    r1 = by_id["r1"]
    assert r1[header.index("variant")] == "Màu Đen"
    assert "chất lượng" in r1[header.index("tags")]
    assert "product_quality" in r1[header.index("sub_ratings")]
    assert r1[header.index("media_count")] == "2"


# ---------------------------------------------------------------------------
# Shopee ingest end-to-end from the real fixtures
# ---------------------------------------------------------------------------


async def test_shopee_ingest_persists_products_reviews_chunks_and_sampling(
    session: AsyncSession, session_factory: Any
) -> None:
    product_rows = [row for row in detail_items() if row["itemId"] == "48571216193"]
    low_rows = [
        *low_star_items(),  # stars 1,2,3 + product_summary + summary + info
        {
            "review_id": "with-variant",
            "rating": 2,
            "comment": "Đóng gói hộp bị móp, tai nghe vẫn ổn định sau hai tuần dùng thử",
            "variant": "Màu Xanh",
            "review_tags": ["đóng gói"],
            "product_id": "48571216193",
            "shop_id": "1937969974",
        },
    ]
    high_rows = [row for row in low_star_items() if row.get("_type") is None][:2]

    client = ScriptedApify(
        product_runs=[
            make_run(product_rows, cost_usd=0.002),
            make_run(shop_items(), cost_usd=0.002),  # rating fallback card
        ],
        review_runs=[
            make_run(low_rows, cost_usd=0.0359),
            make_run(high_rows, cost_usd=0.00718),
        ],
    )

    result = await crawl_url(
        SHOPEE_PRODUCT_URL,
        session_factory=session_factory,
        apify_client=client,
        max_reviews=200,
    )

    assert result.status == "done", result.error
    assert result.product_ids

    ingested = await ingest_source(
        result.source_id,
        session_factory=session_factory,
        embedder=FakeEmbedder(dim=1024),
        settings=None,
    )
    assert ingested.stats.chunks_created > 0

    async with session_factory() as fresh:
        source = await fresh.get(Source, result.source_id)
        assert source is not None
        assert source.platform == "shopee"
        assert source.status == "done"

        sampling = source.stats["sampling"]
        assert sampling["provider"] == "apify"
        assert sampling["strategy"] == "stratified"
        assert sampling["window_months"] == 12
        assert sampling["cost_usd"] == pytest.approx(0.002 + 0.002 + 0.0359 + 0.00718)
        low = sampling["groups"][0]
        high = sampling["groups"][1]
        # The real stars-1/2/3 fixture reports `stopped_at_max` at maxReviewsPerProduct=10.
        assert low["coverage"] == "stopped_at_max"
        assert low["delivered"] >= 10
        assert low["delivered_new"] == low["delivered"]
        assert high["coverage"] == "complete"  # no product_summary row in the high run

        product = (
            await fresh.execute(select(Product).where(Product.external_id == "48571216193"))
        ).scalar_one()
        # Rating only from the shop card fallback; sold is never estimated.
        assert product.rating_avg == 4.9
        assert product.rating_count is None
        assert product.sold_count is None
        assert product.review_count is None
        assert product.brand == "JBL"

        reviews = list(
            (await fresh.execute(select(Review).where(Review.product_id == product.id)))
            .scalars()
            .all()
        )
        assert len(reviews) == 11
        anon = next(r for r in reviews if r.external_id == "119214882021")
        assert anon.author is None
        assert anon.variant is None
        assert anon.media_count == 1
        assert anon.sub_ratings["product_quality"] == 3
        assert all(r.quality_score is not None for r in reviews)
        assert all(r.quality_score >= 0.0 for r in reviews)

        chunks = list(
            (await fresh.execute(select(Chunk).where(Chunk.product_id == product.id)))
            .scalars()
            .all()
        )
        assert chunks
        assert any(chunk.source_type == "review" for chunk in chunks)
        review_text = " ".join(chunk.text for chunk in chunks if chunk.source_type == "review")
        # A6: variant + tags are part of the review chunk text so they are searchable.
        assert "Màu Xanh" in review_text
        assert "đóng gói" in review_text


async def test_recollection_counts_stored_reviews_and_only_new_rows(
    session: AsyncSession, session_factory: Any
) -> None:
    """B5: `delivered` = rows in the DB after upsert, `delivered_new` = this collection only."""
    product_rows = [row for row in detail_items() if row["itemId"] == "48571216193"]
    new_rows = [row for row in low_star_items() if row.get("_type") is None][:3]

    # A previous crawl already stored six low-star reviews for this product.
    old_product = Product(
        id=uuid.uuid4(),
        platform="shopee",
        external_id="48571216193",
        title="Tai Nghe JBL Tune 230NC",
        url=SHOPEE_PRODUCT_URL,
        price=Decimal("298000"),
        currency="VND",
        raw={},
    )
    session.add(old_product)
    await session.flush()
    session.add_all(
        [
            Review(
                id=uuid.uuid4(),
                product_id=old_product.id,
                external_id=f"old-{i}",
                rating=(i % 3) + 1,
                content="Đánh giá cũ đã có trong DB từ lần thu thập trước.",
                sentiment="negative",
                sentiment_source="rating",
            )
            for i in range(6)
        ]
    )
    await session.commit()

    client = ScriptedApify(
        product_runs=[
            make_run(product_rows, cost_usd=0.002),
            make_run(shop_items(), cost_usd=0.002),
        ],
        review_runs=[
            make_run(new_rows, cost_usd=0.012),
            make_run([], cost_usd=0.0),
        ],
    )

    result = await crawl_url(
        SHOPEE_PRODUCT_URL,
        session_factory=session_factory,
        apify_client=client,
        data_provider_profile_id=None,
    )
    assert result.status == "done", result.error

    async with session_factory() as fresh:
        source = await fresh.get(Source, result.source_id)
        assert source is not None
        sampling = source.stats["sampling"]
        low = sampling["groups"][0]
        assert low["delivered_new"] == 3
        assert low["delivered"] == 9, "six stored reviews plus the three new ones"
        assert sampling["cost_usd"] == pytest.approx(0.002 + 0.002 + 0.012)

        stored = (
            (await fresh.execute(select(Review).where(Review.product_id == old_product.id)))
            .scalars()
            .all()
        )
        assert len(stored) == 9, "reviews are upserted, never duplicated"


# ---------------------------------------------------------------------------
# Agent SQL: review_sample + quality_score ordering
# ---------------------------------------------------------------------------


def _agent_ctx(session: AsyncSession, seeded: dict[str, Any], source: Source) -> AgentContext:
    class _SessionFactory:
        def __call__(self) -> Any:
            class _Ctx:
                async def __aenter__(self) -> AsyncSession:
                    return session

                async def __aexit__(self, *_exc: Any) -> None:
                    return None

            return _Ctx()

    return AgentContext(
        session_factory=_SessionFactory(),
        source=source,
        shop=seeded.get("shop"),
        products=list(seeded["products"]) if "products" in seeded else [seeded["product"]],
        embedder=NullEmbedder(),
        llm_client=None,  # type: ignore[arg-type]  (skips the optional LLM summary)
        citations=CitationRegistry(),
        settings=_settings(),
    )


def _settings() -> Any:
    from tests.agent_fakes import make_settings

    return make_settings()


async def test_get_product_and_query_products_flag_the_sample_type(session: AsyncSession) -> None:
    seeded = await seed_stratified_source(session)
    ctx = _agent_ctx(session, seeded, seeded["source"])

    from app.agent.tools import GetProductArgs

    single = await _run_get_product(
        ctx, GetProductArgs(product_id=seeded["products"][0].external_id)
    )
    listing = await _run_query_products(ctx, QueryProductsArgs(limit=10))

    assert single.ok is True
    assert single.data["rating_avg"] == 4.87
    assert single.data["rating_count"] == 54
    assert single.data["review_sample"] == "stratified"
    assert "review_avg_rating" not in single.data
    assert "average_rating" not in single.data

    assert listing.ok is True
    assert listing.data["review_sample"] == "stratified"
    assert all(item["review_sample"] == "stratified" for item in listing.data["items"])


async def test_search_reviews_exposes_quality_fields_and_prefers_quality(
    session: AsyncSession,
) -> None:
    seeded = await seed_stratified_source(session)
    ctx = _agent_ctx(session, seeded, seeded["source"])

    result = await _run_search_reviews(
        ctx, SearchReviewsArgs(product_id=seeded["products"][0].external_id, limit=10)
    )

    assert result.ok is True
    items = result.data["items"]
    assert len(items) == 4
    assert [item["quality_score"] for item in items] == sorted(
        (item["quality_score"] for item in items), reverse=True
    )
    first = items[0]
    assert first["quality_score"] == 0.9  # r4: highest quality score
    assert first["media_count"] if "media_count" in first else True
    by_rating = {item["rating"]: item for item in items}
    assert by_rating[1]["variant"] == "Màu Đen"
    assert by_rating[1]["tags"] == ["chất lượng"]
    assert by_rating[1]["sub_ratings"] == {"product_quality": 1}
    assert by_rating[1]["quality_score"] == 0.8


async def test_summarize_reviews_returns_platform_rating_only(session: AsyncSession) -> None:
    seeded = await seed_stratified_source(session)
    ctx = _agent_ctx(session, seeded, seeded["source"])

    result = await _run_summarize_reviews(
        ctx, SummarizeReviewsArgs(product_id=seeded["products"][0].external_id)
    )

    assert result.ok is True
    assert result.data["rating_avg"] == 4.87
    assert result.data["rating_count"] == 54
    assert result.data["review_sample"] == "stratified"
    assert result.data["star_distribution"] is None
    assert result.data["total_reviews_collected"] == 4


async def test_natural_source_keeps_star_distribution(session: AsyncSession) -> None:
    seeded = await seed_natural_source(session)
    ctx = _agent_ctx(session, seeded, seeded["source"])

    result = await _run_summarize_reviews(
        ctx, SummarizeReviewsArgs(product_id=seeded["product"].external_id)
    )

    assert result.data["review_sample"] == "natural"
    assert result.data["star_distribution"] == {"1": 1, "2": 1, "3": 1, "4": 1, "5": 1}


# ---------------------------------------------------------------------------
# Provider profile decryption path (profile -> fetcher -> ApifyClient)
# ---------------------------------------------------------------------------


async def test_profile_token_is_decrypted_and_its_ceiling_used(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.platforms.plugins.shopee as shopee_plugin_module

    captured: dict[str, Any] = {}

    class _FakeApify:
        def __init__(self, token: str, **kwargs: Any) -> None:
            captured["token"] = token

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(shopee_plugin_module, "ApifyClient", _FakeApify)

    box = SecretBox(SECRET)
    profile = DataProviderProfile(
        id=uuid.uuid4(),
        user_id="local",
        provider="apify",
        name="Apify",
        api_token_enc=box.encrypt("apify_api_PROFILE_TOKEN"),
        max_cost_usd_per_link=0.85,
    )
    session.add(profile)
    await session.commit()

    plugin = shopee_plugin_module.PLUGIN
    fetcher = plugin.create_fetcher(profile, secret_box=box)

    assert captured["token"] == "apify_api_PROFILE_TOKEN"
    assert fetcher._max_cost_usd == 0.85


def test_shopee_plugin_requires_a_profile_or_an_injected_client() -> None:
    from app.crawl.errors import ProviderError
    from app.platforms.plugins.shopee import PLUGIN

    with pytest.raises(ProviderError) as excinfo:
        PLUGIN.create_fetcher(None)

    assert excinfo.value.code == "data_provider_required"
    assert excinfo.value.status_code == 409


def test_shopee_plugin_rejects_a_profile_of_another_provider() -> None:
    from types import SimpleNamespace

    from app.crawl.errors import ProviderError
    from app.platforms.plugins.shopee import PLUGIN

    profile = SimpleNamespace(provider="serpapi", api_token_enc="x", max_cost_usd_per_link=0.3)

    with pytest.raises(ProviderError) as excinfo:
        PLUGIN.create_fetcher(profile)

    assert excinfo.value.code == "data_provider_mismatch"
