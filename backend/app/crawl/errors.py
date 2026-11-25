"""Error hierarchy for the crawling subsystem."""

from __future__ import annotations


class CrawlError(Exception):
    """Base error for all crawl-layer failures."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class UnsupportedURLError(CrawlError):
    """Raised when a URL has an invalid scheme, unknown domain, or unsupported path."""


class UnsupportedPlatformError(CrawlError):
    """Raised when a URL belongs to a recognized platform without an adapter yet."""

    def __init__(
        self,
        platform: str,
        message: str,
        *,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message, status_code=status_code)
        self.platform = platform


class BlockedError(CrawlError):
    """Raised when upstream blocks the crawler (HTTP 403 or captcha/challenge)."""


class NotFoundError(CrawlError):
    """Raised when the requested product or shop does not exist (HTTP 404)."""


class UpstreamError(CrawlError):
    """Raised on upstream 5xx, network timeout, or malformed upstream payload."""
