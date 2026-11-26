"""URL classification for supported and recognized e-commerce platforms."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

import httpx

from app.crawl.errors import UnsupportedPlatformError, UnsupportedURLError
from app.crawl.types import ParsedURL
from app.net.pinned import make_safe_client
from app.security.url_validator import DNSResolver, safe_request

_ALLOWED_SCHEMES = frozenset({"http", "https"})

_TIKI_HOSTS = frozenset({"tiki.vn", "www.tiki.vn", "m.tiki.vn"})
_TIKI_SHORT_HOSTS = frozenset({"ti.ki", "www.ti.ki"})

_TIKI_PRODUCT_PATH_RE = re.compile(r"^/(?P<slug>[^/]+-p(?P<pid>\d+))\.html/?$", re.IGNORECASE)
_TIKI_SHOP_PATH_RE = re.compile(r"^/cua-hang/(?P<slug>[A-Za-z0-9._-]+)(?:/.*)?$", re.IGNORECASE)

_UNSUPPORTED_PLATFORMS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "shopee",
        (
            "shopee.vn",
            "shopee.com",
            "shopee.sg",
            "shopee.co.th",
            "shopee.co.id",
            "shopee.com.my",
            "shopee.ph",
            "shp.ee",
        ),
        (
            "Shopee chưa được hỗ trợ: API yêu cầu chữ ký JS obfuscate, phiên đăng nhập và "
            "chống bot/captcha mạnh. Xem docs/research/hard-ecommerce-sites.md."
        ),
    ),
    (
        "lazada",
        (
            "lazada.vn",
            "lazada.com",
            "lazada.sg",
            "lazada.co.th",
            "lazada.co.id",
            "lazada.com.my",
            "lazada.com.ph",
        ),
        (
            "Lazada chưa được hỗ trợ: dùng hệ chống bot Alibaba (slider captcha) và "
            "API mtop có token ký. Xem docs/research/hard-ecommerce-sites.md."
        ),
    ),
    (
        "amazon",
        (
            "amazon.com",
            "amazon.co.uk",
            "amazon.co.jp",
            "amazon.de",
            "amazon.fr",
            "amazon.ca",
            "amazon.sg",
            "amazon.com.au",
            "amzn.to",
            "a.co",
        ),
        (
            "Amazon chưa được hỗ trợ: chống bot/captcha 503, review đầy đủ yêu cầu đăng nhập "
            "và layout không đồng nhất. Xem docs/research/hard-ecommerce-sites.md."
        ),
    ),
    (
        "ebay",
        (
            "ebay.com",
            "ebay.vn",
            "ebay.co.uk",
            "ebay.de",
            "ebay.fr",
            "ebay.com.au",
            "ebay.ca",
        ),
        (
            "eBay chưa được hỗ trợ: cần tích hợp Browse API chính thức (OAuth) hoặc HTML "
            "adapter riêng. Xem docs/research/hard-ecommerce-sites.md."
        ),
    ),
    (
        "tiktok_shop",
        (
            "tiktok.com",
            "shop.tiktok.com",
            "vt.tiktok.com",
            "vm.tiktok.com",
        ),
        (
            "TikTok Shop chưa được hỗ trợ: request web ký bằng X-Bogus/msToken và ưu tiên app. "
            "Xem docs/research/hard-ecommerce-sites.md."
        ),
    ),
)


def _matches_domain(host: str, domains: tuple[str, ...]) -> bool:
    for domain in domains:
        if host == domain or host.endswith(f".{domain}"):
            return True
    return False


def is_tiki_short_url(url: str) -> bool:
    """Return True if ``url`` is an HTTP(S) Tiki short link (``ti.ki``)."""
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return False
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        return False
    host = (parsed.hostname or "").lower()
    return host in _TIKI_SHORT_HOSTS


def classify_url(url: str) -> ParsedURL:
    """Classify an e-commerce URL into a canonical :class:`ParsedURL`.

    Supports Tiki product and shop URLs (``tiki.vn``, ``www.tiki.vn``, ``m.tiki.vn``).
    Recognizes Shopee, Lazada, Amazon, eBay, and TikTok Shop and raises
    :class:`UnsupportedPlatformError` with a Vietnamese explanation pointing to
    ``docs/research/hard-ecommerce-sites.md``.
    """
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

    if host in _TIKI_HOSTS:
        path = parsed.path or "/"
        product_match = _TIKI_PRODUCT_PATH_RE.match(path)
        if product_match is not None:
            slug = product_match.group("slug")
            pid = product_match.group("pid")
            query = parse_qs(parsed.query)
            raw_spid = (query.get("spid") or [None])[0]
            spid = raw_spid if (raw_spid and raw_spid.isdigit()) else None
            canonical = f"https://tiki.vn/{slug}.html"
            if spid is not None:
                canonical = f"{canonical}?spid={spid}"
            return ParsedURL(
                platform="tiki",
                kind="product",
                product_id=pid,
                spid=spid,
                canonical_url=canonical,
            )

        shop_match = _TIKI_SHOP_PATH_RE.match(path)
        if shop_match is not None:
            shop_slug = shop_match.group("slug")
            return ParsedURL(
                platform="tiki",
                kind="shop",
                shop_slug=shop_slug,
                canonical_url=f"https://tiki.vn/cua-hang/{shop_slug}",
            )

        raise UnsupportedURLError(
            f"Đường dẫn Tiki chưa được hỗ trợ: '{path}'. "
            "Chỉ hỗ trợ link sản phẩm (*-p<id>.html) hoặc link shop (/cua-hang/<slug>)."
        )

    if host in _TIKI_SHORT_HOSTS:
        raise UnsupportedURLError(
            "Link rút gọn ti.ki cần được mở rộng (resolve redirect) trước khi phân loại."
        )

    for platform, domains, message in _UNSUPPORTED_PLATFORMS:
        if _matches_domain(host, domains):
            raise UnsupportedPlatformError(platform=platform, message=message)

    raise UnsupportedURLError(f"Tên miền '{host}' chưa được hỗ trợ.")


async def resolve_and_classify_url(
    url: str,
    *,
    client: httpx.AsyncClient | None = None,
    allow_private: bool = False,
    resolver: DNSResolver | None = None,
    timeout: float = 15.0,
) -> ParsedURL:
    """Resolve short links (e.g. ``ti.ki``) safely via :func:`safe_request`, then classify."""
    if not is_tiki_short_url(url):
        return classify_url(url)

    owns_client = client is None
    active_client = (
        make_safe_client(allow_private=allow_private, resolver=resolver, timeout=timeout)
        if client is None
        else client
    )
    try:
        response = await safe_request(
            active_client,
            "GET",
            url.strip(),
            allow_private=allow_private,
            resolver=resolver,
        )
        final_url = str(response.url)
        await response.aclose()
    finally:
        if owns_client:
            await active_client.aclose()

    return classify_url(final_url)
