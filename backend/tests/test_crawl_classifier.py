"""Unit tests for Tiki/other URL classification (``app.crawl.classifier``)."""

from __future__ import annotations

import httpx
import pytest

from app.crawl.classifier import (
    classify_url,
    is_tiki_short_url,
    resolve_and_classify_url,
)
from app.crawl.errors import UnsupportedPlatformError, UnsupportedURLError
from tests.conftest import fake_resolver

PRODUCT_URL = "https://tiki.vn/gao-st25-harmony-vinh-hien-5kg-p277880353.html?spid=277880354"


# ---------------------------------------------------------------------------
# Tiki product URLs
# ---------------------------------------------------------------------------


def test_product_url_with_spid() -> None:
    parsed = classify_url(PRODUCT_URL)

    assert parsed.platform == "tiki"
    assert parsed.kind == "product"
    assert parsed.product_id == "277880353"
    assert parsed.spid == "277880354"
    assert parsed.shop_slug is None
    assert parsed.seller_id is None
    assert parsed.canonical_url == (
        "https://tiki.vn/gao-st25-harmony-vinh-hien-5kg-p277880353.html?spid=277880354"
    )


def test_product_url_without_spid() -> None:
    parsed = classify_url("https://tiki.vn/gao-st25-harmony-vinh-hien-5kg-p277880353.html")

    assert parsed.product_id == "277880353"
    assert parsed.spid is None
    assert parsed.canonical_url.endswith("-p277880353.html")


def test_product_url_ignores_extra_query_params() -> None:
    parsed = classify_url("https://tiki.vn/abc-p42.html?foo=bar&spid=99&utm_source=fb&spid=100")

    assert parsed.product_id == "42"
    assert parsed.spid == "99"  # first occurrence wins
    assert parsed.canonical_url == "https://tiki.vn/abc-p42.html?spid=99"


def test_product_url_non_numeric_spid_is_dropped() -> None:
    parsed = classify_url("https://tiki.vn/abc-p42.html?spid=not-a-number")

    assert parsed.spid is None
    assert parsed.canonical_url == "https://tiki.vn/abc-p42.html"


def test_product_url_trailing_slash_is_accepted() -> None:
    parsed = classify_url("https://tiki.vn/abc-p42.html/")

    assert parsed.product_id == "42"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.tiki.vn/abc-p42.html",
        "https://m.tiki.vn/abc-p42.html",
        "https://TIKI.VN/abc-p42.html",
        "http://tiki.vn/abc-p42.html",
    ],
)
def test_product_url_host_and_scheme_variants(url: str) -> None:
    parsed = classify_url(url)

    assert parsed.platform == "tiki"
    assert parsed.product_id == "42"
    assert parsed.canonical_url == "https://tiki.vn/abc-p42.html"


def test_product_slug_may_contain_dots_and_dashes() -> None:
    parsed = classify_url("https://tiki.vn/sach-skill-builder-2.0-p123456789.html")

    assert parsed.product_id == "123456789"


# ---------------------------------------------------------------------------
# Tiki shop URLs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://tiki.vn/cua-hang/tiki-trading",
        "https://tiki.vn/cua-hang/tiki-trading/",
        "https://m.tiki.vn/cua-hang/tiki-trading",
        "https://tiki.vn/cua-hang/tiki-trading?include=blog",
    ],
)
def test_shop_url_variants(url: str) -> None:
    parsed = classify_url(url)

    assert parsed.platform == "tiki"
    assert parsed.kind == "shop"
    assert parsed.shop_slug == "tiki-trading"
    assert parsed.product_id is None
    assert parsed.canonical_url == "https://tiki.vn/cua-hang/tiki-trading"


def test_shop_url_with_sub_path_keeps_the_slug() -> None:
    parsed = classify_url("https://tiki.vn/cua-hang/tiki-trading/san-pham-hot")

    assert parsed.kind == "shop"
    assert parsed.shop_slug == "tiki-trading"


def test_unsupported_tiki_path_is_rejected() -> None:
    with pytest.raises(UnsupportedURLError, match="Đường dẫn Tiki"):
        classify_url("https://tiki.vn/top-ban-chay")


def test_tiki_homepage_is_rejected() -> None:
    with pytest.raises(UnsupportedURLError):
        classify_url("https://tiki.vn/")


def test_tiki_product_url_missing_html_suffix_is_rejected() -> None:
    with pytest.raises(UnsupportedURLError):
        classify_url("https://tiki.vn/abc-p42")


# ---------------------------------------------------------------------------
# Short links (ti.ki)
# ---------------------------------------------------------------------------


def test_short_url_detection() -> None:
    assert is_tiki_short_url("https://ti.ki/abc123") is True
    assert is_tiki_short_url("http://www.ti.ki/abc123") is True
    assert is_tiki_short_url(PRODUCT_URL) is False
    assert is_tiki_short_url("ftp://ti.ki/abc") is False
    assert is_tiki_short_url("https://tiki.vn/ti.ki") is False
    assert is_tiki_short_url("not a url") is False


