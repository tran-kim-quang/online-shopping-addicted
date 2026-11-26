"""E-commerce crawling subsystem (classifier, safe HTTP, adapters, persistence)."""

from app.crawl.errors import (
    BlockedError,
    CrawlError,
    NotFoundError,
    UnsupportedPlatformError,
    UnsupportedURLError,
    UpstreamError,
)
from app.crawl.types import (
    CrawlResult,
    CrawlStats,
    ParsedURL,
    ProductData,
    ProgressEvent,
    ReviewData,
    ShopData,
)

__all__ = [
    "BlockedError",
    "CrawlError",
    "CrawlResult",
    "CrawlStats",
    "NotFoundError",
    "ParsedURL",
    "ProductData",
    "ProgressEvent",
    "ReviewData",
    "ShopData",
    "UnsupportedPlatformError",
    "UnsupportedURLError",
    "UpstreamError",
]
