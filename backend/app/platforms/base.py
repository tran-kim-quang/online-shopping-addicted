"""Abstract base classes for platform plugins and fetchers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Literal
from urllib.parse import SplitResult

from app.crawl.types import ParsedURL, ProductData, ReviewData, ShopData


class PlatformFetcher(ABC):
    """Unified async data-fetching interface implemented by every platform adapter/fetcher."""

    cost_usd: float = 0.0
    supports_shop_detail_skip: bool = False
    supports_shop_review_feed: bool = False
    sampling_metadata: dict[str, Any] | None = None

    @abstractmethod
    async def fetch_product(self, ref: ParsedURL) -> ProductData:
        """Fetch normalized product details for ``ref``."""

    @abstractmethod
    async def fetch_reviews(self, ref: ParsedURL, *, limit: int = 200) -> list[ReviewData]:
        """Fetch customer reviews for ``ref``."""

    @abstractmethod
    async def fetch_shop(self, ref: ParsedURL) -> ShopData:
        """Fetch normalized seller/shop metadata for ``ref``."""

    @abstractmethod
    async def list_shop_products(self, ref: ParsedURL, *, limit: int = 100) -> list[ProductData]:
        """List lightweight product entries belonging to a shop."""

    @abstractmethod
    async def aclose(self) -> None:
        """Release any underlying HTTP/provider client resources."""


class PlatformPlugin(ABC):
    """Plugin contract for a supported e-commerce platform."""

    name: str
    display_name: str
    hosts: frozenset[str]
    short_hosts: frozenset[str] = frozenset()
    unsupported_hosts: frozenset[str] = frozenset()
    currency: str = "VND"
    kinds: frozenset[Literal["product", "shop"]] = frozenset({"product", "shop"})
    requires_provider: str | None = None
    client_patterns: list[str] = []

    def unsupported_host_reason(self, host: str) -> str | None:
        """Return Vietnamese reason if ``host`` is an unsupported variant of this platform."""
        _ = host
        return None

    @abstractmethod
    def parse_url(self, url: SplitResult) -> ParsedURL:
        """Validate path/query of a URL whose host belongs to this plugin."""

    @abstractmethod
    def create_fetcher(
        self,
        provider_profile: Any | None = None,
        **kwargs: Any,
    ) -> PlatformFetcher:
        """Instantiate a :class:`PlatformFetcher` for this platform."""