def test_short_url_cannot_be_classified_without_resolving() -> None:
    with pytest.raises(UnsupportedURLError, match="ti.ki"):
        classify_url("https://ti.ki/abc123")


async def test_short_url_is_resolved_then_classified() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "ti.ki":
            return httpx.Response(302, headers={"location": PRODUCT_URL})
        return httpx.Response(200, text="ok")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        parsed = await resolve_and_classify_url(
            "https://ti.ki/abc123",
            client=client,
            resolver=fake_resolver,
        )
    finally:
        await client.aclose()

    assert seen[0] == "https://ti.ki/abc123"
    assert len(seen) == 2  # short link + the redirect target
    assert parsed.kind == "product"
    assert parsed.product_id == "277880353"


async def test_short_url_without_redirect_fails_classification() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(UnsupportedURLError):
            await resolve_and_classify_url(
                "https://ti.ki/abc123",
                client=client,
                resolver=fake_resolver,
            )
    finally:
        await client.aclose()


async def test_non_short_url_is_classified_without_any_request() -> None:
    parsed = await resolve_and_classify_url(PRODUCT_URL)

    assert parsed.product_id == "277880353"


# ---------------------------------------------------------------------------
# Recognized but unsupported platforms
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "platform"),
    [
        ("https://www.lazada.vn/products/x-i123.html", "lazada"),
        ("https://www.amazon.com/dp/B08N5WRWNW", "amazon"),
        ("https://amzn.to/3abc", "amazon"),
        ("https://www.ebay.com/itm/123456", "ebay"),
        ("https://www.tiktok.com/@shop/video/123", "tiktok_shop"),
        ("https://vt.tiktok.com/ZSabc/", "tiktok_shop"),
        ("https://shop.tiktok.com/view/product/123", "tiktok_shop"),
    ],
)
def test_unsupported_platforms_raise_with_vietnamese_hint(url: str, platform: str) -> None:
    # Phase 6 moved the messages from classifier.py into app/platforms/known.py and removed
    # the "see docs/research/..." hint (docs/ now only publishes OVERVIEW.md).
    with pytest.raises(UnsupportedPlatformError) as excinfo:
        classify_url(url)

    exc = excinfo.value
    assert exc.platform == platform
    assert "chưa được hỗ trợ" in str(exc)
    assert "docs/" not in str(exc)


def test_shopee_url_is_supported_after_phase_6() -> None:
    # Shopee left known-unsupported and became a plugin (requires an Apify data provider).
    parsed = classify_url("https://shopee.vn/product/1937969974/48571216193")

    assert parsed.platform == "shopee"
    assert parsed.kind == "product"
    assert parsed.product_id == "48571216193"


# ---------------------------------------------------------------------------
# Unknown / malformed input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://google.com/search?q=tiki",
        "https://tiki.vn.evil.example/abc-p42.html",
        "https://notiki.vn/abc-p42.html",
    ],
)
def test_unknown_domain_is_rejected(url: str) -> None:
    with pytest.raises(UnsupportedURLError, match="chưa được hỗ trợ"):
        classify_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://tiki.vn/abc-p42.html",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/html,<h1>x</h1>",
        "//tiki.vn/abc-p42.html",
    ],
)
def test_unsupported_scheme_is_rejected(url: str) -> None:
    with pytest.raises(UnsupportedURLError):
        classify_url(url)


@pytest.mark.parametrize("url", ["", "   ", "https://", "https:///path"])
def test_empty_or_hostless_url_is_rejected(url: str) -> None:
    with pytest.raises(UnsupportedURLError):
        classify_url(url)


@pytest.mark.parametrize("url", ["http://[::1", "https://[not-an-ip]/abc-p1.html"])
def test_malformed_authority_is_rejected(url: str) -> None:
    with pytest.raises(UnsupportedURLError):
        classify_url(url)


@pytest.mark.parametrize("url", ["http://[::1", "https://[not-an-ip]/x"])
def test_short_url_detection_survives_malformed_urls(url: str) -> None:
    assert is_tiki_short_url(url) is False


async def test_owned_safe_client_is_created_and_closed_for_short_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[httpx.AsyncClient] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "ti.ki":
            return httpx.Response(302, headers={"location": PRODUCT_URL})
        return httpx.Response(200, text="ok")

    def _fake_make_safe_client(**_kwargs: object) -> httpx.AsyncClient:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        created.append(client)
        return client

    monkeypatch.setattr("app.crawl.classifier.make_safe_client", _fake_make_safe_client)

    parsed = await resolve_and_classify_url("https://ti.ki/abc123", resolver=fake_resolver)

    assert parsed.product_id == "277880353"
    assert len(created) == 1
    assert created[0].is_closed is True
