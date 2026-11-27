"""Integration tests against a **dedicated** Postgres database.

The project database (``shopqa``) already holds real crawled data, so these
tests create and use a separate database (``shopqa_phase2_test`` by default)
and truncate **only** that one. They skip automatically when Postgres is down.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.crawl.service as service_module
from app.crawl.adapters.base import AdapterRegistry
from app.crawl.errors import NotFoundError
from app.crawl.repository import (
    create_source,
    update_source,
    upsert_product,
    upsert_reviews,
    upsert_shop,
)
from app.crawl.service import crawl_url
from app.db.models import Product, Review, Shop, Source
from tests.crawl_fakes import FakeAdapter, make_product, make_review, make_shop

pytestmark = pytest.mark.integration

BACKEND_DIR = Path(__file__).resolve().parents[1]

PG_HOST = os.environ.get("TEST_PHASE2_PG_HOST", "localhost")
PG_PORT = int(os.environ.get("TEST_PHASE2_PG_PORT", "5433"))
PG_USER = os.environ.get("TEST_PHASE2_PG_USER", "shopqa")
PG_PASSWORD = os.environ.get("TEST_PHASE2_PG_PASSWORD", "shopqa")
PG_MAINTENANCE_DB = os.environ.get("TEST_PHASE2_PG_MAINTENANCE_DB", "shopqa")
TEST_DB_NAME = os.environ.get("TEST_PHASE2_DB_NAME", "shopqa_phase2_test")

PROJECT_DB_NAME = "shopqa"  # never touched: holds real crawl data
MAINTENANCE_DSN = f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{PG_MAINTENANCE_DB}"
TEST_DSN = f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{TEST_DB_NAME}"
TEST_DATABASE_URL = (
    f"postgresql+asyncpg://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{TEST_DB_NAME}"
)

PRODUCT_URL = "https://tiki.vn/integration-product-p9001.html"


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
            # Identifiers cannot be parameterised; TEST_DB_NAME is our own constant.
            await conn.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def phase2_database_url() -> str:
    """Create the isolated database if needed and run ``alembic upgrade head`` on it."""
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
async def session_factory(phase2_database_url: str) -> AsyncIterator[Any]:
    """Function-scoped engine + clean crawl tables in the dedicated test database."""
    engine = create_async_engine(phase2_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            current = (await conn.execute(text("SELECT current_database()"))).scalar_one()
            assert current == TEST_DB_NAME
            await conn.execute(text("TRUNCATE reviews, products, shops, sources CASCADE"))
        yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _count(session_factory: Any, model: type) -> int:
    async with session_factory() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar_one()


# ---------------------------------------------------------------------------
# Migration 0002
# ---------------------------------------------------------------------------


async def test_migration_0002_creates_tables(session_factory: Any) -> None:
    async with session_factory() as session:
        tables = {
            row[0]
            for row in await session.execute(
                text(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
                )
            )
        }
        revision = (await session.execute(text("SELECT version_num FROM alembic_version"))).scalar()

    assert {"sources", "shops", "products", "reviews"} <= tables
    # Later phases keep stacking migrations (this fixture runs ``upgrade head``); 0006 adds
    # ``products.rating_breakdown`` and none of the crawl tables from 0002 disappear.
    assert revision == "0006_product_rating_breakdown"


async def test_migration_0002_creates_expected_constraints_and_indexes(
    session_factory: Any,
) -> None:
    async with session_factory() as session:
        unique_constraints = {
            row[0]
            for row in await session.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE contype = 'u' AND conrelid::regclass::text IN "
                    "('shops', 'products', 'reviews')"
                )
            )
        }
        check_constraints = {
            row[0]
            for row in await session.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE contype = 'c' AND conrelid = 'sources'::regclass"
                )
            )
        }
        delete_rules = {
            row[0]: row[1]
            for row in await session.execute(
                text(
                    "SELECT conrelid::regclass::text, confdeltype::text FROM pg_constraint "
                    "WHERE contype = 'f'"
                )
            )
        }
        indexes = {
            row[0]
            for row in await session.execute(
                text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
            )
        }
        product_columns = {
            row[0]
            for row in await session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'products'"
                )
            )
        }

    assert unique_constraints == {
        "uq_shops_platform_external_id",
        "uq_products_platform_external_id",
        "uq_reviews_product_external_id",
    }
    assert check_constraints == {"ck_sources_url_type", "ck_sources_status"}
    assert delete_rules["reviews"] == "c"  # ON DELETE CASCADE
    assert delete_rules["products"] == "n"  # ON DELETE SET NULL
    assert {
        "ix_products_shop_id",
        "ix_products_platform_sold_count_desc",
        "ix_reviews_product_id_rating",
    } <= indexes
    assert {
        "price",
        "list_price",
        "sold_count",
        "sold_text",
        "rating_avg",
        "rating_count",
        "review_count",
        "category",
        "brand",
        "short_description",
        "description_html",
        "description_text",
        "specs",
        "images",
        "raw",
        "crawled_at",
    } <= product_columns


# ---------------------------------------------------------------------------
# Repository: upsert semantics
# ---------------------------------------------------------------------------


async def test_upsert_product_is_idempotent(session_factory: Any) -> None:
    product = make_product("9001", price="100000")

    async with session_factory() as session:
        first = await upsert_product(session, product)
        await session.commit()
        first_id = first.id
        first_crawled_at = first.crawled_at

    updated = make_product("9001", price="123456", title="Tên mới", sold_count=99)
    async with session_factory() as session:
        second = await upsert_product(session, updated)
        await session.commit()

    assert second.id == first_id
    assert second.crawled_at >= first_crawled_at
    assert await _count(session_factory, Product) == 1

    async with session_factory() as session:
        row = (
            await session.execute(select(Product).where(Product.external_id == "9001"))
        ).scalar_one()
        assert row.price == Decimal("123456")
        assert row.title == "Tên mới"
        assert row.sold_count == 99
        assert row.url == "https://tiki.vn/san-pham-9001.html"
        assert row.raw == {"id": "9001"}


async def test_upsert_shop_preserves_enrichment_when_payload_is_sparse(
    session_factory: Any,
) -> None:
    full = make_shop("seller-1", name="Shop A", rating=4.7, follower_count=1234)
    sparse = make_shop("seller-1", name="Shop A renamed", rating=None, follower_count=None)

    async with session_factory() as session:
        await upsert_shop(session, full)
        await session.commit()

    async with session_factory() as session:
        await upsert_shop(session, sparse)
        await session.commit()

    assert await _count(session_factory, Shop) == 1
    async with session_factory() as session:
        row = (
            await session.execute(select(Shop).where(Shop.external_id == "seller-1"))
        ).scalar_one()
        assert row.name == "Shop A renamed"
        assert row.rating == pytest.approx(4.7)
        assert row.follower_count == 1234


async def test_upsert_reviews_dedupes_within_one_batch(session_factory: Any) -> None:
    async with session_factory() as session:
        product = await upsert_product(session, make_product("9002"))
        await session.commit()
        product_id = product.id

    batch = [
        make_review("r1", content="first"),
        make_review("r2"),
        make_review("r1", content="last wins"),
    ]
    async with session_factory() as session:
        written = await upsert_reviews(session, product_id, batch)
        await session.commit()

    assert written == 2
    assert await _count(session_factory, Review) == 2
    async with session_factory() as session:
        r1 = (
            await session.execute(
                select(Review).where(Review.product_id == product_id, Review.external_id == "r1")
            )
        ).scalar_one()
        assert r1.content == "last wins"


async def test_upsert_reviews_updates_existing_rows(session_factory: Any) -> None:
    async with session_factory() as session:
        product = await upsert_product(session, make_product("9003"))
        await upsert_reviews(session, product.id, [make_review("r1", content="old")])
        await session.commit()
        product_id = product.id

    async with session_factory() as session:
        await upsert_reviews(session, product_id, [make_review("r1", content="new", rating=3)])
        await session.commit()

    assert await _count(session_factory, Review) == 1
    async with session_factory() as session:
        review = (await session.execute(select(Review))).scalar_one()
        assert review.content == "new"
        assert review.rating == 3


async def test_upsert_reviews_with_empty_list_is_a_noop(session_factory: Any) -> None:
    async with session_factory() as session:
        product = await upsert_product(session, make_product("9004"))
        await session.commit()
        written = await upsert_reviews(session, product.id, [])

    assert written == 0
    assert await _count(session_factory, Review) == 0


# ---------------------------------------------------------------------------
# Constraints and cascades
# ---------------------------------------------------------------------------


async def test_duplicate_product_insert_violates_unique_constraint(
    session_factory: Any,
) -> None:
    async with session_factory() as session:
        await upsert_product(session, make_product("9005"))
        await session.commit()

    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO products "
                    "(id, platform, external_id, title, url, currency, "
                    "specs, images, raw, crawled_at) "
                    "VALUES (:id, 'tiki', '9005', 'dup', 'https://x', 'VND', "
                    "'{}'::jsonb, '[]'::jsonb, '{}'::jsonb, now())"
                ),
                {"id": uuid.uuid4()},
            )
    assert await _count(session_factory, Product) == 1


async def test_duplicate_review_insert_violates_unique_constraint(
    session_factory: Any,
) -> None:
    async with session_factory() as session:
        product = await upsert_product(session, make_product("9006"))
        await upsert_reviews(session, product.id, [make_review("r1")])
        await session.commit()
        product_id = product.id

    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO reviews "
                    "(id, product_id, external_id, rating, images, raw, crawled_at) "
                    "VALUES (:id, :product_id, 'r1', 5, '[]'::jsonb, '{}'::jsonb, now())"
                ),
                {"id": uuid.uuid4(), "product_id": product_id},
            )


async def test_deleting_product_cascades_to_reviews(session_factory: Any) -> None:
    async with session_factory() as session:
        product = await upsert_product(session, make_product("9007"))
        await upsert_reviews(session, product.id, [make_review("r1"), make_review("r2")])
        await session.commit()
        product_id = product.id

    assert await _count(session_factory, Review) == 2

    async with session_factory() as session:
        await session.execute(text("DELETE FROM products WHERE id = :id"), {"id": product_id})
        await session.commit()

    assert await _count(session_factory, Review) == 0


async def test_deleting_shop_sets_product_shop_id_to_null(session_factory: Any) -> None:
    async with session_factory() as session:
        shop = await upsert_shop(session, make_shop("seller-9"))
        product = await upsert_product(session, make_product("9008"), shop_id=shop.id)
        await session.commit()
        shop_id, product_id = shop.id, product.id

    async with session_factory() as session:
        await session.execute(text("DELETE FROM shops WHERE id = :id"), {"id": shop_id})
        await session.commit()

    async with session_factory() as session:
        row = (await session.execute(select(Product).where(Product.id == product_id))).scalar_one()
        assert row.shop_id is None


async def test_source_status_check_constraint_rejects_unknown_status(
    session_factory: Any,
) -> None:
    async with session_factory() as session:
        source = await create_source(session, url="https://tiki.vn/x-p1.html")
        await session.commit()
        source_id = source.id

    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text("UPDATE sources SET status = 'exploded' WHERE id = :id"),
                {"id": source_id},
            )


async def test_source_lifecycle_round_trip(session_factory: Any) -> None:
    async with session_factory() as session:
        source = await create_source(
            session,
            url=PRODUCT_URL,
            status="running",
            platform="tiki",
            external_ref={"product_id": "9001"},
        )
        await session.commit()
        source_id = source.id

    async with session_factory() as session:
        await update_source(
            session,
            source_id,
            url_type="product",
            status="done",
            stats={"products_total": 1, "reviews_count": 2},
            crawled_at=None,
        )
        await session.commit()

    async with session_factory() as session:
        row = (await session.execute(select(Source).where(Source.id == source_id))).scalar_one()
        assert row.status == "done"
        assert row.url_type == "product"
        assert row.platform == "tiki"
        assert row.external_ref == {"product_id": "9001"}
        assert row.stats["reviews_count"] == 2


# ---------------------------------------------------------------------------
# Service end-to-end against real SQL
# ---------------------------------------------------------------------------


def _registry(adapter: FakeAdapter) -> AdapterRegistry:
    registry = AdapterRegistry()
    registry.register(adapter)
    return registry


async def test_service_product_crawl_persists_and_is_idempotent(session_factory: Any) -> None:
    shop = make_shop("seller-1", name="Shop E2E")
    adapter = FakeAdapter(
        default_product=make_product("9001", price="100000", shop=shop),
        default_reviews=[make_review("r1"), make_review("r2")],
    )

    first = await crawl_url(
        PRODUCT_URL, session_factory=session_factory, registry=_registry(adapter)
    )

    assert first.status == "done"
    assert await _count(session_factory, Source) == 1
    assert await _count(session_factory, Product) == 1
    assert await _count(session_factory, Review) == 2
    assert await _count(session_factory, Shop) == 1

    async with session_factory() as session:
        product = (await session.execute(select(Product))).scalar_one()
        assert product.shop_id == first.shop_id
        assert product.price == Decimal("100000")

    # Second run of the same URL must update, not duplicate.
    adapter2 = FakeAdapter(
        default_product=make_product("9001", price="99999", title="Giá mới", shop=shop),
        default_reviews=[make_review("r1", content="updated"), make_review("r3")],
    )
    second = await crawl_url(
        PRODUCT_URL, session_factory=session_factory, registry=_registry(adapter2)
    )

    assert second.status == "done"
    assert await _count(session_factory, Product) == 1
    assert await _count(session_factory, Review) == 3
    assert await _count(session_factory, Shop) == 1
    # Each run creates its own source row.
    assert await _count(session_factory, Source) == 2

    async with session_factory() as session:
        product = (await session.execute(select(Product))).scalar_one()
        assert product.price == Decimal("99999")
        assert product.title == "Giá mới"

    async with session_factory() as session:
        sources = (
            (await session.execute(select(Source).order_by(Source.created_at))).scalars().all()
        )
        assert [source.status for source in sources] == ["done", "done"]
        assert sources[-1].stats["reviews_count"] == 2
        assert sources[-1].url_type == "product"


async def test_service_shop_crawl_persists_products_and_reruns_cleanly(
    session_factory: Any,
) -> None:
    ids = ("9101", "9102")
    adapter = FakeAdapter(
        shop=make_shop("seller-7", name="Shop 7"),
        listings=[make_product(pid) for pid in ids],
        products={pid: make_product(pid) for pid in ids},
        default_reviews=[make_review("r1")],
    )

    first = await crawl_url(
        "https://tiki.vn/cua-hang/shop-7",
        session_factory=session_factory,
        registry=_registry(adapter),
        max_products=2,
        max_reviews=1,
    )

    assert first.status == "done"
    assert await _count(session_factory, Product) == 2
    assert await _count(session_factory, Review) == 2
    assert await _count(session_factory, Shop) == 1

    async with session_factory() as session:
        products = (await session.execute(select(Product))).scalars().all()
        assert {product.external_id for product in products} == set(ids)
        assert all(product.shop_id == first.shop_id for product in products)

    rerun = await crawl_url(
        "https://tiki.vn/cua-hang/shop-7",
        session_factory=session_factory,
        registry=_registry(adapter),
        max_products=2,
        max_reviews=1,
    )

    assert rerun.status == "done"
    assert await _count(session_factory, Product) == 2
    assert await _count(session_factory, Review) == 2
    assert await _count(session_factory, Shop) == 1


async def test_service_partial_run_records_errors_and_keeps_good_products(
    session_factory: Any,
) -> None:
    adapter = FakeAdapter(
        shop=make_shop("seller-8"),
        listings=[make_product("9201"), make_product("9202")],
        products={"9201": make_product("9201"), "9202": NotFoundError("gone")},
        default_reviews=[],
    )

    result = await crawl_url(
        "https://tiki.vn/cua-hang/shop-8",
        session_factory=session_factory,
        registry=_registry(adapter),
    )

    assert result.status == "partial"
    assert await _count(session_factory, Product) == 1

    async with session_factory() as session:
        source = (await session.execute(select(Source))).scalar_one()
        assert source.status == "partial"
        assert source.stats["products_failed"] == 1
        assert source.stats["errors"][0]["product_id"] == "9202"


async def test_service_failure_persists_failed_source(session_factory: Any) -> None:
    adapter = FakeAdapter(default_product=NotFoundError("product vanished"))

    result = await crawl_url(
        PRODUCT_URL, session_factory=session_factory, registry=_registry(adapter)
    )

    assert result.status == "failed"
    async with session_factory() as session:
        source = (await session.execute(select(Source))).scalar_one()
        assert source.status == "failed"
        assert "product vanished" in (source.error or "")
        assert source.crawled_at is None
    assert await _count(session_factory, Product) == 0


async def test_service_dry_run_never_touches_the_database(session_factory: Any) -> None:
    adapter = FakeAdapter(
        default_product=make_product("9301"),
        default_reviews=[make_review("r1")],
    )

    result = await crawl_url(
        PRODUCT_URL,
        session_factory=session_factory,
        registry=_registry(adapter),
        dry_run=True,
    )

    assert result.preview_product is not None
    assert await _count(session_factory, Source) == 0
    assert await _count(session_factory, Product) == 0
    assert await _count(session_factory, Review) == 0


def test_service_module_imports_real_repository() -> None:
    """Guard: these tests exercise the real repository, not the stubbed one."""
    assert service_module.upsert_product is upsert_product
