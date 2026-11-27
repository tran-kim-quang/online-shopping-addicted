"""Shopee URL/plugin tests (A4): every documented shape, rejects, short-link resolution."""

from __future__ import annotations

import httpx
import pytest

from app.crawl.classifier import classify_url, is_short_url, resolve_and_classify_url
from app.crawl.errors import ShortLinkUnresolvableError, UnsupportedURLError
from app.platforms.registry import PlatformRegistry
from tests.conftest import fake_resolver

SHOPEE_PRODUCT = "https://shopee.vn/product/1937969974/48571216193"


@pytest.fixture
def plugin():
    return PlatformRegistry().get("shopee")


# ---------------------------------------------------------------------------
# Product URLs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        SHOPEE_PRODUCT,
        "https://shopee.vn/Tai-Nghe-Bluetooth-JBL-Tune-230NC-Bass-Cang-Pin-40H-i.1937969974.48571216193",
        "https://www.shopee.vn/product/1937969974/48571216193/",
        "https://shopee.vn/product/1937969974/48571216193?sp_atk=abc&utm_source=facebook",
        "https://shopee.vn/product/1937969974/48571216193#product-reviews",
    ],
)
def test_product_urls_normalize_to_the_canonical_shopee_form(url: str) -> None:
    parsed = classify_url(url)

    assert parsed.platform == "shopee"
    assert parsed.kind == "product"
    assert parsed.product_id == "48571216193"
    assert parsed.seller_id == "1937969974"
    assert parsed.canonical_url == SHOPEE_PRODUCT


def test_slug_url_keeps_the_last_two_numeric_parts() -> None:
    parsed = classify_url("https://shopee.vn/abc-i.11.22")

    assert parsed.product_id == "22"
    assert parsed.seller_id == "11"
    assert parsed.canonical_url == "https://shopee.vn/product/11/22"


# ---------------------------------------------------------------------------
# Shop URLs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://shopee.vn/shop/1937969974",
        "https://www.shopee.vn/shop/1937969974/",
        "https://shopee.vn/shop/1937969974?entryPoint=ShopByPDP",
    ],
)
def test_shop_urls_are_supported(url: str) -> None:
    parsed = classify_url(url)

    assert parsed.platform == "shopee"
    assert parsed.kind == "shop"
    assert parsed.seller_id == "1937969974"
    assert parsed.product_id is None
    assert parsed.canonical_url == "https://shopee.vn/shop/1937969974"


@pytest.mark.parametrize(
    "url",
    [
        "https://shopee.vn/mavis_tech_shop",
        "https://shopee.vn/",
        "https://shopee.vn/shop",
        "https://shopee.vn/shop/abc",
        "https://shopee.vn/product/1",
        "https://shopee.vn/product/1/2/3",
        "https://shopee.vn/abc-i.11",
    ],
)
def test_username_and_malformed_paths_are_rejected_with_a_vietnamese_hint(url: str) -> None:
    with pytest.raises(UnsupportedURLError) as excinfo:
        classify_url(url)

    assert "shopee.vn/shop/<id>" in str(excinfo.value) or "link sản phẩm" in str(excinfo.value)


@pytest.mark.parametrize(
    "host",
    [
        "shopee.sg",
        "www.shopee.co.th",
        "shopee.co.id",
        "shopee.com.my",
        "shopee.ph",
        "shopee.com",
    ],
)
def test_other_country_shopee_hosts_explain_vietnam_only(host: str) -> None:
    with pytest.raises(UnsupportedURLError) as excinfo:
        classify_url(f"https://{host}/product/1/2")

    assert "shopee.vn" in str(excinfo.value)
    assert "chưa được hỗ trợ" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Short links (s.shopee.vn / shp.ee / vn.shp.ee)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["s.shopee.vn", "shp.ee", "vn.shp.ee"])
def test_short_hosts_are_registered_for_shopee(host: str) -> None:
    assert is_short_url(f"https://{host}/abc123") is True
    assert PlatformRegistry().for_short_host(host).name == "shopee"


async def test_shopee_short_link_resolves_then_classifies() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "shp.ee":
            return httpx.Response(302, headers={"location": SHOPEE_PRODUCT})
        return httpx.Response(200, text="ok")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        parsed = await resolve_and_classify_url(
            "https://shp.ee/abc123", client=client, resolver=fake_resolver
        )
    finally:
        await client.aclose()

    assert seen == ["https://shp.ee/abc123", SHOPEE_PRODUCT]
    assert parsed.platform == "shopee"
    assert parsed.product_id == "48571216193"


async def test_unresolvable_shopee_short_link_raises_short_link_unresolvable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        _ = request
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ShortLinkUnresolvableError) as excinfo:
            await resolve_and_classify_url(
                "https://s.shopee.vn/abc123", client=client, resolver=fake_resolver
            )
    finally:
        await client.aclose()

    assert excinfo.value.error_code == "short_link_unresolvable"
    assert "trình duyệt" in str(excinfo.value)


async def test_short_link_without_redirect_is_unresolvable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        _ = request
        return httpx.Response(200, text="Shopee landing page")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ShortLinkUnresolvableError):
            await resolve_and_classify_url(
                "https://vn.shp.ee/abc123", client=client, resolver=fake_resolver
            )
    finally:
        await client.aclose()


async def test_short_link_timeout_is_unresolvable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("too slow", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ShortLinkUnresolvableError):
            await resolve_and_classify_url(
                "https://shp.ee/abc123", client=client, resolver=fake_resolver
            )
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# Plugin metadata
# ---------------------------------------------------------------------------


def test_plugin_metadata_declares_the_provider_requirement(plugin) -> None:
    assert plugin.name == "shopee"
    assert plugin.display_name == "Shopee"
    assert plugin.currency == "VND"
    assert plugin.requires_provider == "apify"
    assert plugin.hosts == frozenset({"shopee.vn", "www.shopee.vn"})
    assert plugin.short_hosts == frozenset({"s.shopee.vn", "shp.ee", "vn.shp.ee"})
    assert plugin.kinds == frozenset({"product", "shop"})
    assert plugin.client_patterns == [
        r"^/.+-i\.\d+\.\d+/?$",
        r"^/product/\d+/\d+/?$",
        r"^/shop/\d+/?$",
    ]
