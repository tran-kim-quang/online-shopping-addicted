"""Shared test doubles for the crawl layer.

No sockets, no database: HTTP goes through ``httpx.MockTransport``, DNS through
fake resolvers, and the platform adapter / repository are scripted stubs.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

from app.crawl.adapters.base import PlatformAdapter
from app.crawl.errors import NotFoundError, UpstreamError
from app.crawl.http import CrawlHttp
from app.crawl.rate_limit import DomainRateLimiter
from app.crawl.types import ParsedURL, ProductData, ReviewData, ShopData

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "tiki"

TIKI_PRODUCT_ID = "277880353"
TIKI_SPID = "277880354"

# R4 (Phase 3): ``CrawlHttp`` no longer installs a fake resolver for test clients,
# so every test must inject one explicitly to stay off the real network.
PUBLIC_TEST_IP = "93.184.216.34"


async def public_resolver(_host: str) -> list[str]:
    """Deterministic resolver returning a public IP without any DNS lookup."""
    return [PUBLIC_TEST_IP]


# ---------------------------------------------------------------------------
# Fixtures on disk
# ---------------------------------------------------------------------------


def load_fixture(name: str) -> Any:
    """Load a sanitized Tiki JSON fixture by file name."""
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Rate limiting / CrawlHttp helpers
# ---------------------------------------------------------------------------


async def no_sleep(_seconds: float) -> None:
    """Async no-op sleep used to keep retry/rate-limit tests instant."""


def fast_rate_limiter() -> DomainRateLimiter:
    """Return the process rate limiter configured to never actually wait."""
    limiter = DomainRateLimiter()
    limiter.configure(rate_per_second=10_000.0, sleep=no_sleep)
    limiter.clear()
    return limiter


def make_crawl_http(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    max_retries: int = 3,
    rate_limiter: Any = None,
    sleeps: list[float] | None = None,
    base_url: str = "https://tiki.vn",
    resolver: Any = public_resolver,
    **kwargs: Any,
) -> tuple[CrawlHttp, list[float]]:
    """Build a ``CrawlHttp`` bound to a ``MockTransport`` with recorded sleeps.

    ``resolver`` defaults to :func:`public_resolver`; production code no longer
    special-cases ``MockTransport`` (Phase 3 / R4), so it must be passed explicitly.
    """
    recorded = sleeps if sleeps is not None else []

    async def _sleep(seconds: float) -> None:
        recorded.append(seconds)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=base_url,
    )
    http = CrawlHttp(
        client=client,
        rate_limiter=rate_limiter if rate_limiter is not None else fast_rate_limiter(),
        resolver=resolver,
        max_retries=max_retries,
        sleep=_sleep,
        jitter=lambda _cap: 0.0,
        **kwargs,
    )
    return http, recorded


def json_response(payload: Any, *, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def product_ref(
    product_id: str = TIKI_PRODUCT_ID,
    *,
    spid: str | None = TIKI_SPID,
    canonical_url: str | None = None,
) -> ParsedURL:
    return ParsedURL(
        platform="tiki",
        kind="product",
        product_id=product_id,
        spid=spid,
        canonical_url=canonical_url or f"https://tiki.vn/product-p{product_id}.html",
    )


def shop_ref(slug: str = "tiki-trading", *, seller_id: str | None = None) -> ParsedURL:
    return ParsedURL(
        platform="tiki",
        kind="shop",
        shop_slug=slug,
        seller_id=seller_id,
        canonical_url=f"https://tiki.vn/cua-hang/{slug}",
    )


# ---------------------------------------------------------------------------
# Data builders
# ---------------------------------------------------------------------------


def make_shop(
    external_id: str = "seller-1",
    *,
    name: str = "Shop Test",
    rating: float | None = 4.5,
    follower_count: int | None = 100,
    product_count: int | None = 10,
    url: str | None = None,
) -> ShopData:
    return ShopData(
        platform="tiki",
        external_id=external_id,
        name=name,
        url=url or f"https://tiki.vn/cua-hang/{external_id}",
        rating=rating,
        follower_count=follower_count,
        product_count=product_count,
        raw={"id": external_id, "name": name},
    )


def make_product(
    external_id: str = "1001",
    *,
    title: str | None = None,
    price: str | int | None = 100000,
    sold_count: int | None = 10,
    shop: ShopData | None = None,
    **overrides: Any,
) -> ProductData:
    data: dict[str, Any] = {
        "platform": "tiki",
        "external_id": external_id,
        "title": title or f"Sản phẩm {external_id}",
        "url": f"https://tiki.vn/san-pham-{external_id}.html",
        "spid": f"{external_id}0",
        "price": Decimal(str(price)) if price is not None else None,
        "list_price": Decimal(str(price)) if price is not None else None,
        "currency": "VND",
        "sold_count": sold_count,
        "sold_text": f"Đã bán {sold_count}" if sold_count is not None else None,
        "rating_avg": 4.8,
        "rating_count": 12,
        "review_count": 3,
        "category": "Nhà cửa",
        "brand": "Thương hiệu",
        "short_description": "Mô tả ngắn",
        "description_html": "<p>Mô tả dài</p>",
        "description_text": "Mô tả dài",
        "specs": {"Xuất xứ": "Việt Nam"},
        "images": ["https://salt.tikicdn.com/a.jpg"],
        "shop": shop,
        "raw": {"id": external_id},
    }
    data.update(overrides)
    return ProductData(**data)


def make_review(external_id: str = "r1", *, rating: int = 5, **overrides: Any) -> ReviewData:
    data: dict[str, Any] = {
        "external_id": external_id,
        "rating": rating,
        "title": "Hài lòng",
        "content": "Sản phẩm tốt",
        "author": "Người dùng A.",
        "helpful_count": 1,
        "images": [],
        "raw": {"id": external_id},
    }
    data.update(overrides)
    return ReviewData(**data)


# ---------------------------------------------------------------------------
# Fake platform adapter
# ---------------------------------------------------------------------------


class FakeAdapter(PlatformAdapter):
    """Scripted ``PlatformAdapter`` recording calls and peak concurrency."""

    platform = "tiki"

    def __init__(
        self,
        *,
        products: dict[str, ProductData | Exception] | None = None,
        default_product: ProductData | Exception | None = None,
        reviews: dict[str, list[ReviewData] | Exception] | None = None,
        default_reviews: list[ReviewData] | Exception | None = None,
        shop: ShopData | Exception | None = None,
        listings: Sequence[ProductData] | Exception = (),
        delay: float = 0.0,
    ) -> None:
        super().__init__(http=None)
        self.products = products or {}
        self.default_product = default_product
        self.reviews = reviews or {}
        self.default_reviews = default_reviews
        self.shop = shop
        self.listings = listings
        self.delay = delay
        self.calls: list[tuple[str, str | None, int | None]] = []
        self.limits: list[tuple[str, int]] = []
        self.active = 0
        self.max_active = 0

    @asynccontextmanager
    async def _in_flight(self) -> AsyncIterator[None]:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield
        finally:
            self.active -= 1

    async def fetch_product(self, ref: ParsedURL) -> ProductData:
        self.calls.append(("fetch_product", ref.product_id, None))
        async with self._in_flight():
            value = self.products.get(ref.product_id or "", self.default_product)
            if value is None:
                raise NotFoundError(f"No scripted product for {ref.product_id}")
            if isinstance(value, Exception):
                raise value
            return value

    async def fetch_reviews(self, ref: ParsedURL, *, limit: int = 200) -> list[ReviewData]:
        self.calls.append(("fetch_reviews", ref.product_id, limit))
        self.limits.append(("reviews", limit))
        async with self._in_flight():
            value = self.reviews.get(ref.product_id or "", self.default_reviews)
            if isinstance(value, Exception):
                raise value
            return list(value or [])[:limit]

    async def fetch_shop(self, ref: ParsedURL) -> ShopData:
        self.calls.append(("fetch_shop", ref.shop_slug, None))
        async with self._in_flight():
            if self.shop is None:
                raise NotFoundError(f"No scripted shop for {ref.shop_slug}")
            if isinstance(self.shop, Exception):
                raise self.shop
            return self.shop

    async def list_shop_products(self, ref: ParsedURL, *, limit: int = 100) -> list[ProductData]:
        self.calls.append(("list_shop_products", ref.shop_slug, limit))
        self.limits.append(("products", limit))
        async with self._in_flight():
            if isinstance(self.listings, Exception):
                raise self.listings
            return list(self.listings)[:limit]


# ---------------------------------------------------------------------------
# Repository / session doubles (service orchestration tests)
# ---------------------------------------------------------------------------


class DummySession:
    """Async session stand-in; the repository is stubbed, so it only counts commits."""

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class DummySessionFactory:
    """Callable session factory yielding :class:`DummySession` context managers."""

    def __init__(self) -> None:
        self.sessions: list[DummySession] = []
        self.opens = 0

    @asynccontextmanager
    async def _cm(self) -> AsyncIterator[DummySession]:
        session = DummySession()
        self.sessions.append(session)
        self.opens += 1
        try:
            yield session
        finally:
            session.closed = True

    def __call__(self) -> Any:
        return self._cm()

    @property
    def commits(self) -> int:
        return sum(session.commits for session in self.sessions)


class RepositorySpy:
    """Records repository calls made by ``app.crawl.service``."""

    def __init__(self) -> None:
        self.source_id = uuid.uuid4()
        self.sources: list[dict[str, Any]] = []
        self.source_updates: list[dict[str, Any]] = []
        self.shops: list[ShopData] = []
        self.products: list[tuple[ProductData, uuid.UUID | None]] = []
        self.reviews: list[tuple[uuid.UUID, list[str]]] = []

    async def create_source(self, _session: Any, **kwargs: Any) -> SimpleNamespace:
        self.sources.append(kwargs)
        return SimpleNamespace(id=self.source_id)

    async def update_source(self, _session: Any, source_id: uuid.UUID, **kwargs: Any) -> None:
        self.source_updates.append({"source_id": source_id, **kwargs})

    async def upsert_shop(self, _session: Any, shop: ShopData, **_kwargs: Any) -> SimpleNamespace:
        self.shops.append(shop)
        return SimpleNamespace(id=uuid.uuid4())

    async def upsert_product(
        self,
        _session: Any,
        product: ProductData,
        *,
        shop_id: uuid.UUID | None = None,
        **_kwargs: Any,
    ) -> SimpleNamespace:
        self.products.append((product, shop_id))
        return SimpleNamespace(id=uuid.uuid4())

    async def upsert_reviews(
        self,
        _session: Any,
        product_id: uuid.UUID,
        reviews: Sequence[ReviewData],
        **_kwargs: Any,
    ) -> int:
        self.reviews.append((product_id, [review.external_id for review in reviews]))
        return len(reviews)

    def install(self, monkeypatch: Any, module: Any) -> None:
        """Patch the repository functions imported into the service module."""
        for name in (
            "create_source",
            "update_source",
            "upsert_shop",
            "upsert_product",
            "upsert_reviews",
        ):
            monkeypatch.setattr(module, name, getattr(self, name))


class UnsupportedRegistry:
    """Registry double whose ``get`` always fails (platform has no adapter)."""

    def get(self, platform: str) -> Any:
        from app.crawl.errors import UnsupportedPlatformError

        raise UnsupportedPlatformError(
            platform=platform,
            message=f"Không có adapter cho '{platform}'.",
        )


def raise_upstream(message: str = "upstream boom") -> Exception:
    return UpstreamError(message)
