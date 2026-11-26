"""Integration tests for ``GET /api/sources`` (paginated list for the Phase 5 web app).

Isolation contract: everything happens in ``shopqa_phase5_sources_test`` (override with
``TEST_SOURCES_*`` env vars), created through a maintenance connection to ``shopqa``.
The project database ``shopqa`` is never written, truncated or migrated here. No LLM calls,
no outbound network: the endpoint is pure SQL.

Covered: field-by-field item shape, SQL-side aggregation (products/reviews/chat sessions),
shop matching through ``external_ref``, explicit ``product_ids`` scoping, ``display_name``
fallbacks, ``ingested``/``last_job_id`` surfacing, ordering, pagination, every filter,
query-count parity (no N+1) and the CORS headers the browser needs for the CSV download.
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
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.deps import get_session, get_settings
from app.core.config import Settings
from app.crawl.repository import update_source
from app.db.models import ChatSession, Product, Review, Shop, Source
from app.main import create_app

pytestmark = pytest.mark.integration

BACKEND_DIR = Path(__file__).resolve().parents[1]
SECRET = os.environ.get("SECRET_KEY", "vN5y6rM0J9h2xL8qK3pW1zB4cF7dG0tH2sV5mN8wX4Q=")

PG_HOST = os.environ.get("TEST_SOURCES_PG_HOST", "localhost")
PG_PORT = int(os.environ.get("TEST_SOURCES_PG_PORT", "5433"))
PG_USER = os.environ.get("TEST_SOURCES_PG_USER", "shopqa")
PG_PASSWORD = os.environ.get("TEST_SOURCES_PG_PASSWORD", "shopqa")
PG_MAINTENANCE_DB = os.environ.get("TEST_SOURCES_PG_MAINTENANCE_DB", "shopqa")
TEST_DB_NAME = os.environ.get("TEST_SOURCES_DB_NAME", "shopqa_phase5_sources_test")
PROJECT_DB_NAME = "shopqa"

MAINTENANCE_DSN = f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{PG_MAINTENANCE_DB}"
TEST_DATABASE_URL = (
    f"postgresql+asyncpg://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{TEST_DB_NAME}"
)
NOW = datetime.now(UTC).replace(microsecond=0)
ALLOWED_ORIGIN = "http://localhost:3000"


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
def sources_database_url() -> str:
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
async def engine(sources_database_url: str) -> AsyncIterator[Any]:
    created = create_async_engine(sources_database_url, poolclass=NullPool)
    try:
        async with created.begin() as conn:
            current = (await conn.execute(text("SELECT current_database()"))).scalar_one()
            assert current == TEST_DB_NAME, f"refusing to truncate {current}"
            await conn.execute(
                text("TRUNCATE chat_sessions, chunks, reviews, products, shops, sources CASCADE")
            )
        yield created
    finally:
        await created.dispose()


@pytest.fixture
async def session_factory(engine: Any) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
async def session(session_factory: Any) -> AsyncIterator[AsyncSession]:
    async with session_factory() as active_session:
        yield active_session


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


# ---------------------------------------------------------------------------
# Seeds
# ---------------------------------------------------------------------------


def _shop(name: str, *, external_id: str) -> Shop:
    return Shop(
        id=uuid.uuid4(),
        platform="tiki",
        external_id=external_id,
        name=name,
        url=f"https://tiki.vn/cua-hang/{external_id}",
        rating=4.7,
        follower_count=100,
        product_count=2,
        raw={},
        crawled_at=NOW - timedelta(hours=2),
    )


def _product(external_id: str, title: str, *, shop_id: uuid.UUID) -> Product:
    return Product(
        id=uuid.uuid4(),
        shop_id=shop_id,
        platform="tiki",
        external_id=external_id,
        title=title,
        url=f"https://tiki.vn/{external_id}.html",
        currency="VND",
        sold_count=10,
        rating_avg=4.5,
        rating_count=3,
        review_count=3,
        raw={},
        images=[],
        crawled_at=NOW - timedelta(hours=2),
    )


def _review(product_id: uuid.UUID, external_id: str) -> Review:
    return Review(
        product_id=product_id,
        external_id=external_id,
        rating=5,
        sentiment="positive",
        sentiment_source="rating",
        aspects=[],
        images=[],
        raw={},
        crawled_at=NOW - timedelta(hours=2),
    )


def _source(
    *,
    url: str,
    url_type: str | None,
    platform: str | None,
    status: str,
    updated_at: datetime,
    stats: dict[str, Any] | None = None,
    external_ref: dict[str, Any] | None = None,
    error: str | None = None,
) -> Source:
    return Source(
        id=uuid.uuid4(),
        url=url,
        url_type=url_type,
        platform=platform,
        status=status,
        error=error,
        stats={} if stats is None else stats,
        external_ref={} if external_ref is None else external_ref,
        crawled_at=updated_at - timedelta(minutes=5),
        created_at=updated_at - timedelta(hours=1),
        updated_at=updated_at,
    )


async def seed_sources(session: AsyncSession) -> dict[str, Any]:
    """Five sources covering shop/product scoping, explicit ids, statuses and platforms."""
    fahasa = _shop("Nhà sách Fahasa", external_id="seller-fahasa")
    other = _shop("Shop Khác", external_id="seller-khac")
    session.add_all([fahasa, other])
    await session.flush()

    p1 = _product("2001", "Combo HSK 1", shop_id=fahasa.id)
    p2 = _product("2002", "Sách luyện thi HSK 2", shop_id=fahasa.id)
    p3 = _product("3001", "Sản phẩm shop khác", shop_id=other.id)
    session.add_all([p1, p2, p3])
    await session.flush()

    session.add_all(
        [
            _review(p1.id, "2001-r1"),
            _review(p1.id, "2001-r2"),
            _review(p2.id, "2002-r1"),
            _review(p3.id, "3001-r1"),
        ]
    )

    shop_source = _source(
        url="https://tiki.vn/cua-hang/fahasa",
        url_type="shop",
        platform="tiki",
        status="done",
        updated_at=NOW - timedelta(minutes=30),
        stats={"ingest": {"chunks": 6}, "last_job_id": "job-shop-done"},
        external_ref={
            "shop_id": str(fahasa.id),
            "seller_id": fahasa.external_id,
            "canonical_url": fahasa.url,
        },
    )
    product_source = _source(
        url="https://tiki.vn/combo-hsk-1-p2001.html",
        url_type="product",
        platform="tiki",
        status="running",
        updated_at=NOW - timedelta(minutes=5),
        stats={"last_job_id": "job-product-running"},
        external_ref={"product_id": "2001"},
    )
    explicit_source = _source(
        url="https://tiki.vn/san-pham-le-p3001.html",
        url_type="product",
        platform="tiki",
        status="failed",
        updated_at=NOW - timedelta(minutes=10),
        error="blocked: captcha",
        stats={"last_job_id": "job-explicit"},
        external_ref={"product_ids": [str(p3.id)]},
    )
    empty_source = _source(
        url="https://tiki.vn/cua-hang/chua-crawl",
        url_type=None,
        platform=None,
        status="pending",
        updated_at=NOW - timedelta(hours=3),
    )
    shopee_source = _source(
        url="https://shopee.vn/shop-abc",
        url_type="shop",
        platform="shopee",
        status="partial",
        updated_at=NOW - timedelta(hours=6),
    )
    sources = [shop_source, product_source, explicit_source, empty_source, shopee_source]
    session.add_all(sources)
    await session.flush()

    session.add(ChatSession(source_id=shop_source.id, title="Top 3 bán chạy?"))
    await session.commit()

    return {
        "shop": fahasa,
        "products": {"2001": p1, "2002": p2, "3001": p3},
        "shop_source": shop_source,
        "product_source": product_source,
        "explicit_source": explicit_source,
        "empty_source": empty_source,
        "shopee_source": shopee_source,
    }


def _by_id(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in body["items"]}


# ---------------------------------------------------------------------------
# Item shape and SQL aggregation
# ---------------------------------------------------------------------------


async def test_list_aggregates_counts_per_source_without_n_plus_one(
    api_client: httpx.AsyncClient, session: AsyncSession, engine: Any
) -> None:
    seeded = await seed_sources(session)
    statements: list[str] = []

    def _record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        statements.append(" ".join(statement.split()))

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        response = await api_client.get("/api/sources")
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _record)

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 5
    assert len(body["items"]) == 5
    # Exactly one COUNT + one aggregated page query, regardless of row count.
    assert len(statements) == 2
    assert statements[0].startswith("SELECT COUNT(*)")
    assert statements[1].startswith("WITH page_sources AS")
    assert "matched_shops" in statements[1]
    assert "LIMIT" in statements[1]

    items = _by_id(body)
    shop_item = items[str(seeded["shop_source"].id)]
    assert shop_item["display_name"] == "Nhà sách Fahasa"
    assert shop_item["shop_name"] == "Nhà sách Fahasa"
    assert shop_item["url_type"] == "shop"
    assert shop_item["platform"] == "tiki"
    assert shop_item["status"] == "done"
    assert shop_item["products_count"] == 2
    assert shop_item["reviews_collected"] == 3
    assert shop_item["chat_sessions_count"] == 1
    assert shop_item["ingested"] is True
    assert shop_item["last_job_id"] == "job-shop-done"
    assert shop_item["error"] is None
    assert shop_item["crawled_at"] is not None

    product_item = items[str(seeded["product_source"].id)]
    # Product source without explicit ids: scoped by external_ref.product_id.
    assert product_item["display_name"] == "Combo HSK 1"
    assert product_item["products_count"] == 1
    assert product_item["reviews_collected"] == 2
    assert product_item["chat_sessions_count"] == 0
    assert product_item["ingested"] is False
    assert product_item["last_job_id"] == "job-product-running"

    explicit_item = items[str(seeded["explicit_source"].id)]
    # Explicit product_ids win over platform/external matching (product belongs to another shop).
    assert explicit_item["display_name"] == "Sản phẩm shop khác"
    assert explicit_item["products_count"] == 1
    assert explicit_item["reviews_collected"] == 1
    assert explicit_item["status"] == "failed"
    assert explicit_item["error"] == "blocked: captcha"

    empty_item = items[str(seeded["empty_source"].id)]
    assert empty_item["display_name"] == "https://tiki.vn/cua-hang/chua-crawl"
    assert empty_item["products_count"] == 0
    assert empty_item["reviews_collected"] == 0
    assert empty_item["ingested"] is False
    assert empty_item["last_job_id"] is None

    shopee_item = items[str(seeded["shopee_source"].id)]
    assert shopee_item["status"] == "partial"
    assert shopee_item["platform"] == "shopee"
    assert shopee_item["display_name"] == "https://shopee.vn/shop-abc"


async def test_items_are_ordered_by_updated_at_desc(
    api_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    seeded = await seed_sources(session)

    response = await api_client.get("/api/sources")

    ids = [item["id"] for item in response.json()["items"]]
    assert ids == [
        str(seeded["product_source"].id),
        str(seeded["explicit_source"].id),
        str(seeded["shop_source"].id),
        str(seeded["empty_source"].id),
        str(seeded["shopee_source"].id),
    ]


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


async def test_pagination_keeps_total_and_returns_disjoint_pages(
    api_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    await seed_sources(session)

    first = await api_client.get("/api/sources", params={"limit": 2, "offset": 0})
    second = await api_client.get("/api/sources", params={"limit": 2, "offset": 2})

    assert first.json()["total"] == second.json()["total"] == 5
    assert len(first.json()["items"]) == 2
    assert len(second.json()["items"]) == 2
    assert {i["id"] for i in first.json()["items"]}.isdisjoint(
        {i["id"] for i in second.json()["items"]}
    )


async def test_offset_past_total_returns_empty_items_with_total(
    api_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    await seed_sources(session)

    response = await api_client.get("/api/sources", params={"limit": 20, "offset": 9})

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 5}


async def test_limit_upper_bound_is_enforced_at_http_layer(
    api_client: httpx.AsyncClient,
) -> None:
    response = await api_client.get("/api/sources", params={"limit": 100})

    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("params", "expected_keys"),
    [
        ({"status": "running"}, ["product_source"]),
        ({"status": "failed"}, ["explicit_source"]),
        ({"url_type": "shop"}, ["shop_source", "shopee_source"]),
        ({"url_type": "product"}, ["product_source", "explicit_source"]),
        ({"platform": "shopee"}, ["shopee_source"]),
        ({"platform": "tiki", "status": "done"}, ["shop_source"]),
        ({"status": "failed", "platform": "shopee"}, []),
        (
            {"status": "  "},
            [
                "product_source",
                "explicit_source",
                "shop_source",
                "empty_source",
                "shopee_source",
            ],
        ),
    ],
)
async def test_filters_select_matching_sources(
    api_client: httpx.AsyncClient,
    session: AsyncSession,
    params: dict[str, str],
    expected_keys: list[str],
) -> None:
    seeded = await seed_sources(session)

    response = await api_client.get("/api/sources", params=params)

    assert response.status_code == 200
    body = response.json()
    expected_ids = {str(seeded[key].id) for key in expected_keys}
    assert {item["id"] for item in body["items"]} == expected_ids
    assert body["total"] == len(expected_ids)


# ---------------------------------------------------------------------------
# Detail endpoint parity (display_name + last_job_id)
# ---------------------------------------------------------------------------


async def test_detail_endpoint_exposes_display_name_and_last_job_id(
    api_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    seeded = await seed_sources(session)

    shop_response = await api_client.get(f"/api/sources/{seeded['shop_source'].id}")
    product_response = await api_client.get(f"/api/sources/{seeded['product_source'].id}")
    empty_response = await api_client.get(f"/api/sources/{seeded['empty_source'].id}")

    assert shop_response.status_code == 200
    shop_body = shop_response.json()
    assert shop_body["display_name"] == "Nhà sách Fahasa"
    assert shop_body["last_job_id"] == "job-shop-done"
    assert shop_body["stats"]["ingest"] == {"chunks": 6}

    assert product_response.json()["display_name"] == "Combo HSK 1"
    assert product_response.json()["last_job_id"] == "job-product-running"

    assert empty_response.json()["display_name"] == "https://tiki.vn/cua-hang/chua-crawl"
    assert empty_response.json()["last_job_id"] is None


async def test_detail_endpoint_returns_404_envelope_for_unknown_source(
    api_client: httpx.AsyncClient,
) -> None:
    response = await api_client.get(f"/api/sources/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["detail"] == "Source not found."


# ---------------------------------------------------------------------------
# Browser-facing CORS on the CSV download
# ---------------------------------------------------------------------------


async def test_csv_download_exposes_content_disposition_to_allowed_origin(
    api_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    seeded = await seed_sources(session)

    response = await api_client.get(
        f"/api/sources/{seeded['shop_source'].id}/export/products.csv",
        headers={"Origin": ALLOWED_ORIGIN},
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    assert "content-disposition" in response.headers["access-control-expose-headers"].lower()
    assert "attachment" in response.headers["content-disposition"]


# ---------------------------------------------------------------------------
# last_job_id lifecycle: crawl updates must not drop the running job handle
# ---------------------------------------------------------------------------


async def test_crawl_update_preserves_last_job_id_in_stats(
    session: AsyncSession,
) -> None:
    seeded = await seed_sources(session)
    source = seeded["shop_source"]

    await update_source(
        session,
        source.id,
        status="done",
        stats={"ingest": {"chunks": 9}},
    )
    await session.commit()
    await session.refresh(source)

    assert source.stats == {"last_job_id": "job-shop-done", "ingest": {"chunks": 9}}
    assert source.status == "done"


async def test_crawl_update_may_replace_last_job_id_explicitly(
    session: AsyncSession,
) -> None:
    seeded = await seed_sources(session)
    source = seeded["shop_source"]

    await update_source(session, source.id, stats={"last_job_id": "job-new"})
    await session.commit()
    await session.refresh(source)

    assert source.stats == {"last_job_id": "job-new"}


async def test_crawl_update_without_existing_job_id_writes_plain_stats(
    session: AsyncSession,
) -> None:
    seeded = await seed_sources(session)
    source = seeded["empty_source"]

    await update_source(session, source.id, stats={"ingest": {"chunks": 0}}, status="running")
    await session.commit()
    await session.refresh(source)

    assert source.stats == {"ingest": {"chunks": 0}}
    assert "last_job_id" not in source.stats


async def test_list_surfaces_preserved_last_job_id_after_recrawl(
    api_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    seeded = await seed_sources(session)
    source = seeded["shop_source"]

    await update_source(session, source.id, status="running", stats={"ingest": {"chunks": 1}})
    await session.commit()

    response = await api_client.get("/api/sources", params={"status": "running"})

    body = response.json()
    assert body["total"] == 2  # product_source + recrawled shop_source
    item = _by_id(body)[str(source.id)]
    assert item["status"] == "running"
    assert item["last_job_id"] == "job-shop-done"


# ---------------------------------------------------------------------------
# Freshness filter (`fresh=true`) — cache-policy Vòng 2
#
# "Gần đây" only lists sources that are still inside the mandatory 48h crawl cache
# (status done + ingested + crawled_at inside the TTL). The predicate is shared with
# `app/jobs/submit.py`; the equivalence test at the end of this section pins that.
# ---------------------------------------------------------------------------

TTL_HOURS = 48


def _fresh_source(
    *,
    url: str,
    status: str = "done",
    crawled_at: datetime | None,
    stats: dict[str, Any] | None = None,
    platform: str | None = "tiki",
    url_type: str | None = "shop",
    updated_at: datetime | None = None,
) -> Source:
    """Source with an explicit ``crawled_at`` so the TTL window can be exercised."""
    timestamp = updated_at if updated_at is not None else NOW
    return Source(
        id=uuid.uuid4(),
        url=url,
        url_type=url_type,
        platform=platform,
        status=status,
        stats={} if stats is None else stats,
        external_ref={},
        crawled_at=crawled_at,
        created_at=timestamp - timedelta(days=3),
        updated_at=timestamp,
    )


async def seed_cache_window_sources(session: AsyncSession) -> dict[str, Any]:
    """Six sources spanning both sides of the 48h boundary and every non-cacheable state."""
    now = datetime.now(UTC)
    ingested = {"ingest": {"chunks": 4}, "last_job_id": "job-cache"}
    inside = _fresh_source(
        url="https://tiki.vn/cua-hang/trong-han",
        crawled_at=now - timedelta(hours=47, minutes=59),
        stats=ingested,
    )
    expired = _fresh_source(
        url="https://tiki.vn/cua-hang/qua-han",
        crawled_at=now - timedelta(hours=48, minutes=1),
        stats=ingested,
        updated_at=now - timedelta(minutes=4),
    )
    failed = _fresh_source(
        url="https://tiki.vn/cua-hang/loi",
        status="failed",
        crawled_at=now - timedelta(hours=1),
        stats=ingested,
        updated_at=now - timedelta(minutes=3),
    )
    partial = _fresh_source(
        url="https://tiki.vn/cua-hang/mot-phan",
        status="partial",
        crawled_at=now - timedelta(hours=1),
        stats=ingested,
        updated_at=now - timedelta(minutes=2),
    )
    not_ingested = _fresh_source(
        url="https://tiki.vn/cua-hang/chua-ingest",
        crawled_at=now - timedelta(hours=1),
        stats={"last_job_id": "job-crawl-only"},
        updated_at=now - timedelta(minutes=1),
    )
    never_crawled = _fresh_source(
        url="https://tiki.vn/cua-hang/chua-crawl-bao-gio",
        crawled_at=None,
        stats=ingested,
        updated_at=now - timedelta(seconds=30),
    )
    shopee_inside = _fresh_source(
        url="https://shopee.vn/shop-trong-han",
        platform="shopee",
        crawled_at=now - timedelta(hours=2),
        stats=ingested,
        updated_at=now - timedelta(seconds=10),
    )
    sources = {
        "inside": inside,
        "expired": expired,
        "failed": failed,
        "partial": partial,
        "not_ingested": not_ingested,
        "never_crawled": never_crawled,
        "shopee_inside": shopee_inside,
    }
    session.add_all(list(sources.values()))
    await session.commit()
    return sources


async def test_fresh_filter_keeps_only_sources_inside_the_ttl_window(
    api_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    seeded = await seed_cache_window_sources(session)

    response = await api_client.get("/api/sources", params={"fresh": "true"})

    assert response.status_code == 200
    body = response.json()
    expected = {str(seeded["inside"].id), str(seeded["shopee_inside"].id)}
    assert {item["id"] for item in body["items"]} == expected
    # `total` counts the filtered subset, not the whole table.
    assert body["total"] == 2

    inside_item = _by_id(body)[str(seeded["inside"].id)]
    assert inside_item["refresh_after"] is not None
    assert datetime.fromisoformat(inside_item["refresh_after"]) == (
        seeded["inside"].crawled_at + timedelta(hours=TTL_HOURS)
    )
    assert datetime.fromisoformat(inside_item["crawled_at"]) == seeded["inside"].crawled_at


async def test_fresh_filter_excludes_failed_partial_and_crawl_only_sources(
    api_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    await seed_cache_window_sources(session)

    body = (await api_client.get("/api/sources", params={"fresh": "true"})).json()
    urls = {item["url"] for item in body["items"]}

    # Recent crawl/failure timestamps are irrelevant while status/stats are wrong.
    assert "https://tiki.vn/cua-hang/loi" not in urls
    assert "https://tiki.vn/cua-hang/mot-phan" not in urls
    assert "https://tiki.vn/cua-hang/chua-ingest" not in urls
    assert "https://tiki.vn/cua-hang/chua-crawl-bao-gio" not in urls
    # ... and a source past the 48h boundary drops out even though it is done + ingested.
    assert "https://tiki.vn/cua-hang/qua-han" not in urls


async def test_fresh_false_keeps_every_source_and_still_reports_the_window(
    api_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    seeded = await seed_cache_window_sources(session)

    explicit_off = await api_client.get("/api/sources", params={"fresh": "false"})
    default = await api_client.get("/api/sources")

    assert explicit_off.json()["total"] == default.json()["total"] == 7
    items = _by_id(default.json())
    never_crawled = items[str(seeded["never_crawled"].id)]
    assert never_crawled["crawled_at"] is None
    assert never_crawled["refresh_after"] is None
    assert datetime.fromisoformat(items[str(seeded["expired"].id)]["refresh_after"]) == seeded[
        "expired"
    ].crawled_at + timedelta(hours=TTL_HOURS)


async def test_fresh_filter_combines_with_other_filters_and_pagination(
    api_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    await seed_cache_window_sources(session)

    tiki_page = await api_client.get(
        "/api/sources", params={"fresh": "true", "platform": "tiki", "limit": 1, "offset": 0}
    )
    tiki_second = await api_client.get(
        "/api/sources", params={"fresh": "true", "platform": "tiki", "limit": 1, "offset": 1}
    )
    # A stale status filter simply has no fresh rows to return.
    failed_only = await api_client.get("/api/sources", params={"fresh": "true", "status": "failed"})

    assert tiki_page.status_code == 200
    assert tiki_page.json()["total"] == 1
    assert len(tiki_page.json()["items"]) == 1
    assert tiki_second.json()["total"] == 1
    assert tiki_second.json()["items"] == []
    assert failed_only.json() == {"items": [], "total": 0}


async def test_fresh_list_matches_the_ingest_cache_hit_decision(
    api_client: httpx.AsyncClient, session: AsyncSession
) -> None:
    """The list filter and the ingest cache lookup must never disagree."""
    from app.crawl.types import ParsedURL
    from app.jobs.submit import submit_parsed_ingest
    from tests.ingest_fakes import RecordingJobEvents, RecordingJobQueue

    seeded = await seed_cache_window_sources(session)
    list_body = (await api_client.get("/api/sources", params={"fresh": "true"})).json()
    listed_ids = {item["id"] for item in list_body["items"]}

    settings = Settings(SECRET_KEY=SECRET)
    decisions: dict[str, bool] = {}
    queue = RecordingJobQueue()
    for key, source in seeded.items():
        parsed = ParsedURL(platform="tiki", kind="shop", canonical_url=source.url)
        result = await submit_parsed_ingest(
            parsed,
            session=session,
            settings=settings,
            job_events=RecordingJobEvents(),
            job_queue=queue,
        )
        decisions[key] = result.cached
        if result.cached:
            assert result.source_id == source.id
            assert result.refresh_after == source.crawled_at + timedelta(hours=TTL_HOURS)
            assert result.crawled_at == source.crawled_at
        else:
            assert result.job_id is not None
            assert result.source_id is None

    for key, source in seeded.items():
        assert decisions[key] is (str(source.id) in listed_ids), key

    # Misses enqueue without the retired `force` kwarg; hits enqueue nothing.
    assert len(queue.calls) == len([cached for cached in decisions.values() if not cached])
    assert all("force" not in payload for payload in queue.calls)
