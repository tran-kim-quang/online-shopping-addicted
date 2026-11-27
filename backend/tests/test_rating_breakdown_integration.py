"""Rating breakdown integration: dedicated Postgres (``shopqa_phase6_rating_test``) + Redis 15.

Covers the SQL-only half of this fix:

* migration ``0006_product_rating_breakdown`` (head, column type, downgrade/upgrade),
* ``upsert_product`` never wiping a stored breakdown with ``NULL``,
* ``build_source_report`` switching to platform counts (complete / partial) while Tiki keeps
  ``rating_basis="reviews"``,
* a full ``crawl_url`` that persists the breakdown + ``sampling.runs`` and, on a second crawl,
  sends the stored ``until_by_group`` as a full ISO ``newerThan`` and merges without double
  counting.

Never touches ``shopqa``, Redis DB 0, or the running containers.
"""

from __future__ import annotations

import asyncio
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

from app.crawl.repository import upsert_product
from app.crawl.service import crawl_url
from app.crawl.types import ProductData
from app.db.models import Product, Shop, Source
from app.reports.source_report import build_source_report
from tests.shopee_fakes import ScriptedApify, make_run

pytestmark = pytest.mark.integration

BACKEND_DIR = Path(__file__).resolve().parents[1]

PG_HOST = os.environ.get("TEST_PHASE6_RATING_PG_HOST", "localhost")
PG_PORT = int(os.environ.get("TEST_PHASE6_RATING_PG_PORT", "5433"))
PG_USER = os.environ.get("TEST_PHASE6_RATING_PG_USER", "shopqa")
PG_PASSWORD = os.environ.get("TEST_PHASE6_RATING_PG_PASSWORD", "shopqa")
PG_MAINTENANCE_DB = os.environ.get("TEST_PHASE6_RATING_PG_MAINTENANCE_DB", "shopqa")
TEST_DB_NAME = os.environ.get("TEST_PHASE6_RATING_DB_NAME", "shopqa_phase6_rating_test")
TEST_REDIS_DB = int(os.environ.get("TEST_PHASE6_RATING_REDIS_DB", "15"))
PROJECT_DB_NAME = "shopqa"

MAINTENANCE_DSN = f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{PG_MAINTENANCE_DB}"
TEST_DATABASE_URL = (
    f"postgresql+asyncpg://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{TEST_DB_NAME}"
)
TEST_REDIS_URL = f"redis://{PG_HOST}:6380/{TEST_REDIS_DB}"
NOW = datetime(2026, 11, 27, 3, 0, tzinfo=UTC)
PRODUCT_ID = "48571216193"
SHOP_ID = "1937969974"
PRODUCT_URL = f"https://shopee.vn/product/{SHOP_ID}/{PRODUCT_ID}"
SINCE = "2025-11-27T05:13:58Z"


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
def rating_database_url() -> str:
    assert TEST_DB_NAME != PROJECT_DB_NAME, "integration tests must not target the project DB"
    try:
        asyncio.run(_ensure_test_database())
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable at {MAINTENANCE_DSN}: {exc}")

    result = _alembic("upgrade", "head")
    assert result.returncode == 0, f"alembic failed:\n{result.stdout}\n{result.stderr}"
    return TEST_DATABASE_URL


async def _flush_test_redis() -> int:
    """Flush Redis DB 15 inside one fresh loop and report how many keys it held."""
    import redis.asyncio as aioredis

    client = aioredis.from_url(TEST_REDIS_URL, decode_responses=True)
    try:
        await client.flushdb()
        return int(await client.dbsize())
    finally:
        await client.aclose()


@pytest.fixture(scope="session", autouse=True)
def redis_isolation() -> Any:
    """Redis DB 15 is reserved for tests: flush before and after, never touch DB 0."""
    try:
        asyncio.run(_flush_test_redis())
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Redis not reachable at {TEST_REDIS_URL}: {exc}")
    yield
    asyncio.run(_flush_test_redis())


@pytest.fixture
async def session_factory(rating_database_url: str) -> AsyncIterator[Any]:
    engine = create_async_engine(rating_database_url, poolclass=NullPool)
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


def full_stars(
    counts: dict[int, int], *, incomplete: frozenset[int] = frozenset()
) -> dict[str, Any]:
    return {
        str(star): {"count": counts.get(star, 0), "complete": star not in incomplete}
        for star in range(1, 6)
    }


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
            "delivered": 5,
            "delivered_new": 5,
            "coverage": "complete_to_date",
        },
        {
            "name": "high",
            "stars": [4, 5],
            "with_media_only": True,
            "max": 30,
            "delivered": 2,
            "delivered_new": 2,
            "coverage": "complete_to_date",
        },
    ],
    "cost_usd": 0.00718,
}


