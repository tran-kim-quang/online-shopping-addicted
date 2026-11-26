"""Integration tests for the source report + CSV export against a dedicated Postgres DB.

Isolation contract: everything happens in ``shopqa_phase5_test`` (override with
``TEST_REPORT_*`` env vars), created via a maintenance connection to ``shopqa``. The
project database ``shopqa`` is never written, truncated, or migrated here. No LLM calls
and no outbound network: the report and CSV paths are pure SQL.

Covered here: every ``totals`` field, median/weighted rating with
``coalesce(rating_count, review_count)``, rating distribution, per-product rows,
ranking thresholds/tie-breaks, jsonb aspects, every warning code, shop vs product
sources, the HTTP endpoints, streaming CSV over >500 rows (batches, cursors, aborts),
the production ``get_session`` wiring and ``scripts/report.py``.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import re
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
from sqlalchemy import event, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.agent.citations import CitationRegistry
from app.agent.tools import AgentContext, _run_query_products
from app.agent.tools import QueryProductsArgs as ArgsQueryProducts
from app.api.deps import get_session, get_settings
from app.core.config import Settings
from app.db.database import Database
from app.db.models import Chunk, Product, Review, Shop, Source
from app.main import create_app
from app.reports.csv_export import (
    PRODUCTS_CSV_COLUMNS,
    REVIEWS_CSV_BASE_COLUMNS,
    UTF8_BOM_BYTES,
    build_csv_filename,
    iter_products_csv,
    iter_reviews_csv,
)
from app.reports.source_report import build_source_report

pytestmark = pytest.mark.integration

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent
SECRET = os.environ.get("SECRET_KEY", "vN5y6rM0J9h2xL8qK3pW1zB4cF7dG0tH2sV5mN8wX4Q=")

PG_HOST = os.environ.get("TEST_REPORT_PG_HOST", "localhost")
PG_PORT = int(os.environ.get("TEST_REPORT_PG_PORT", "5433"))
PG_USER = os.environ.get("TEST_REPORT_PG_USER", "shopqa")
PG_PASSWORD = os.environ.get("TEST_REPORT_PG_PASSWORD", "shopqa")
PG_MAINTENANCE_DB = os.environ.get("TEST_REPORT_PG_MAINTENANCE_DB", "shopqa")
TEST_DB_NAME = os.environ.get("TEST_REPORT_DB_NAME", "shopqa_phase5_test")
PROJECT_DB_NAME = "shopqa"

MAINTENANCE_DSN = f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{PG_MAINTENANCE_DB}"
TEST_DATABASE_URL = (
    f"postgresql+asyncpg://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{TEST_DB_NAME}"
)
NOW = datetime.now(UTC).replace(microsecond=0)


# ---------------------------------------------------------------------------
# DB bootstrap / fixtures
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
def report_database_url() -> str:
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
async def session_factory(report_database_url: str) -> AsyncIterator[Any]:
    engine = create_async_engine(report_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            current = (await conn.execute(text("SELECT current_database()"))).scalar_one()
            assert current == TEST_DB_NAME, f"refusing to truncate {current}"
            await conn.execute(text("TRUNCATE chunks, reviews, products, shops, sources CASCADE"))
        yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
async def session(session_factory: Any) -> AsyncIterator[AsyncSession]:
    async with session_factory() as active_session:
        yield active_session


# ---------------------------------------------------------------------------
# Seeds
# ---------------------------------------------------------------------------


def _product(
    external_id: str,
    title: str,
    *,
    shop_id: uuid.UUID | None,
    price: str | None = "125000",
    list_price: str | None = None,
    sold_count: int | None = 100,
    rating_avg: float | None = 4.5,
    rating_count: int | None = 10,
    review_count: int | None = 10,
    positive: int = 0,
    neutral: int = 0,
    negative: int = 0,
) -> Product:
    return Product(
        id=uuid.uuid4(),
        shop_id=shop_id,
        platform="tiki",
        external_id=external_id,
        title=title,
        url=f"https://tiki.vn/{external_id}.html",
        price=Decimal(price) if price is not None else None,
        list_price=Decimal(list_price) if list_price is not None else None,
        currency="VND",
        sold_count=sold_count,
        rating_avg=rating_avg,
        rating_count=rating_count,
        review_count=review_count,
        positive_review_count=positive,
        neutral_review_count=neutral,
        negative_review_count=negative,
        brand="Harmony",
        category="Thực phẩm",
        short_description=f"{title} chính hãng.",
        specs={"Xuất xứ": "Việt Nam"},
        images=[],
        raw={},
        crawled_at=NOW - timedelta(hours=2),
        ingested_at=NOW - timedelta(hours=1),
    )


def _review(
    product_id: uuid.UUID,
    external_id: str,
    rating: int,
    sentiment: str | None,
    sentiment_source: str | None,
    *,
    aspects: Any = None,
    created_at: datetime | None = None,
    title: str | None = None,
    content: str | None = None,
    author: str | None = "Nguyễn Văn A",
) -> Review:
    return Review(
        product_id=product_id,
        external_id=external_id,
        rating=rating,
        sentiment=sentiment,
        sentiment_source=sentiment_source,
        aspects=[] if aspects is None else aspects,
        created_at_source=created_at,
        title=title,
        content=content,
        author=author,
        helpful_count=2,
        images=[],
        raw={},
        crawled_at=NOW - timedelta(hours=2),
    )


def _source(
    *,
    shop: Shop | None,
    url_type: str = "shop",
    status: str = "done",
    stats: dict[str, Any] | None = None,
    crawled_at: datetime | None = None,
    product_external_id: str | None = None,
) -> Source:
    ref: dict[str, Any] = {}
    if shop is not None:
        ref["shop_id"] = str(shop.id)
        ref["seller_id"] = shop.external_id
    if product_external_id is not None:
        ref["product_id"] = product_external_id
    return Source(
        id=uuid.uuid4(),
        url="https://tiki.vn/cua-hang/shop-bao-cao",
        url_type=url_type,
        platform="tiki",
        status=status,
        stats={"ingest": {"chunks": 5}} if stats is None else stats,
        crawled_at=crawled_at if crawled_at is not None else NOW - timedelta(hours=2),
        external_ref=ref,
        created_at=NOW - timedelta(hours=3),
        updated_at=NOW - timedelta(hours=1),
    )


async def seed_catalog(session: AsyncSession) -> dict[str, Any]:
    """Shop source with 7 products covering every aggregate/tie-break/edge branch."""
    shop = Shop(
        id=uuid.uuid4(),
        platform="tiki",
        external_id="seller-report-1",
        name="Shop Báo Cáo",
        url="https://tiki.vn/cua-hang/shop-bao-cao",
        rating=4.6,
        follower_count=1234,
        product_count=99,
        raw={},
        crawled_at=NOW - timedelta(hours=2),
    )
    session.add(shop)
    await session.flush()

    p1 = _product(
        "1001",
        "Gạo ST25 5kg",
        shop_id=shop.id,
        price="125000",
        list_price="150000",
        sold_count=500,
        rating_avg=4.5,
        rating_count=None,  # Tiki thật: chỉ có review_count
        review_count=100,
    )
    p2 = _product(
        "1002",
        "Gạo ST24 5kg",
        shop_id=shop.id,
        price="99000",
        list_price="99000",
        sold_count=900,
        rating_avg=5.0,
        rating_count=10,
        review_count=4,
    )
    p3 = _product(
        "1003",
        "Nếp thơm 5kg",
        shop_id=shop.id,
        price="150000",
        list_price=None,
        sold_count=None,
        rating_avg=4.8,
        rating_count=None,
        review_count=None,
    )
    p4 = _product(
        "1004",
        "Gạo lứt 1kg",
        shop_id=shop.id,
        price="45000",
        list_price="50000",
        sold_count=100,
        rating_avg=4.8,
        rating_count=1000,
        review_count=2,
        positive=3,
        negative=1,
    )
    p5 = _product(
        "1005",
        "Gạo Nhật 2kg",
        shop_id=shop.id,
        price=None,
        list_price=None,
        sold_count=50,
        rating_avg=4.8,
        rating_count=3,
        review_count=0,
    )
    p6 = _product(
        "1006",
        "Bột gạo 500g",
        shop_id=shop.id,
        price="20000",
        list_price="30000",
        sold_count=500,
        rating_avg=5.0,
        rating_count=3,
        review_count=0,
    )
    p7 = _product(
        "1007",
        "Bánh tráng 200g",
        shop_id=shop.id,
        price="30000",
        list_price="30000",
        sold_count=20,
        rating_avg=5.0,
        rating_count=2,  # dưới ngưỡng 3 -> không vào highest_rated
        review_count=2,
    )
    products = [p1, p2, p3, p4, p5, p6, p7]
    session.add_all(products)
    await session.flush()

    session.add_all(
        [
            _review(
                p1.id,
                "1001-r1",
                5,
                "positive",
                "llm",
                aspects=[{"aspect": "Giao Hàng", "sentiment": "POSITIVE"}],
                created_at=datetime(2026, 10, 5, tzinfo=UTC),
                title="Rất đáng tiền",
                content="Cơm dẻo, thơm, giao nhanh",
            ),
            _review(
                p1.id,
                "1001-r2",
                4,
                "positive",
                "llm",
                aspects=[
                    {"aspect": "đóng gói", "sentiment": "negative"},
                    {"aspect": "   ", "sentiment": "positive"},
                ],
                created_at=datetime(2026, 10, 3, tzinfo=UTC),
                content="Đóng gói hơi sơ sài",
            ),
            _review(
                p1.id,
                "1001-r3",
                2,
                "negative",
                "rating",
                aspects={"not": "an array"},  # jsonb sai hình dạng, phải bị bỏ qua
                created_at=datetime(2026, 10, 1, tzinfo=UTC),
                content="Giao chậm",
            ),
            _review(
                p2.id,
                "1002-r1",
                5,
                "positive",
                "llm",
                created_at=datetime(2026, 9, 30, tzinfo=UTC),
                content="Tốt",
            ),
            _review(
                p2.id,
                "1002-r2",
                5,
                "positive",
                "llm",
                created_at=datetime(2026, 9, 28, tzinfo=UTC),
                content="Rất tốt",
            ),
            _review(
                p2.id,
                "1002-r3",
                4,
                "neutral",
                "llm",
                created_at=datetime(2026, 9, 27, tzinfo=UTC),
                content="Tạm ổn",
            ),
            _review(
                p2.id,
                "1002-r4",
                1,
                "negative",
                "rating",
                created_at=datetime(2026, 9, 26, tzinfo=UTC),
                content="=cmd|' /C calc'!A0",  # formula injection
                title="+1 ngôi sao",
            ),
        ]
    )

    session.add_all(
        [
            Chunk(
                product_id=p1.id,
                shop_id=shop.id,
                source_type=source_type,
                source_ref=f"{source_type}-{idx}",
                text=f"chunk {source_type} {idx}",
                content_hash=f"{uuid.uuid4().hex}{uuid.uuid4().hex}"[:64],
            )
            for source_type, count in (("description", 2), ("meta", 1), ("review", 2))
            for idx in range(count)
        ]
    )

    source = _source(shop=shop)
    session.add(source)
    await session.commit()
    return {"shop": shop, "products": {p.external_id: p for p in products}, "source": source}


async def _reviews_csv_rows(
    session: AsyncSession, product_ids: list[uuid.UUID], **kwargs: Any
) -> list[dict[str, str]]:
    chunks = [chunk async for chunk in iter_reviews_csv(session, product_ids, **kwargs)]
    return _csv_dicts(b"".join(chunks))


def _rows_by_external(report: Any) -> dict[str, Any]:
    return {row.external_id: row for row in report.products}


def _parse_csv(content: bytes) -> list[list[str]]:
    text_ = (
        content[len(UTF8_BOM_BYTES) :].decode("utf-8")
        if content.startswith(UTF8_BOM_BYTES)
        else content.decode("utf-8")
    )
    return list(csv.reader(io.StringIO(text_), strict=True))


def _csv_dicts(content: bytes) -> list[dict[str, str]]:
    rows = _parse_csv(content)
    header = rows[0]
    return [dict(zip(header, row, strict=True)) for row in rows[1:]]


# ---------------------------------------------------------------------------
# Report: totals
# ---------------------------------------------------------------------------


async def test_totals_match_hand_computed_sql_aggregates(
    session: AsyncSession, session_factory: Any
) -> None:
    seeded = await seed_catalog(session)

    report = await build_source_report(session, seeded["source"], now=NOW)
    totals = report.totals

    assert totals.products == 7
    assert totals.products_with_price == 6
    assert totals.sold_total == 2070
    assert totals.price_min == pytest.approx(20000.0)
    assert totals.price_max == pytest.approx(150000.0)
    # percentile_cont(0.5) của 6 giá: (45.000 + 99.000) / 2
    assert totals.price_median == pytest.approx(72000.0)
    assert totals.review_count_reported == 108
    assert totals.reviews_collected == 7
    assert totals.coverage_pct == pytest.approx(6.5)
    assert totals.sentiment.positive == 4
    assert totals.sentiment.neutral == 1
    assert totals.sentiment.negative == 2
    assert totals.sentiment.unanalyzed == 0
    assert totals.sentiment_source.llm == 5
    assert totals.sentiment_source.rating == 2
    assert totals.sentiment_source.none == 0
    assert totals.chunks.total == 5
    assert totals.chunks.by_type == {"description": 2, "meta": 1, "review": 2}
    assert report.shop is not None
    assert report.shop.name == "Shop Báo Cáo"
    assert report.shop.follower_count == 1234
    assert report.source.id == seeded["source"].id
    assert report.generated_at == NOW


async def test_weighted_rating_uses_coalesce_of_rating_count_and_review_count(
    session: AsyncSession,
) -> None:
    seeded = await seed_catalog(session)
    products = list((await session.execute(select(Product))).scalars().all())

    report = await build_source_report(session, seeded["source"], now=NOW)

    def _effective(prod: Product) -> int:
        return prod.rating_count if prod.rating_count is not None else (prod.review_count or 0)

    weighted = [p for p in products if p.rating_avg is not None and _effective(p) > 0]
    expected_num = sum((p.rating_avg or 0.0) * _effective(p) for p in weighted)
    expected_den = sum(_effective(p) for p in weighted)
    naive_num = sum(
        (p.rating_avg or 0.0) * p.rating_count for p in products if p.rating_count is not None
    )
    naive_den = sum(p.rating_count for p in products if p.rating_count is not None)

    assert report.totals.rating_avg_weighted == pytest.approx(round(expected_num / expected_den, 2))
    assert report.totals.rating_avg_weighted == pytest.approx(4.78)
    # Nếu bỏ qua review_count khi rating_count NULL thì ra 4.80 -> khoá hành vi coalesce.
    assert round(naive_num / naive_den, 2) == pytest.approx(4.80)
    assert report.totals.rating_avg_weighted != pytest.approx(round(naive_num / naive_den, 2))

    row = _rows_by_external(report)["1001"]
    assert row.rating_count is None
    assert row.rating_count_effective == 100
    assert row.review_count_reported == 100


async def test_rating_distribution_always_has_five_stars_with_pcts(
    session: AsyncSession,
) -> None:
    seeded = await seed_catalog(session)

    report = await build_source_report(session, seeded["source"], now=NOW)

    assert [bucket.stars for bucket in report.rating_distribution] == [5, 4, 3, 2, 1]
    assert [bucket.count for bucket in report.rating_distribution] == [3, 2, 0, 1, 1]
    assert [bucket.pct for bucket in report.rating_distribution] == [42.9, 28.6, 0.0, 14.3, 14.3]


async def test_product_rows_sorted_by_sold_count_desc_nulls_last_with_effective_tiebreak(
    session: AsyncSession,
) -> None:
    seeded = await seed_catalog(session)

    report = await build_source_report(session, seeded["source"], now=NOW)

    assert [row.external_id for row in report.products] == [
        "1002",
        "1001",  # cùng 500 sold nhưng effective 100 > 3
        "1006",
        "1004",
        "1005",
        "1007",
        "1003",  # sold_count NULL xuống cuối
    ]


async def test_product_row_derived_fields(session: AsyncSession) -> None:
    seeded = await seed_catalog(session)

    report = await build_source_report(session, seeded["source"], now=NOW)
    rows = _rows_by_external(report)

    p1 = rows["1001"]
    assert p1.price == pytest.approx(125000.0)
    assert p1.discount_pct == pytest.approx(16.7)
    assert (p1.positive, p1.neutral, p1.negative) == (2, 0, 1)
    assert p1.positive_pct == pytest.approx(66.7)
    assert p1.reviews_collected == 3
    assert p1.coverage_pct == pytest.approx(3.0)
    assert p1.last_review_at == datetime(2026, 10, 5, tzinfo=UTC)

    # Không có review row -> dùng bộ đếm trên products và last_review_at = None.
    p4 = rows["1004"]
    assert (p4.positive, p4.neutral, p4.negative) == (3, 0, 1)
    assert p4.positive_pct == pytest.approx(75.0)
    assert p4.reviews_collected == 0
    assert p4.coverage_pct == pytest.approx(0.0)
    assert p4.last_review_at is None
    assert p4.discount_pct == pytest.approx(10.0)

    # review_count = 0 và giá NULL -> chia 0 thành None, không phải 0%.
    p5 = rows["1005"]
    assert p5.coverage_pct is None
    assert p5.price is None
    assert p5.discount_pct is None
    assert p5.positive_pct is None

    p3 = rows["1003"]
    assert p3.review_count_reported is None
    assert p3.coverage_pct is None
    assert p3.rating_count_effective is None
    assert p3.sold_count is None


# ---------------------------------------------------------------------------
# Report: rankings
# ---------------------------------------------------------------------------


async def test_rankings_apply_thresholds_and_tiebreaks(session: AsyncSession) -> None:
    seeded = await seed_catalog(session)

    report = await build_source_report(session, seeded["source"], now=NOW)
    rankings = report.rankings

    assert [item.title for item in rankings.best_selling] == [
        "Gạo ST24 5kg",
        "Gạo ST25 5kg",
        "Bột gạo 500g",
        "Gạo lứt 1kg",
        "Gạo Nhật 2kg",
        "Bánh tráng 200g",
    ]

    # 5.0 sao: Gạo ST24 (10 phiếu) trước Bột gạo (3 phiếu); Bánh tráng (2 phiếu) bị loại.
    assert [item.title for item in rankings.highest_rated] == [
        "Gạo ST24 5kg",
        "Bột gạo 500g",
        "Gạo lứt 1kg",
        "Gạo Nhật 2kg",
        "Gạo ST25 5kg",
    ]
    assert all(item.value >= 4.5 for item in rankings.highest_rated)

    # most_praised chỉ lấy positive > 0, tie-break bằng rating_avg.
    assert [item.title for item in rankings.most_praised] == [
        "Gạo lứt 1kg",
        "Gạo ST24 5kg",
        "Gạo ST25 5kg",
    ]
    assert all(item.value > 0 for item in rankings.most_praised)

    # most_complained chỉ lấy negative > 0, tie-break bằng reviews_collected.
    assert [item.title for item in rankings.most_complained] == [
        "Gạo ST24 5kg",
        "Gạo ST25 5kg",
        "Gạo lứt 1kg",
    ]

    # biggest_discount bỏ sản phẩm giảm 0% và thiếu list_price.
    assert [item.title for item in rankings.biggest_discount] == [
        "Bột gạo 500g",
        "Gạo ST25 5kg",
        "Gạo lứt 1kg",
    ]
    assert [item.value for item in rankings.biggest_discount] == pytest.approx([33.3, 16.7, 10.0])
    assert all(item.value > 0 for item in rankings.biggest_discount)

    assert [item.title for item in rankings.cheapest] == [
        "Bột gạo 500g",
        "Bánh tráng 200g",
        "Gạo lứt 1kg",
        "Gạo ST24 5kg",
        "Gạo ST25 5kg",
        "Nếp thơm 5kg",
    ]


async def test_most_complained_is_empty_without_negative_reviews(
    session: AsyncSession,
) -> None:
    shop = Shop(
        id=uuid.uuid4(),
        platform="tiki",
        external_id="seller-happy",
        name="Shop Vui Vẻ",
        url="https://tiki.vn/cua-hang/shop-vui-ve",
        raw={},
        crawled_at=NOW,
    )
    session.add(shop)
    await session.flush()
    product = _product("3001", "Trà sen 100g", shop_id=shop.id, review_count=2, rating_count=2)
    session.add(product)
    await session.flush()
    session.add_all(
        [
            _review(product.id, "3001-r1", 5, "positive", "llm"),
            _review(product.id, "3001-r2", 5, "positive", "llm"),
        ]
    )
    source = _source(shop=shop)
    session.add(source)
    await session.commit()

    report = await build_source_report(session, source, now=NOW)

    assert report.rankings.most_complained == []
    assert report.rankings.most_praised[0].value == 2


async def test_aspects_are_aggregated_from_jsonb_and_skip_malformed_entries(
    session: AsyncSession,
) -> None:
    seeded = await seed_catalog(session)

    report = await build_source_report(session, seeded["source"], now=NOW)

    assert [(a.aspect, a.count) for a in report.aspects.top_positive] == [("giao hàng", 1)]
    assert [(a.aspect, a.count) for a in report.aspects.top_negative] == [("đóng gói", 1)]
    assert "no_aspects" not in [w.code for w in report.warnings]


async def test_top_n_limits_rankings_and_aspect_lists(session: AsyncSession) -> None:
    seeded = await seed_catalog(session)

    report = await build_source_report(session, seeded["source"], top_n=2, now=NOW)

    assert len(report.rankings.best_selling) == 2
    assert len(report.rankings.highest_rated) == 2
    assert len(report.rankings.cheapest) == 2
    assert len(report.aspects.top_positive) <= 2
    assert len(report.aspects.top_negative) <= 2
    # products[] luôn đủ, top_n chỉ ảnh hưởng rankings/aspects.
    assert len(report.products) == 7


# ---------------------------------------------------------------------------
# Report: warnings
# ---------------------------------------------------------------------------


async def test_warnings_report_partial_coverage_missing_price_and_sold(
    session: AsyncSession,
) -> None:
    seeded = await seed_catalog(session)

    report = await build_source_report(session, seeded["source"], now=NOW)
    codes = [w.code for w in report.warnings]

    assert codes == ["partial_reviews", "missing_price", "missing_sold_count"]
    partial = report.warnings[0]
    assert partial.message == "Mới thu thập 3/104 review ở 3 sản phẩm."
    products = seeded["products"]
    assert set(partial.product_ids or []) == {
        products["1001"].id,
        products["1004"].id,
        products["1007"].id,
    }
    assert report.warnings[1].product_ids == [products["1005"].id]
    assert report.warnings[2].product_ids == [products["1003"].id]


async def test_no_warnings_when_data_is_complete(session: AsyncSession) -> None:
    shop = Shop(
        id=uuid.uuid4(),
        platform="tiki",
        external_id="seller-complete",
        name="Shop Đầy Đủ",
        url="https://tiki.vn/cua-hang/shop-day-du",
        raw={},
        crawled_at=NOW - timedelta(hours=1),
    )
    session.add(shop)
    await session.flush()
    product = _product(
        "4001",
        "Mật ong 500ml",
        shop_id=shop.id,
        review_count=1,
        rating_count=1,
        sold_count=10,
        price="200000",
        list_price="250000",
        rating_avg=5.0,
    )
    session.add(product)
    await session.flush()
    session.add(
        _review(
            product.id,
            "4001-r1",
            5,
            "positive",
            "llm",
            aspects=[{"aspect": "chất lượng", "sentiment": "positive"}],
            created_at=NOW - timedelta(days=3),
        )
    )
    source = _source(shop=shop, crawled_at=NOW - timedelta(hours=1))
    session.add(source)
    await session.commit()

    report = await build_source_report(session, source, now=NOW)

    assert report.warnings == []


async def test_reported_count_lower_and_sentiment_rating_only_warnings(
    session: AsyncSession,
) -> None:
    shop = Shop(
        id=uuid.uuid4(),
        platform="tiki",
        external_id="seller-rating-only",
        name="Shop Chỉ Sao",
        url="https://tiki.vn/cua-hang/shop-chi-sao",
        raw={},
        crawled_at=NOW - timedelta(hours=1),
    )
    session.add(shop)
    await session.flush()
    product = _product(
        "5001",
        "Khăn giấy 100 tờ",
        shop_id=shop.id,
        review_count=1,  # sàn báo 1 nhưng thu được 3
        rating_count=1,
        sold_count=10,
        price="15000",
        list_price="20000",
    )
    session.add(product)
    await session.flush()
    session.add_all(
        [
            _review(product.id, f"5001-r{i}", 5, "positive", "rating", created_at=NOW)
            for i in range(2)
        ]
        + [_review(product.id, "5001-r9", 4, "positive", "rating", created_at=NOW)]
    )
    source = _source(shop=shop, crawled_at=NOW - timedelta(hours=1))
    session.add(source)
    await session.commit()

    report = await build_source_report(session, source, now=NOW)
    codes = [w.code for w in report.warnings]

    assert "reported_count_lower" in codes
    assert "sentiment_rating_only" in codes
    assert "no_aspects" in codes
    assert report.totals.coverage_pct == pytest.approx(300.0)


async def test_unanalyzed_reviews_warning_lists_products(session: AsyncSession) -> None:
    seeded = await seed_catalog(session)
    products = seeded["products"]
    session.add(
        _review(products["1006"].id, "1006-r1", 3, None, None, created_at=NOW - timedelta(days=1))
    )
    await session.commit()

    report = await build_source_report(session, seeded["source"], now=NOW)

    entry = next(w for w in report.warnings if w.code == "unanalyzed_reviews")
    assert entry.product_ids == [products["1006"].id]
    assert report.totals.sentiment.unanalyzed == 1


async def test_not_ingested_and_stale_data_warnings(session: AsyncSession) -> None:
    seeded = await seed_catalog(session)
    seeded["source"].stats = {}
    seeded["source"].crawled_at = NOW - timedelta(hours=48)
    await session.commit()

    report = await build_source_report(session, seeded["source"], now=NOW, settings=None)
    codes = [w.code for w in report.warnings]

    assert "not_ingested" in codes
    assert "stale_data" in codes
    stale = next(w for w in report.warnings if w.code == "stale_data")
    assert f"hơn {Settings(SECRET_KEY=SECRET).CRAWL_CACHE_TTL_HOURS} giờ" in stale.message


# ---------------------------------------------------------------------------
# Report: source shapes
# ---------------------------------------------------------------------------


async def test_product_source_reports_only_that_product(session: AsyncSession) -> None:
    await seed_catalog(session)
    product = (
        await session.execute(select(Product).where(Product.external_id == "1001"))
    ).scalar_one()
    source = _source(shop=None, url_type="product", product_external_id="1001")
    session.add(source)
    await session.commit()

    report = await build_source_report(session, source, now=NOW)

    assert [row.external_id for row in report.products] == ["1001"]
    assert report.totals.products == 1
    assert report.totals.reviews_collected == 3
    assert report.totals.sold_total == 500
    assert report.shop is not None and report.shop.id == product.shop_id
    assert report.source.url_type == "product"


async def test_shop_source_without_reviews_has_zero_distribution(session: AsyncSession) -> None:
    shop = Shop(
        id=uuid.uuid4(),
        platform="tiki",
        external_id="seller-no-reviews",
        name="Shop Chưa Có Đánh Giá",
        url="https://tiki.vn/cua-hang/shop-chua-co-danh-gia",
        raw={},
        crawled_at=NOW - timedelta(hours=1),
    )
    session.add(shop)
    await session.flush()
    session.add(_product("6001", "Nồi chiên 5L", shop_id=shop.id, review_count=0, rating_count=0))
    source = _source(shop=shop, crawled_at=NOW - timedelta(hours=1))
    session.add(source)
    await session.commit()

    report = await build_source_report(session, source, now=NOW)

    assert report.totals.reviews_collected == 0
    assert report.totals.coverage_pct is None
    assert [bucket.count for bucket in report.rating_distribution] == [0, 0, 0, 0, 0]
    assert all(bucket.pct is None for bucket in report.rating_distribution)
    assert report.rankings.most_complained == []
    assert report.totals.products == 1


# ---------------------------------------------------------------------------
# CSV: streaming, filters, injection
# ---------------------------------------------------------------------------


async def _bulk_reviews(session: AsyncSession, product: Product, count: int) -> None:
    rows = [
        {
            "id": uuid.uuid4(),
            "product_id": product.id,
            "external_id": f"bulk-{i:04d}",
            "rating": 5 if i % 2 == 0 else 4,
            "sentiment": "positive" if i % 2 == 0 else "neutral",
            "sentiment_source": "llm",
            "title": f"Đánh giá {i}",
            "content": f"Nội dung {i}, có dấu phẩy\nvà xuống dòng",
            "author": f"Người dùng {i}",
            "aspects": [{"aspect": "giao hàng", "sentiment": "positive"}],
            "images": [],
            "raw": {},
            "helpful_count": i % 7,
            "created_at_source": NOW - timedelta(minutes=i),
            "crawled_at": NOW - timedelta(hours=1),
        }
        for i in range(count)
    ]
    await session.execute(insert(Review), rows)
    await session.commit()


async def test_reviews_csv_streams_more_than_five_hundred_rows_in_batches(
    session_factory: Any,
) -> None:
    async with session_factory() as session:
        shop = Shop(
            id=uuid.uuid4(),
            platform="tiki",
            external_id="seller-bulk",
            name="Shop Nhiều Review",
            url="https://tiki.vn/cua-hang/shop-nhieu-review",
            raw={},
            crawled_at=NOW - timedelta(hours=1),
        )
        session.add(shop)
        await session.flush()
        product = _product("7001", "Sản phẩm nhiều review", shop_id=shop.id, review_count=620)
        session.add(product)
        await session.flush()
        source = _source(shop=shop, crawled_at=NOW - timedelta(hours=1))
        session.add(source)
        await session.commit()
        await _bulk_reviews(session, product, 620)

        execution_options: list[dict[str, Any]] = []

        listen_target = session.bind.sync_engine  # type: ignore[union-attr]

        @event.listens_for(listen_target, "before_cursor_execute")
        def _capture(
            _conn: Any,
            _cur: Any,
            statement: str,
            _params: Any,
            context: Any,
            _executemany: Any,
        ) -> None:
            if "FROM reviews" in statement and "ORDER BY" in statement:
                execution_options.append(dict(context.execution_options))

        generator = iter_reviews_csv(session, [product.id])
        chunks = [chunk async for chunk in generator]

    assert execution_options, "reviews select must go through before_cursor_execute"
    options = execution_options[-1]
    assert options["stream_results"] is True
    assert options["yield_per"] == 500
    assert options["max_row_buffer"] == 500

    header = _parse_csv(chunks[0])[0]
    assert header == list(REVIEWS_CSV_BASE_COLUMNS)
    data_rows = [row for chunk in chunks[1:] for row in _parse_csv(chunk)]
    assert len(data_rows) == 620
    # 1 header + ceil(620/500) lô dữ liệu
    assert len(chunks) == 3
    assert [len(_parse_csv(chunk)) for chunk in chunks[1:]] == [500, 120]

    async with session_factory() as verify:
        current = (await verify.execute(text("SELECT current_database()"))).scalar_one()
        assert current == TEST_DB_NAME
        remaining = (await verify.execute(text("SELECT count(*) FROM reviews"))).scalar_one()
    assert remaining == 620


async def test_products_csv_streams_cursor_and_releases_it_on_early_abort(
    session_factory: Any,
) -> None:
    async with session_factory() as session:
        shop = Shop(
            id=uuid.uuid4(),
            platform="tiki",
            external_id="seller-abort",
            name="Shop Ngắt Giữa Chừng",
            url="https://tiki.vn/cua-hang/shop-ngat-giua-chung",
            raw={},
            crawled_at=NOW - timedelta(hours=1),
        )
        session.add(shop)
        await session.flush()
        products = [
            _product(f"8{i:03d}", f"Sản phẩm {i}", shop_id=shop.id, sold_count=i, review_count=0)
            for i in range(700)
        ]
        session.add_all(products)
        await session.commit()

        generator = iter_products_csv(session, [p.id for p in products], batch_size=100)
        header = await generator.__anext__()
        first_batch = await generator.__anext__()
        assert _parse_csv(header)[0] == list(PRODUCTS_CSV_COLUMNS)
        assert len(_parse_csv(first_batch)) == 100

        await generator.aclose()  # client ngắt giữa chừng

        # Cursor/portal đã nhả: session dùng lại được và stream mới chạy đủ.
        assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
        second = [chunk async for chunk in iter_products_csv(session, [p.id for p in products])]
        assert len(_parse_csv(second[0])) == 1
        assert sum(len(_parse_csv(chunk)) for chunk in second[1:]) == 700
        await session.close()

    async with session_factory() as hygiene:
        leftover = (
            await hygiene.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND state = 'idle in transaction' "
                    "AND pid <> pg_backend_pid()"
                )
            )
        ).scalar_one()
        assert leftover == 0


async def test_reviews_csv_applies_filters_and_author_opt_in(session: AsyncSession) -> None:
    seeded = await seed_catalog(session)
    product_ids = [p.id for p in seeded["products"].values()]

    default_rows = await _reviews_csv_rows(session, product_ids)
    assert len(default_rows) == 7
    assert "author" not in default_rows[0]

    negative = await _reviews_csv_rows(session, product_ids, sentiment="negative")
    assert len(negative) == 2
    assert {row["sentiment"] for row in negative} == {"negative"}

    low = await _reviews_csv_rows(session, product_ids, min_rating=4, max_rating=5)
    assert {int(row["rating"]) for row in low} == {4, 5}

    with_author = await _reviews_csv_rows(session, product_ids, include_author=True)
    assert "author" in with_author[0]
    assert all(row["author"] == "Nguyễn Văn A" for row in with_author)

    # Formula injection trong content/title phải bị vô hiệu hoá.
    injected = [row for row in default_rows if row["review_external_id"] == "1002-r4"][0]
    assert injected["content"] == "'=cmd|' /C calc'!A0"
    assert injected["title"] == "'+1 ngôi sao"
    assert "raw" not in injected


async def test_products_csv_carries_effective_rating_count_and_shop_name(
    session: AsyncSession,
) -> None:
    seeded = await seed_catalog(session)
    product_ids = [p.id for p in seeded["products"].values()]

    chunks = [chunk async for chunk in iter_products_csv(session, product_ids)]
    rows = _csv_dicts(b"".join(chunks))

    assert len(rows) == 7
    by_external = {row["external_id"]: row for row in rows}
    assert by_external["1001"]["rating_count"] == ""
    assert by_external["1001"]["rating_count_effective"] == "100"
    assert by_external["1001"]["shop_name"] == "Shop Báo Cáo"
    assert by_external["1001"]["discount_pct"] == "16.7"
    assert by_external["1001"]["coverage_pct"] == "3.0"
    assert by_external["1005"]["price"] == ""
    assert by_external["1003"]["sold_count"] == ""
    assert [row["external_id"] for row in rows] == [
        "1002",
        "1001",
        "1006",
        "1004",
        "1005",
        "1007",
        "1003",
    ]


# ---------------------------------------------------------------------------
# HTTP endpoints against the real schema
# ---------------------------------------------------------------------------


@pytest.fixture
async def api_client(session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_settings] = lambda: Settings(SECRET_KEY=SECRET)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", timeout=30.0
    ) as client:
        yield client


async def test_api_report_endpoint_returns_full_json(
    api_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    seeded = await seed_catalog(session)

    response = await api_client.get(
        f"/api/sources/{seeded['source'].id}/report", params={"top_n": 3}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["totals"]["products"] == 7
    assert body["totals"]["reviews_collected"] == 7
    assert body["totals"]["rating_avg_weighted"] == pytest.approx(4.78)
    assert body["shop"]["name"] == "Shop Báo Cáo"
    assert len(body["products"]) == 7
    assert len(body["rankings"]["best_selling"]) == 3
    assert [b["stars"] for b in body["rating_distribution"]] == [5, 4, 3, 2, 1]
    assert body["aspects"]["top_positive"][0]["aspect"] == "giao hàng"
    assert {w["code"] for w in body["warnings"]} >= {"partial_reviews", "missing_price"}


async def test_api_409_only_when_in_progress_and_empty(
    api_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    pending_empty = _source(shop=None, url_type=None, status="pending", stats={})
    session.add(pending_empty)
    pending_with_products = await seed_catalog(session)
    pending_with_products["source"].status = "pending"
    await session.commit()

    empty_response = await api_client.get(f"/api/sources/{pending_empty.id}/report")
    ready_response = await api_client.get(
        f"/api/sources/{pending_with_products['source'].id}/report"
    )

    assert empty_response.status_code == 409
    assert empty_response.json()["error"] == "source_not_ready"
    assert ready_response.status_code == 200


async def test_api_csv_endpoints_return_bom_attachments(
    api_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    seeded = await seed_catalog(session)
    source = seeded["source"]

    products_response = await api_client.get(f"/api/sources/{source.id}/export/products.csv")
    reviews_response = await api_client.get(f"/api/sources/{source.id}/export/reviews.csv")

    assert products_response.status_code == 200
    assert reviews_response.status_code == 200
    for response in (products_response, reviews_response):
        assert response.content.startswith(UTF8_BOM_BYTES)
        assert response.headers["content-type"].startswith("text/csv")
        filename = response.headers["content-disposition"].split("filename=")[1].strip('"')
        assert re.match(r"^tiki-shop-bao-cao-(products|reviews)-\d{8}\.csv$", filename)

    product_rows = _csv_dicts(products_response.content)
    review_rows = _csv_dicts(reviews_response.content)
    assert len(product_rows) == 7
    assert len(review_rows) == 7
    assert "author" not in review_rows[0]

    expected_name = build_csv_filename(source, seeded["shop"], [], "products")
    assert expected_name.startswith("tiki-shop-bao-cao-products-")


async def test_api_reviews_csv_filters(session: AsyncSession) -> None:
    seeded = await seed_catalog(session)
    source = seeded["source"]
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_settings] = lambda: Settings(SECRET_KEY=SECRET)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            f"/api/sources/{source.id}/export/reviews.csv",
            params={"sentiment": "negative", "min_rating": "1", "include_author": "true"},
        )
        too_many = await client.get(
            f"/api/sources/{source.id}/export/reviews.csv",
            params={"min_rating": "5", "max_rating": "2"},
        )

    rows = _csv_dicts(response.content)
    assert response.status_code == 200
    assert len(rows) == 2
    assert {row["sentiment"] for row in rows} == {"negative"}
    assert rows[0]["author"] == "Nguyễn Văn A"
    assert too_many.status_code == 422


# ---------------------------------------------------------------------------
# Production wiring: default get_session must stay open while streaming
# ---------------------------------------------------------------------------


async def test_default_get_session_streaming_response_completes(
    session: AsyncSession, report_database_url: str
) -> None:
    shop = Shop(
        id=uuid.uuid4(),
        platform="tiki",
        external_id="seller-wiring",
        name="Shop Wiring",
        url="https://tiki.vn/cua-hang/shop-wiring",
        raw={},
        crawled_at=NOW - timedelta(hours=1),
    )
    session.add(shop)
    await session.flush()
    product = _product("9001", "Sản phẩm wiring", shop_id=shop.id, review_count=600)
    session.add(product)
    await session.flush()
    source = _source(shop=shop, crawled_at=NOW - timedelta(hours=1))
    session.add(source)
    await session.commit()
    await _bulk_reviews(session, product, 600)

    Database().init(report_database_url)
    try:
        app = create_app()  # KHÔNG override get_session: dùng Database singleton
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", timeout=60.0
        ) as client:
            response = await client.get(f"/api/sources/{source.id}/export/reviews.csv")
            report_response = await client.get(f"/api/sources/{source.id}/report")

        assert response.status_code == 200
        assert len(_csv_dicts(response.content)) == 600
        assert report_response.status_code == 200
        assert report_response.json()["totals"]["reviews_collected"] == 600
    finally:
        await Database().dispose()

    conn = await asyncpg.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, database=TEST_DB_NAME
    )
    try:
        leftovers = await conn.fetchval(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = $1 AND state = 'idle in transaction'",
            TEST_DB_NAME,
        )
    finally:
        await conn.close()
    assert leftovers == 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli_env() -> dict[str, str]:
    return {
        **os.environ,
        "DATABASE_URL": TEST_DATABASE_URL,
        "SECRET_KEY": SECRET,
        "LOG_LEVEL": "WARNING",
    }


async def test_cli_prints_report_and_writes_csv(session: AsyncSession, tmp_path: Path) -> None:
    seeded = await seed_catalog(session)
    csv_dir = tmp_path / "exports"

    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "scripts/report.py", str(seeded["source"].id), "--csv-dir", str(csv_dir)],
        cwd=BACKEND_DIR,
        env=_cli_env(),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert f"SOURCE REPORT: {seeded['source'].id}" in result.stdout
    assert "Products: 7 (with price: 6)" in result.stdout
    assert "Sold total: 2,070" in result.stdout
    assert "Weighted rating: 4.78" in result.stdout
    assert "Reviews: collected=7 / reported=108 (coverage=6.5%)" in result.stdout
    assert "[PHAN BO SAO / RATING DISTRIBUTION]" in result.stdout
    assert "[CANH BAO / WARNINGS]" in result.stdout
    assert "[partial_reviews]" in result.stdout
    assert "[missing_price]" in result.stdout

    files = sorted(csv_dir.glob("*.csv"))
    assert len(files) == 2
    products_file = next(p for p in files if "-products-" in p.name)
    reviews_file = next(p for p in files if "-reviews-" in p.name)

    products_content = products_file.read_bytes()
    reviews_content = reviews_file.read_bytes()
    assert products_content.startswith(UTF8_BOM_BYTES)
    assert reviews_content.startswith(UTF8_BOM_BYTES)
    assert len(_csv_dicts(products_content)) == 7
    assert len(_csv_dicts(reviews_content)) == 7
    assert "Exported products CSV:" in result.stdout
    assert "Exported reviews CSV:" in result.stdout


async def test_cli_json_flag_and_argument_errors(session: AsyncSession, tmp_path: Path) -> None:
    seeded = await seed_catalog(session)

    ok = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "scripts/report.py", str(seeded["source"].id), "--json"],
        cwd=BACKEND_DIR,
        env=_cli_env(),
        capture_output=True,
        text=True,
    )
    assert ok.returncode == 0, ok.stderr
    payload = json.loads(ok.stdout)
    assert payload["totals"]["products"] == 7
    assert payload["rankings"]["cheapest"][0]["value"] == pytest.approx(20000.0)

    bad_uuid = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "scripts/report.py", "not-a-uuid"],
        cwd=BACKEND_DIR,
        env=_cli_env(),
        capture_output=True,
        text=True,
    )
    assert bad_uuid.returncode == 2
    assert "Invalid source_id UUID" in bad_uuid.stderr

    unknown = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "scripts/report.py", str(uuid.uuid4())],
        cwd=BACKEND_DIR,
        env=_cli_env(),
        capture_output=True,
        text=True,
    )
    assert unknown.returncode == 1
    assert "not found" in unknown.stderr

    bad_top_n = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "scripts/report.py", str(seeded["source"].id), "--top-n", "0"],
        cwd=BACKEND_DIR,
        env=_cli_env(),
        capture_output=True,
        text=True,
    )
    assert bad_top_n.returncode == 2
    assert "--top-n must be between 1 and 50" in bad_top_n.stderr
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# Agent tool tie-break uses the same effective rating count
# ---------------------------------------------------------------------------


async def test_agent_query_products_rating_sort_tiebreaks_on_effective_count(
    session: AsyncSession,
) -> None:
    seeded = await seed_catalog(session)
    products = list(seeded["products"].values())
    # effective: 1006 = 3 (rating_count), 1001 = 100 (review_count)
    session.add(
        _product(
            "1010",
            "Gạo cùng điểm",
            shop_id=seeded["shop"].id,
            sold_count=10,
            rating_avg=5.0,
            rating_count=None,
            review_count=7,
        )
    )
    await session.commit()
    products = list((await session.execute(select(Product))).scalars().all())

    ctx = AgentContext(
        session_factory=session_factory_for(session),
        source=seeded["source"],
        shop=seeded["shop"],
        products=products,
        embedder=None,
        llm_client=None,  # type: ignore[arg-type]
        citations=CitationRegistry(),
    )
    result = await _run_query_products(
        ctx, ArgsQueryProducts(sort_by="rating_avg", order="desc", limit=10)
    )

    assert result.ok is True
    ranked = [item["external_id"] for item in result.data["items"]]
    # 5.0 sao: 1002 (10 phiếu) -> 1010 (7 phiếu) -> 1006 (3 phiếu); 1007 (2) xuống sau.
    assert ranked[:4] == ["1002", "1010", "1006", "1007"]
    assert ranked.index("1006") < ranked.index("1001")


def session_factory_for(session: AsyncSession) -> Any:
    """Wrap an already-open session in the async-context-manager shape tools expect."""

    class _Factory:
        def __call__(self) -> Any:
            return _Ctx(session)

    class _Ctx:
        def __init__(self, active: AsyncSession) -> None:
            self._active = active

        async def __aenter__(self) -> AsyncSession:
            return self._active

        async def __aexit__(self, *_exc: Any) -> None:
            return None

    return _Factory()
