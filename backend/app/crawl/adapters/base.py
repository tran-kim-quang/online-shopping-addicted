"""Abstract platform adapter interface and singleton adapter registry."""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.core.singleton import SingletonMeta
from app.crawl.errors import UnsupportedPlatformError
from app.crawl.http import CrawlHttp
from app.crawl.types import ParsedURL, ProductData, ReviewData, ShopData
from app.platforms.base import PlatformFetcher


class PlatformAdapter(PlatformFetcher, ABC):
    """Abstract base class for e-commerce platform crawlers."""

    platform: str
    cost_usd: float = 0.0

    def __init__(self, http: CrawlHttp | None = None) -> None:
        self._http: CrawlHttp | None = http
        self._owns_http: bool = False

    @property
    def http(self) -> CrawlHttp:
        """Return the injected ``CrawlHttp`` or lazily create an owned default instance."""
        if self._http is None:
            self._http = CrawlHttp()
            self._owns_http = True
        return self._http

    async def aclose(self) -> None:
        """Close the owned ``CrawlHttp`` client if created by this adapter."""
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None
            self._owns_http = False

    @abstractmethod
    async def fetch_product(self, ref: ParsedURL) -> ProductData:
        """Fetch full product detail for ``ref``."""

    @abstractmethod
    async def fetch_reviews(self, ref: ParsedURL, *, limit: int = 200) -> list[ReviewData]:
        """Fetch up to ``limit`` customer reviews for ``ref``."""

    @abstractmethod
    async def fetch_shop(self, ref: ParsedURL) -> ShopData:
        """Fetch seller/shop metadata for ``ref``."""

    @abstractmethod
    async def list_shop_products(self, ref: ParsedURL, *, limit: int = 100) -> list[ProductData]:
        """List up to ``limit`` lightweight product entries belonging to the shop."""


class AdapterRegistry(metaclass=SingletonMeta):
    """Singleton registry mapping platform keys to ``PlatformAdapter`` instances.

    Registers the built-in :class:`TikiAdapter` automatically on first instantiation.
    """

    def __init__(self) -> None:
        self._adapters: dict[str, PlatformAdapter] = {}
        from app.crawl.adapters.tiki import TikiAdapter

        self.register(TikiAdapter())

    def register(self, adapter: PlatformAdapter) -> None:
        """Register or replace an adapter under ``adapter.platform``."""
        key = adapter.platform.strip().lower()
        if not key:
            raise ValueError("Adapter platform name must not be empty.")
        self._adapters[key] = adapter

    def get(self, platform: str) -> PlatformAdapter:
        """Return the adapter registered for ``platform`` or raise ``UnsupportedPlatformError``."""
        key = platform.strip().lower()
        adapter = self._adapters.get(key)
        if adapter is None:
            raise UnsupportedPlatformError(
                platform=key,
                message=f"Nền tảng '{platform}' chưa có adapter hỗ trợ.",
            )
        return adapter

    def has(self, platform: str) -> bool:
        return platform.strip().lower() in self._adapters

    def platforms(self) -> list[str]:
        return sorted(self._adapters.keys())

    async def aclose_all(self) -> None:
        """Close all adapters that own an HTTP client."""
        for adapter in self._adapters.values():
            await adapter.aclose()