async def seed_shopee_source(
    session: AsyncSession,
    *,
    breakdowns: list[dict[str, Any] | None],
) -> dict[str, Any]:
    shop = Shop(
        id=uuid.uuid4(),
        platform="shopee",
        external_id=SHOP_ID,
        name="Mavis _ Tech _ Shop",
        url=f"https://shopee.vn/shop/{SHOP_ID}",
        rating=4.81,
        raw={},
    )
    session.add(shop)
    await session.flush()

    products: list[Product] = []
    for index, breakdown in enumerate(breakdowns):
        product = Product(
            id=uuid.uuid4(),
            shop_id=shop.id,
            platform="shopee",
            external_id=f"{PRODUCT_ID}{index}",
            title=f"Sản phẩm Shopee {index}",
            url=PRODUCT_URL,
            price=Decimal("298000"),
            currency="VND",
            rating_avg=None,
            rating_count=None,
            review_count=None,
            rating_breakdown=breakdown,
            raw={},
        )
        products.append(product)
    session.add_all(products)
    await session.flush()

    source = Source(
        id=uuid.uuid4(),
        url=f"https://shopee.vn/shop/{SHOP_ID}",
        url_type="shop",
        platform="shopee",
        status="done",
        external_ref={"shop_id": str(shop.id), "product_ids": [str(p.id) for p in products]},
        stats={"sampling": STRATIFIED_SAMPLING, "reviews_count": 7},
        crawled_at=NOW - timedelta(hours=1),
        created_at=NOW - timedelta(hours=2),
        updated_at=NOW - timedelta(hours=1),
    )
    session.add(source)
    await session.commit()
    return {"source": source, "shop": shop, "products": products}


def breakdown_payload(
    stars: dict[str, Any],
    *,
    since: str = SINCE,
    until: str = "2026-11-27T05:14:56Z",
) -> dict[str, Any]:
    return {
        "source": "apify_review_summary",
        "since": since,
        "until": until,
        "until_by_group": {"low": until, "high": "2026-11-27T05:20:00Z"},
        "stars": stars,
    }


# ---------------------------------------------------------------------------
# Migration 0006
# ---------------------------------------------------------------------------


async def test_migration_head_is_0006_and_products_gain_the_jsonb_column(
    session: AsyncSession,
) -> None:
    revision = (await session.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
    assert revision == "0006_product_rating_breakdown"

    columns = {
        row[0]: (row[1], row[2])
        for row in (
            await session.execute(
                text(
                    "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                    "WHERE table_name = 'products'"
                )
            )
        ).all()
    }
    assert columns["rating_breakdown"] == ("jsonb", "YES")


def test_migration_0006_downgrades_and_upgrades_again(rating_database_url: str) -> None:
    assert rating_database_url == TEST_DATABASE_URL

    down = _alembic("downgrade", "-1")
    assert down.returncode == 0, f"{down.stdout}\n{down.stderr}"

    conn = asyncio.run(_columns_after(rating_database_url))
    assert "rating_breakdown" not in conn

    up = _alembic("upgrade", "head")
    assert up.returncode == 0, f"{up.stdout}\n{up.stderr}"

    conn = asyncio.run(_columns_after(rating_database_url))
    assert "rating_breakdown" in conn


async def _columns_after(database_url: str) -> set[str]:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'products'"
                    )
                )
            ).all()
            return {row[0] for row in rows}
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Upsert semantics
# ---------------------------------------------------------------------------


def product_data(breakdown: dict[str, Any] | None) -> ProductData:
    return ProductData(
        platform="shopee",
        external_id=PRODUCT_ID,
        title="Tai Nghe JBL Tune 230NC",
        url=PRODUCT_URL,
        price=Decimal("298000"),
        currency="VND",
        rating_breakdown=breakdown,
    )


