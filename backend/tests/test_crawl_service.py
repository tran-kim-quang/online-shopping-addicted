"""Unit tests for crawl orchestration (``app.crawl.service``).

The platform adapter is scripted and the repository is stubbed, so these tests
run fully offline. Real SQL upsert/idempotency behaviour is covered by
``test_crawl_integration.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

import app.crawl.service as service_module
from app.crawl.adapters.base import AdapterRegistry
from app.crawl.errors import BlockedError, NotFoundError, UpstreamError
from app.crawl.service import crawl_url
from app.crawl.types import ProgressEvent
from tests.crawl_fakes import (
    DummySessionFactory,
    FakeAdapter,
    RepositorySpy,
    UnsupportedRegistry,
    make_product,
    make_review,
    make_shop,
)

PRODUCT_URL = "https://tiki.vn/abc-p1001.html?spid=10010"
SHOP_URL = "https://tiki.vn/cua-hang/tiki-trading"


@pytest.fixture
def repository(monkeypatch: Any) -> RepositorySpy:
    spy = RepositorySpy()
    spy.install(monkeypatch, service_module)
    return spy


def registry_with(adapter: FakeAdapter | Any) -> AdapterRegistry:
    registry = AdapterRegistry()
    registry.register(adapter)
    return registry


def product_adapter(**kwargs: Any) -> FakeAdapter:
    kwargs.setdefault("default_product", make_product("1001"))
    kwargs.setdefault("default_reviews", [make_review("r1"), make_review("r2")])
    return FakeAdapter(**kwargs)


def shop_adapter(
    product_ids: tuple[str, ...] = ("201", "202", "203"), **kwargs: Any
) -> FakeAdapter:
    listings = [make_product(pid) for pid in product_ids]
    products = {pid: make_product(pid) for pid in product_ids}
    kwargs.setdefault("shop", make_shop())
    kwargs.setdefault("listings", listings)
    kwargs.setdefault("products", products)
    kwargs.setdefault("default_reviews", [make_review("r1")])
    return FakeAdapter(**kwargs)


# ---------------------------------------------------------------------------
# Product URL flow
# ---------------------------------------------------------------------------


async def test_product_url_persists_product_and_reviews(repository: RepositorySpy) -> None:
    adapter = product_adapter()
    factory = DummySessionFactory()

    result = await crawl_url(
        PRODUCT_URL,
        session_factory=factory,
        registry=registry_with(adapter),
    )

    assert result.status == "done"
    assert result.source_id == repository.source_id
    assert len(result.product_ids) == 1
    assert result.stats.products_total == 1
    assert result.stats.products_succeeded == 1
    assert result.stats.products_failed == 0
    assert result.stats.reviews_count == 2
    assert result.stats.errors == []
    assert result.stats.duration_seconds >= 0
    assert result.error is None

    assert repository.sources[0]["url"] == PRODUCT_URL
    assert repository.sources[0]["status"] == "running"

    classify_update = repository.source_updates[0]
    assert classify_update["url_type"] == "product"
    assert classify_update["platform"] == "tiki"
    assert classify_update["external_ref"]["product_id"] == "1001"

    assert [product[0].external_id for product in repository.products] == ["1001"]
    assert repository.products[0][1] is None
    assert repository.reviews[0][1] == ["r1", "r2"]

    final_update = repository.source_updates[-1]
    assert final_update["status"] == "done"
    assert final_update["stats"]["reviews_count"] == 2
    assert final_update["crawled_at"] is not None
    assert factory.commits == 4
    assert all(session.closed for session in factory.sessions)


async def test_product_url_upserts_embedded_shop_first(repository: RepositorySpy) -> None:
    shop = make_shop("seller-1")
    adapter = product_adapter(default_product=make_product("1001", shop=shop))

    result = await crawl_url(
        PRODUCT_URL,
        session_factory=DummySessionFactory(),
        registry=registry_with(adapter),
    )

    assert result.status == "done"
    assert repository.shops == [shop]
    shop_id = repository.products[0][1]
    assert shop_id is not None
    # The embedded seller shop is created and linked from the product row.
    assert result.shop_id == shop_id


async def test_reviews_failure_marks_the_run_partial(repository: RepositorySpy) -> None:
    adapter = product_adapter(default_reviews=UpstreamError("review boom"))

    result = await crawl_url(
        PRODUCT_URL,
        session_factory=DummySessionFactory(),
        registry=registry_with(adapter),
    )

    assert result.status == "partial"
    assert result.stats.products_succeeded == 1
    assert result.stats.reviews_count == 0
    assert len(result.stats.errors) == 1
    assert result.stats.errors[0]["stage"] == "reviews"
    assert result.stats.errors[0]["product_id"] == "1001"
    assert "review boom" in result.stats.errors[0]["error"]
    # The product itself is still saved.
    assert len(repository.products) == 1
    assert repository.source_updates[-1]["status"] == "partial"


async def test_product_fetch_failure_marks_the_run_failed(repository: RepositorySpy) -> None:
    adapter = product_adapter(default_product=NotFoundError("product gone"))

    result = await crawl_url(
        PRODUCT_URL,
        session_factory=DummySessionFactory(),
        registry=registry_with(adapter),
    )

    assert result.status == "failed"
    assert result.error is not None and "product gone" in result.error
    assert result.stats.products_failed == 1
    assert result.product_ids == []
    assert repository.products == []
    assert repository.source_updates[-1]["status"] == "failed"
    assert repository.source_updates[-1]["error"] == result.error


async def test_blocked_error_marks_the_run_failed(repository: RepositorySpy) -> None:
    adapter = product_adapter(default_product=BlockedError("HTTP 403"))

    result = await crawl_url(
        PRODUCT_URL,
        session_factory=DummySessionFactory(),
        registry=registry_with(adapter),
    )

    assert result.status == "failed"
    assert "403" in (result.error or "")


async def test_fetch_reviews_is_called_with_the_review_limit(
    repository: RepositorySpy,
) -> None:
    adapter = product_adapter()

    await crawl_url(
        PRODUCT_URL,
        session_factory=DummySessionFactory(),
        registry=registry_with(adapter),
        max_reviews=7,
    )

    assert adapter.limits == [("reviews", 7)]


async def test_zero_review_limit_is_passed_through(repository: RepositorySpy) -> None:
    adapter = product_adapter(default_reviews=[])

    result = await crawl_url(
        PRODUCT_URL,
        session_factory=DummySessionFactory(),
        registry=registry_with(adapter),
        max_reviews=0,
    )

    assert result.status == "done"
    assert adapter.limits == [("reviews", 0)]
    assert result.stats.reviews_count == 0


# ---------------------------------------------------------------------------
# Classification failures
# ---------------------------------------------------------------------------


async def test_unsupported_platform_url_fails_the_source(repository: RepositorySpy) -> None:
    result = await crawl_url(
        "https://shopee.vn/product/1/2",
        session_factory=DummySessionFactory(),
        registry=registry_with(product_adapter()),
    )

    assert result.status == "failed"
    assert "Shopee" in (result.error or "")
    final_update = repository.source_updates[-1]
    assert final_update["status"] == "failed"
    assert final_update["platform"] == "shopee"
    assert repository.products == []


async def test_unknown_domain_fails_without_platform(repository: RepositorySpy) -> None:
    result = await crawl_url(
        "https://example.com/abc-p1.html",
        session_factory=DummySessionFactory(),
        registry=registry_with(product_adapter()),
    )

    assert result.status == "failed"
    assert "example.com" in (result.error or "")
    assert "platform" not in repository.source_updates[-1]


async def test_missing_adapter_fails_the_run(repository: RepositorySpy) -> None:
    result = await crawl_url(
        PRODUCT_URL,
        session_factory=DummySessionFactory(),
        registry=UnsupportedRegistry(),
    )

    assert result.status == "failed"
    assert "adapter" in (result.error or "").lower()


# ---------------------------------------------------------------------------
# Shop URL flow
# ---------------------------------------------------------------------------


async def test_shop_url_saves_every_listed_product(repository: RepositorySpy) -> None:
    adapter = shop_adapter()
    factory = DummySessionFactory()

    result = await crawl_url(
        SHOP_URL,
        session_factory=factory,
        registry=registry_with(adapter),
    )

    assert result.status == "done"
    assert result.stats.products_total == 3
    assert result.stats.products_succeeded == 3
    assert result.stats.products_failed == 0
    assert result.stats.reviews_count == 3
    assert len(result.product_ids) == 3
    assert result.shop_id is not None
    assert [product[0].external_id for product in repository.products] == ["201", "202", "203"]
    assert all(shop_id == result.shop_id for _, shop_id in repository.products)
    assert [shop.external_id for shop in repository.shops] == ["seller-1"]
    assert result.stats.errors == []
    assert repository.source_updates[-1]["status"] == "done"

    kinds = [call[0] for call in adapter.calls]
    assert kinds.count("fetch_shop") == 1
    assert kinds.count("list_shop_products") == 1
    assert kinds.count("fetch_product") == 3
    assert kinds.count("fetch_reviews") == 3


async def test_shop_url_respects_max_products(repository: RepositorySpy) -> None:
    adapter = shop_adapter()

    result = await crawl_url(
        SHOP_URL,
        session_factory=DummySessionFactory(),
        registry=registry_with(adapter),
        max_products=2,
        max_reviews=5,
    )

    assert result.stats.products_total == 2
    assert len(result.product_ids) == 2
    assert ("products", 2) in adapter.limits
    assert all(limit == 5 for kind, limit in adapter.limits if kind == "reviews")


async def test_one_failing_product_marks_the_shop_partial(repository: RepositorySpy) -> None:
    adapter = shop_adapter(
        products={
            "201": make_product("201"),
            "202": NotFoundError("product 202 gone"),
            "203": make_product("203"),
        }
    )

    result = await crawl_url(
        SHOP_URL,
        session_factory=DummySessionFactory(),
        registry=registry_with(adapter),
    )

    assert result.status == "partial"
    assert result.stats.products_total == 3
    assert result.stats.products_succeeded == 2
    assert result.stats.products_failed == 1
    assert len(result.stats.errors) == 1
    error = result.stats.errors[0]
    assert error["product_id"] == "202"
    assert error["url"] == "https://tiki.vn/san-pham-202.html"
    assert "gone" in error["error"]
    assert [product[0].external_id for product in repository.products] == ["201", "203"]
    assert repository.source_updates[-1]["status"] == "partial"


async def test_shop_fetch_failure_marks_the_run_failed(repository: RepositorySpy) -> None:
    adapter = shop_adapter(shop=NotFoundError("shop gone"))

    result = await crawl_url(
        SHOP_URL,
        session_factory=DummySessionFactory(),
        registry=registry_with(adapter),
    )

    assert result.status == "failed"
    assert "shop gone" in (result.error or "")
    assert repository.shops == []
    assert repository.source_updates[-1]["status"] == "failed"


async def test_listing_failure_marks_the_run_failed(repository: RepositorySpy) -> None:
    adapter = shop_adapter(listings=UpstreamError("listing down"))

    result = await crawl_url(
        SHOP_URL,
        session_factory=DummySessionFactory(),
        registry=registry_with(adapter),
    )

    assert result.status == "failed"
    assert "listing down" in (result.error or "")
    # The shop row was already upserted before the listing failed.
    assert len(repository.shops) == 1
    assert repository.products == []


async def test_shop_concurrency_is_bounded(repository: RepositorySpy) -> None:
    adapter = shop_adapter(
        product_ids=("301", "302", "303", "304", "305"),
        delay=0.01,
    )

    result = await crawl_url(
        SHOP_URL,
        session_factory=DummySessionFactory(),
        registry=registry_with(adapter),
        concurrency=2,
    )

    assert result.status == "done"
    assert result.stats.products_succeeded == 5
    assert 1 < adapter.max_active <= 2


async def test_single_concurrency_is_serial(repository: RepositorySpy) -> None:
    adapter = shop_adapter(product_ids=("401", "402"), delay=0.005)

    await crawl_url(
        SHOP_URL,
        session_factory=DummySessionFactory(),
        registry=registry_with(adapter),
        concurrency=0,  # clamped to 1
    )

    assert adapter.max_active == 1


# ---------------------------------------------------------------------------
# Progress events
# ---------------------------------------------------------------------------


async def test_product_progress_events_are_complete(repository: RepositorySpy) -> None:
    events: list[ProgressEvent] = []

    async def on_progress(event: ProgressEvent) -> None:
        events.append(event)

    await crawl_url(
        PRODUCT_URL,
        session_factory=DummySessionFactory(),
        registry=registry_with(product_adapter()),
        on_progress=on_progress,
    )

    assert [event.stage for event in events] == [
        "classify",
        "classify",
        "product",
        "reviews",
        "done",
    ]
    assert events[0].done == 0 and events[0].total == 1
    assert events[1].done == 1 and events[1].total == 1
    assert events[-1].done == 1 and events[-1].total == 1
    assert all(isinstance(event, ProgressEvent) for event in events)


async def test_shop_progress_events_report_each_product(repository: RepositorySpy) -> None:
    events: list[ProgressEvent] = []

    async def on_progress(event: ProgressEvent) -> None:
        events.append(event)

    await crawl_url(
        SHOP_URL,
        session_factory=DummySessionFactory(),
        registry=registry_with(shop_adapter()),
        on_progress=on_progress,
    )

    stages = [event.stage for event in events]
    assert stages[:4] == ["classify", "classify", "shop", "listing"]
    assert stages.count("product") == 3
    assert stages[-1] == "done"

    product_events = [event for event in events if event.stage == "product"]
    assert [event.done for event in product_events] == [1, 2, 3]
    assert all(event.total == 3 for event in product_events)


async def test_failure_emits_a_failed_progress_event(repository: RepositorySpy) -> None:
    events: list[ProgressEvent] = []

    async def on_progress(event: ProgressEvent) -> None:
        events.append(event)

    await crawl_url(
        "https://shopee.vn/product/1/2",
        session_factory=DummySessionFactory(),
        registry=registry_with(product_adapter()),
        on_progress=on_progress,
    )

    assert events[-1].stage == "failed"
    assert events[-1].message


async def test_missing_progress_callback_is_allowed(repository: RepositorySpy) -> None:
    result = await crawl_url(
        PRODUCT_URL,
        session_factory=DummySessionFactory(),
        registry=registry_with(product_adapter()),
        on_progress=None,
    )

    assert result.status == "done"


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


async def test_dry_run_product_previews_without_writing(repository: RepositorySpy) -> None:
    adapter = product_adapter()

    result = await crawl_url(
        PRODUCT_URL,
        registry=registry_with(adapter),
        dry_run=True,
    )

    assert result.status == "done"
    assert result.source_id is None
    assert result.product_ids == []
    assert result.preview_product is not None
    assert result.preview_product.external_id == "1001"
    assert [review.external_id for review in result.preview_reviews] == ["r1", "r2"]
    assert repository.sources == []
    assert repository.products == []
    assert repository.source_updates == []


async def test_dry_run_shop_previews_the_first_product(repository: RepositorySpy) -> None:
    adapter = shop_adapter()

    result = await crawl_url(
        SHOP_URL,
        registry=registry_with(adapter),
        dry_run=True,
        max_reviews=1,
    )

    assert result.status == "done"
    assert result.shop_id is None
    assert result.product_ids == []
    assert result.stats.products_total == 3
    assert result.stats.products_succeeded == 1
    assert result.preview_product is not None
    assert result.preview_product.external_id == "201"
    assert [review.external_id for review in result.preview_reviews] == ["r1"]
    assert repository.shops == []


async def test_dry_run_shop_first_product_failure_is_partial(repository: RepositorySpy) -> None:
    adapter = shop_adapter(
        products={
            "201": NotFoundError("gone"),
            "202": make_product("202"),
            "203": make_product("203"),
        }
    )

    result = await crawl_url(
        SHOP_URL,
        registry=registry_with(adapter),
        dry_run=True,
    )

    assert result.status == "partial"
    assert result.preview_product is None
    assert result.stats.products_failed == 1
    assert result.stats.errors[0]["product_id"] == "201"


async def test_dry_run_product_review_failure_is_partial(repository: RepositorySpy) -> None:
    adapter = product_adapter(default_reviews=UpstreamError("reviews down"))

    result = await crawl_url(
        PRODUCT_URL,
        registry=registry_with(adapter),
        dry_run=True,
    )

    assert result.status == "partial"
    assert result.preview_product is not None
    assert result.stats.errors[0]["stage"] == "reviews"


# ---------------------------------------------------------------------------
# Settings fallbacks
# ---------------------------------------------------------------------------


async def test_settings_defaults_are_used_when_limits_are_omitted(
    repository: RepositorySpy,
) -> None:
    adapter = product_adapter()

    await crawl_url(
        PRODUCT_URL,
        session_factory=DummySessionFactory(),
        registry=registry_with(adapter),
    )

    assert adapter.limits == [("reviews", 200)]


async def test_unreadable_settings_fall_back_to_defaults(
    repository: RepositorySpy, monkeypatch: Any
) -> None:
    def _boom() -> Any:
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(service_module, "get_settings", _boom)
    adapter = shop_adapter()

    result = await crawl_url(
        SHOP_URL,
        session_factory=DummySessionFactory(),
        registry=registry_with(adapter),
    )

    assert result.status == "done"
    assert ("products", 100) in adapter.limits
    assert all(limit == 200 for kind, limit in adapter.limits if kind == "reviews")
    assert result.stats.products_total == 3
