"""Platform-agnostic URL classification for supported and recognized platforms."""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx

from app.crawl.errors import (
    ShortLinkUnresolvableError,
    UnsupportedPlatformError,
    UnsupportedURLError,
)
from app.crawl.types import ParsedURL
from app.net.pinned import make_safe_client
from app.platforms.known import match_known_unsupported
from app.platforms.registry import PlatformRegistry
from app.security.url_validator import DNSResolver, UnsafeURLError, safe_request

_ALLOWED_SCHEMES = frozenset({"http", "https"})


def is_short_url(url: str) -> bool:
    """Return True if ``url`` is an HTTP(S) short link belonging to any registered plugin."""
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return False
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    return PlatformRegistry().for_short_host(host) is not None


# Backwards-compatible alias for callers/tests importing ``is_tiki_short_url``.
is_tiki_short_url = is_short_url


def classify_url(url: str) -> ParsedURL:
    """Classify an e-commerce URL into a canonical :class:`ParsedURL` via ``PlatformRegistry``."""
    cleaned = url.strip()
    if not cleaned:
        raise UnsupportedURLError("URL không được để trống.")

    try:
        parsed = urlsplit(cleaned)
    except ValueError as exc:
        raise UnsupportedURLError(f"URL không hợp lệ: {exc}") from exc

    scheme = parsed.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise UnsupportedURLError(
            f"Giao thức URL '{parsed.scheme}' không được hỗ trợ. Chỉ chấp nhận http/https."
        )

    host = (parsed.hostname or "").lower()
    if not host:
        raise UnsupportedURLError("URL thiếu tên miền (hostname).")

    registry = PlatformRegistry()
    plugin = registry.for_host(host)
    if plugin is not None:
        return plugin.parse_url(parsed)

    unsupported_variant = registry.unsupported_host_reason(host)
    if unsupported_variant is not None:
        _platform_name, reason = unsupported_variant
        raise UnsupportedURLError(reason)

    known = match_known_unsupported(host)
    if known is not None:
        raise UnsupportedPlatformError(platform=known.name, message=known.reason_vi)

    raise UnsupportedURLError(f"Tên miền '{host}' chưa được hỗ trợ.")


async def resolve_and_classify_url(
    url: str,
    *,
    client: httpx.AsyncClient | None = None,
    allow_private: bool = False,
    resolver: DNSResolver | None = None,
    timeout: float = 15.0,
) -> ParsedURL:
    """Resolve short links from any plugin safely via :func:`safe_request`, then classify."""
    if not is_short_url(url):
        return classify_url(url)

    owns_client = client is None
    active_client = (
        make_safe_client(allow_private=allow_private, resolver=resolver, timeout=timeout)
        if client is None
        else client
    )
    try:
        try:
            response = await safe_request(
                active_client,
                "GET",
                url.strip(),
                allow_private=allow_private,
                resolver=resolver,
            )
        except UnsafeURLError:
            raise
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ShortLinkUnresolvableError(
                "Không mở rộng được link rút gọn. Hãy mở link trên trình duyệt rồi dán link đầy đủ."
            ) from exc

        status_code = response.status_code
        final_url = str(response.url)
        await response.aclose()
        if status_code >= 400:
            raise ShortLinkUnresolvableError(
                "Không mở rộng được link rút gọn. Hãy mở link trên trình duyệt rồi dán link đầy đủ."
            )
    finally:
        if owns_client:
            await active_client.aclose()

    if is_short_url(final_url):
        raise ShortLinkUnresolvableError(
            "Không mở rộng được link rút gọn. Hãy mở link trên trình duyệt rồi dán link đầy đủ."
        )

    return classify_url(final_url)