async def test_upsert_never_overwrites_a_stored_breakdown_with_null(session: AsyncSession) -> None:
    stored = breakdown_payload(full_stars({5: 54}))
    await upsert_product(session, product_data(stored), crawled_at=NOW)
    await session.commit()

    await upsert_product(session, product_data(None), crawled_at=NOW + timedelta(minutes=5))
    await session.commit()

    # `expire_on_commit=False` keeps the ORM instance alive: force a re-read from the row.
    row = (
        await session.execute(
            select(Product)
            .where(Product.external_id == PRODUCT_ID)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert row.rating_breakdown == stored, "a crawl without summary data must not erase counts"

    updated = breakdown_payload(full_stars({5: 60}), since=SINCE)
    await upsert_product(session, product_data(updated), crawled_at=NOW + timedelta(minutes=9))
    await session.commit()

    row = (
        await session.execute(
            select(Product)
            .where(Product.external_id == PRODUCT_ID)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert row.rating_breakdown == updated


async def test_upsert_stores_the_breakdown_for_a_new_product(session: AsyncSession) -> None:
    payload = breakdown_payload(full_stars({1: 1, 4: 3, 5: 50}))

    row = await upsert_product(session, product_data(payload), crawled_at=NOW)
    await session.commit()

    assert row.rating_breakdown == payload


# ---------------------------------------------------------------------------
# Report: platform counts vs. reviews
# ---------------------------------------------------------------------------


async def test_complete_source_report_publishes_platform_counts_with_percentages(
    session: AsyncSession,
) -> None:
    seeded = await seed_shopee_source(
        session,
        breakdowns=[
            breakdown_payload(full_stars({1: 10, 2: 10, 3: 20, 4: 30, 5: 50})),
            breakdown_payload(full_stars({5: 10})),
        ],
    )

    report = await build_source_report(session, seeded["source"], now=NOW)

    assert report.rating_basis == "platform_counts"
    counts = report.rating_counts
    assert counts is not None
    assert counts.complete is True
    assert counts.total == 130
    assert (counts.positive, counts.neutral, counts.negative) == (90, 20, 20)
    assert counts.products_with_counts == 2
    assert counts.products_total == 2
    assert counts.since == SINCE

    assert [b.stars for b in report.rating_distribution] == [5, 4, 3, 2, 1]
    assert [b.count for b in report.rating_distribution] == [60, 30, 20, 10, 10]
    assert all(b.lower_bound is False for b in report.rating_distribution)
    assert all(b.pct is not None for b in report.rating_distribution)
    assert sum(b.pct or 0 for b in report.rating_distribution) == pytest.approx(100.0, abs=0.2)

    # Every product row carries its own summary for the detail card.
    assert all(row.rating_counts is not None for row in report.products)
    first = next(
        row for row in report.products if row.rating_counts and row.rating_counts.total == 120
    )
    assert first.rating_counts is not None and first.rating_counts.complete is True

    codes = [w.code for w in report.warnings]
    assert "rating_counts_partial" not in codes
    assert "stratified_sample" in codes


async def test_source_report_is_partial_when_one_product_has_no_breakdown(
    session: AsyncSession,
) -> None:
    seeded = await seed_shopee_source(
        session,
        breakdowns=[
            breakdown_payload(full_stars({1: 1, 4: 3, 5: 50})),
            None,
        ],
    )

    report = await build_source_report(session, seeded["source"], now=NOW)

    assert report.rating_basis == "platform_counts"
    counts = report.rating_counts
    assert counts is not None
    assert counts.complete is False
    assert counts.products_with_counts == 1
    assert counts.products_total == 2
    assert counts.total == 54

    assert all(b.lower_bound is True for b in report.rating_distribution)
    assert all(b.pct is None for b in report.rating_distribution)

    warning = next(w for w in report.warnings if w.code == "rating_counts_partial")
    assert warning.params["stars"] == "1★, 2★, 3★, 4★, 5★"

    missing_row = next(row for row in report.products if row.rating_counts is None)
    assert missing_row.external_id.endswith("1")


async def test_source_report_marks_only_the_limit_reached_star_as_lower_bound(
    session: AsyncSession,
) -> None:
    seeded = await seed_shopee_source(
        session,
        breakdowns=[
            breakdown_payload(
                full_stars({1: 2, 2: 0, 3: 2, 4: 23, 5: 123}, incomplete=frozenset({5}))
            ),
        ],
    )

    report = await build_source_report(session, seeded["source"], now=NOW)

    buckets = {b.stars: b for b in report.rating_distribution}
    assert buckets[5].count == 123
    assert buckets[5].lower_bound is True
    assert all(buckets[star].lower_bound is False for star in (1, 2, 3, 4))
    assert all(b.pct is None for b in report.rating_distribution), "no % while 5★ is partial"

    warning = next(w for w in report.warnings if w.code == "rating_counts_partial")
    assert warning.params == {"stars": "5★"}
    assert report.rating_counts is not None
    assert report.rating_counts.complete is False


async def test_tiki_report_keeps_the_reviews_basis_and_no_rating_counts(
    session: AsyncSession,
) -> None:
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
        rating_avg=4.8,
        rating_count=40,
        review_count=60,
        raw={},
    )
    session.add(product)
    await session.flush()
    source.external_ref = {"product_ids": [str(product.id)]}
    from app.db.models import Review

    session.add_all(
        [
            Review(
                id=uuid.uuid4(),
                product_id=product.id,
                external_id=f"t{index}",
                rating=rating,
                content="Nội dung đủ dài để không bị coi là review rỗng trong bài kiểm thử.",
                sentiment="positive" if rating >= 4 else "negative",
                sentiment_source="rating",
            )
            for index, rating in enumerate([5, 4, 3, 2, 1], start=1)
        ]
    )
    await session.commit()

    report = await build_source_report(session, source, now=NOW)

    assert report.rating_basis == "reviews"
    assert report.rating_counts is None
    assert [b.stars for b in report.rating_distribution] == [5, 4, 3, 2, 1]
    assert [b.count for b in report.rating_distribution] == [1, 1, 1, 1, 1]
    assert all(b.pct == 20.0 for b in report.rating_distribution)
    assert all(row.rating_counts is None for row in report.products)
    assert "rating_counts_partial" not in {w.code for w in report.warnings}


# ---------------------------------------------------------------------------
# crawl_url end-to-end: breakdown persistence, incremental merge, ISO newerThan
# ---------------------------------------------------------------------------


def shopee_product_row() -> dict[str, Any]:
    return {
        "itemId": PRODUCT_ID,
        "shopId": SHOP_ID,
        "name": "Tai Nghe Bluetooth JBL Tune 230NC",
        "price": 298000,
        "rating": 4.87,
        "ratingCount": 54,
        "shopName": "Mavis _ Tech _ Shop",
    }


def keyboard_rows(*, group: str, five_star_count: int = 1) -> list[dict[str, Any]]:
    if group == "low":
        return [
            {
                "_type": "product_summary",
                "product_id": PRODUCT_ID,
                "coverage": "complete_to_date",
                "ratings_found_by_star": {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0},
                "counts_complete": True,
                "star_lists": {
                    "1": "complete_to_date",
                    "2": "complete_to_date",
                    "3": "complete_to_date",
                    "4": "not_requested",
                    "5": "not_requested",
                },
            },
            {"_type": "summary", "stars": [1, 2, 3]},
        ]
    return [
        {
            "_type": "product_summary",
            "product_id": PRODUCT_ID,
            "coverage": "complete_to_date",
            "ratings_found_by_star": {"4": 0, "5": five_star_count},
            "counts_complete": True,
            "star_lists": {
                "1": "not_requested",
                "2": "not_requested",
                "3": "not_requested",
                "4": "complete_to_date",
                "5": "complete_to_date",
            },
        },
        {"_type": "summary", "stars": [4, 5]},
    ]


async def test_shopee_crawl_persists_the_breakdown_and_every_run(
    session: AsyncSession, session_factory: Any
) -> None:
    client = ScriptedApify(
        product_runs=[make_run([shopee_product_row()], cost_usd=0.002, run_id="run-meta")],
        review_runs=[
            make_run(keyboard_rows(group="low"), cost_usd=0.00359, run_id="run-low"),
            make_run(keyboard_rows(group="high"), cost_usd=0.002, run_id="run-high"),
        ],
    )

    result = await crawl_url(PRODUCT_URL, session_factory=session_factory, apify_client=client)

    assert result.status == "done", result.error

    async with session_factory() as fresh:
        source = await fresh.get(Source, result.source_id)
        assert source is not None
        product = (
            await fresh.execute(select(Product).where(Product.external_id == PRODUCT_ID))
        ).scalar_one()

    assert product.rating_breakdown is not None
    assert product.rating_breakdown["source"] == "apify_review_summary"
    assert product.rating_breakdown["stars"] == {
        "1": {"count": 0, "complete": True},
        "2": {"count": 0, "complete": True},
        "3": {"count": 0, "complete": True},
        "4": {"count": 0, "complete": True},
        "5": {"count": 1, "complete": True},
    }
    assert set(product.rating_breakdown["until_by_group"]) == {"low", "high"}
    assert product.rating_breakdown["since"].endswith("Z")

    sampling = source.stats["sampling"]
    assert sampling["cost_usd"] == pytest.approx(0.002 + 0.00359 + 0.002)
    assert [run["kind"] for run in sampling["runs"]] == ["metadata", "low", "high"]
    assert [run["run_id"] for run in sampling["runs"]] == ["run-meta", "run-low", "run-high"]


async def test_second_crawl_sends_the_stored_until_and_merges_without_double_counting(
    session_factory: Any,
) -> None:
    first = ScriptedApify(
        product_runs=[make_run([shopee_product_row()], cost_usd=0.002, run_id="m1")],
        review_runs=[
            make_run(keyboard_rows(group="low"), cost_usd=0.00359, run_id="l1"),
            make_run(keyboard_rows(group="high", five_star_count=1), cost_usd=0.002, run_id="h1"),
        ],
    )
    first_result = await crawl_url(PRODUCT_URL, session_factory=session_factory, apify_client=first)
    assert first_result.status == "done", first_result.error

    async with session_factory() as fresh:
        stored = (
            await fresh.execute(select(Product).where(Product.external_id == PRODUCT_ID))
        ).scalar_one()
        stored_breakdown = dict(stored.rating_breakdown)
        stored_since = stored_breakdown["since"]
        stored_low_until = stored_breakdown["until_by_group"]["low"]
        stored_high_until = stored_breakdown["until_by_group"]["high"]

    second = ScriptedApify(
        product_runs=[make_run([shopee_product_row()], cost_usd=0.002, run_id="m2")],
        review_runs=[
            make_run(keyboard_rows(group="low"), cost_usd=0.00359, run_id="l2"),
            make_run(keyboard_rows(group="high", five_star_count=2), cost_usd=0.002, run_id="h2"),
        ],
    )
    second_result = await crawl_url(
        PRODUCT_URL, session_factory=session_factory, apify_client=second
    )
    assert second_result.status == "done", second_result.error

    calls = second.review_calls()
    assert calls[0].payload["newerThan"] == stored_low_until
    assert calls[1].payload["newerThan"] == stored_high_until
    assert calls[0].payload["newerThan"].endswith("Z")
    assert calls[0].payload["newerThan"] != calls[0].payload["newerThan"][:10]

    async with session_factory() as fresh:
        merged = (
            await fresh.execute(select(Product).where(Product.external_id == PRODUCT_ID))
        ).scalar_one()
        merged_breakdown = dict(merged.rating_breakdown)

    assert merged_breakdown["stars"]["5"]["count"] == 3, "1 + 2, counted once each"
    assert merged_breakdown["since"] == stored_since, "since stays the first crawl"
    # Both crawls may land in the same second, so only "not older" is guaranteed.
    assert merged_breakdown["until_by_group"]["low"] >= stored_low_until
    assert merged_breakdown["until_by_group"]["high"] >= stored_high_until
    assert merged_breakdown["until"] == min(merged_breakdown["until_by_group"].values())


async def test_crawl_without_a_summary_row_leaves_the_breakdown_untouched(
    session_factory: Any,
) -> None:
    first = ScriptedApify(
        product_runs=[make_run([shopee_product_row()], cost_usd=0.002, run_id="m1")],
        review_runs=[
            make_run(keyboard_rows(group="low"), cost_usd=0.00359, run_id="l1"),
            make_run(keyboard_rows(group="high"), cost_usd=0.002, run_id="h1"),
        ],
    )
    await crawl_url(PRODUCT_URL, session_factory=session_factory, apify_client=first)

    async with session_factory() as fresh:
        before = (
            await fresh.execute(select(Product).where(Product.external_id == PRODUCT_ID))
        ).scalar_one()
        before_breakdown = dict(before.rating_breakdown)

    # No product_summary / summary rows at all: the actor returned reviews only.
    second = ScriptedApify(
        product_runs=[make_run([shopee_product_row()], cost_usd=0.002, run_id="m2")],
        review_runs=[
            make_run(
                [{"review_id": "new-1", "rating": 5, "comment": "ổn", "product_id": PRODUCT_ID}],
                cost_usd=0.00359,
                run_id="l2",
            ),
            make_run([], cost_usd=0.002, run_id="h2"),
        ],
    )
    result = await crawl_url(PRODUCT_URL, session_factory=session_factory, apify_client=second)
    assert result.status == "done", result.error

    async with session_factory() as fresh:
        after = (
            await fresh.execute(select(Product).where(Product.external_id == PRODUCT_ID))
        ).scalar_one()

    assert dict(after.rating_breakdown) == before_breakdown, "no data ⇒ no invented counts"
