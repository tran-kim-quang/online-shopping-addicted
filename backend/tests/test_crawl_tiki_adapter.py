"""Tiki adapter tests driven by the sanitized fixtures (``httpx.MockTransport``)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

import httpx
import pytest

from app.crawl.adapters.tiki import (
    TikiAdapter,
    _clean_store_title,
    _parse_review_timestamp,
    _to_decimal,
    _to_float,
    _to_int,
)
from app.crawl.errors import BlockedError, NotFoundError, UnsupportedURLError
from app.crawl.http import CrawlHttp
from tests.crawl_fakes import (
    TIKI_PRODUCT_ID,
    load_fixture,
    make_crawl_http,
    product_ref,
    shop_ref,
)

PRODUCT_DETAIL = load_fixture("product_detail.json")
REVIEWS_PAGE1 = load_fixture("reviews_page1.json")
REVIEWS_PAGE2 = load_fixture("reviews_page2.json")
SHOP_INFO = load_fixture("shop_info.json")
SHOP_WIDGET = load_fixture("shop_widget.json")
SHOP_LISTING = load_fixture("shop_listing_page1.json")

Handler = Callable[[httpx.Request], httpx.Response]


def _respond(value: Any, request: httpx.Request) -> httpx.Response:
    if callable(value):
        return cast(httpx.Response, value(request))
    if isinstance(value, httpx.Response):
        return value
    if isinstance(value, Exception):
        raise value
    return httpx.Response(200, json=value)


def build_handler(
    *,
    product: Any = None,
    reviews: Any = None,
    store: Any = None,
    widget: Any = None,
    listing: Any = None,
) -> Handler:
    """Route Tiki API paths to scripted payloads; fail on unexpected paths."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/api/v2/products/"):
            return _respond(product, request)
        if path == "/api/v2/products":
            return _respond(listing, request)
        if path == "/api/v2/reviews":
            return _respond(reviews, request)
        if path.startswith("/api/v2/seller/stores/"):
            return _respond(store, request)
        if path == "/api/shopping/v2/widgets/seller":
            return _respond(widget, request)
        raise AssertionError(f"unexpected request path: {path}")

    return handler


def paged_reviews(request: httpx.Request) -> httpx.Response:
    page = int(request.url.params.get("page", "1"))
    return httpx.Response(200, json=REVIEWS_PAGE1 if page == 1 else REVIEWS_PAGE2)


@pytest.fixture
async def tiki_http() -> AsyncIterator[Any]:
    """Factory creating ``CrawlHttp`` instances whose clients are closed on teardown."""
    created: list[CrawlHttp] = []

    def _make(handler: Handler, **kwargs: Any) -> tuple[CrawlHttp, list[float]]:
        http, sleeps = make_crawl_http(handler, **kwargs)
        created.append(http)
        return http, sleeps

    yield _make

    for http in created:
        if not http.client.is_closed:
            await http.client.aclose()


# ---------------------------------------------------------------------------
# fetch_product: full mapping from the real payload
# ---------------------------------------------------------------------------


async def test_fetch_product_maps_every_field(tiki_http) -> None:
    http, _ = tiki_http(build_handler(product=PRODUCT_DETAIL))
    adapter = TikiAdapter(http=http)

    product = await adapter.fetch_product(product_ref())

    assert product.platform == "tiki"
    assert product.external_id == TIKI_PRODUCT_ID
    assert product.title == "Gạo ST25 Harmony Vinh Hiển 5KG"
    assert product.url == (
        "https://tiki.vn/gao-st25-harmony-vinh-hien-5kg-p277880353.html?spid=277880354"
    )
    assert product.spid == "277880354"
    assert product.price == Decimal("191000")
    assert product.list_price == Decimal("200000")
    assert product.currency == "VND"
    assert product.sold_count == 4486
    assert product.sold_text == "Đã bán 4k"
    assert product.rating_avg == 5.0
    assert product.review_count == 159
    assert product.rating_count is None
    assert product.category == "Gạo tẻ"
    assert product.brand == "gạo vinh hiển"
    assert product.short_description is not None
    assert product.description_html is not None
    assert product.description_text is not None
    assert "<p>" not in product.description_text
    assert "ST25 Harmony" in product.description_text
    assert product.specs["Thành phần"] == "100% gạo trắng"
    assert product.specs["Thương hiệu"] == "gạo vinh hiển"
    assert product.specs["Trọng lượng sản phẩm"] == "5kg"
    assert len(product.images) == 3
    assert product.images[0].startswith("https://salt.tikicdn.com/ts/product/")

    assert product.shop is not None
    assert product.shop.external_id == "1"
    assert product.shop.name == "Tiki Trading"
    assert product.shop.url == "https://tiki.vn/cua-hang/tiki-trading"
    # Seller payload from a product detail carries no rating/follower numbers.
    assert product.shop.rating is None
    assert product.shop.follower_count is None
    assert product.raw["id"] == 277880353


async def test_fetch_product_derives_spid_from_seller_when_missing(tiki_http) -> None:
    http, _ = tiki_http(build_handler(product=PRODUCT_DETAIL))
    adapter = TikiAdapter(http=http)

    product = await adapter.fetch_product(product_ref(spid=None))

    assert product.spid == "277880354"


async def test_fetch_product_sends_platform_and_spid_params(tiki_http) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=PRODUCT_DETAIL)

    http, _ = tiki_http(handler)
    adapter = TikiAdapter(http=http)
    await adapter.fetch_product(product_ref())

    assert seen[0].url.path == f"/api/v2/products/{TIKI_PRODUCT_ID}"
    assert seen[0].url.params["platform"] == "web"
    assert seen[0].url.params["spid"] == "277880354"
    assert seen[0].headers["referer"] == "https://tiki.vn/"


async def test_fetch_product_with_sparse_payload_leaves_fields_none(tiki_http) -> None:
    http, _ = tiki_http(build_handler(product={"id": 42, "name": "Sản phẩm tối giản"}))
    adapter = TikiAdapter(http=http)

    product = await adapter.fetch_product(product_ref("42"))

    assert product.external_id == "42"
    assert product.title == "Sản phẩm tối giản"
    assert product.price is None
    assert product.list_price is None
    assert product.sold_count is None
    assert product.sold_text is None
    assert product.rating_avg is None
    assert product.review_count is None
    assert product.category is None
    assert product.brand is None
    assert product.specs == {}
    assert product.images == []
    assert product.shop is None
    assert product.description_html is None
    assert product.description_text is None


async def test_fetch_product_empty_name_becomes_empty_string(tiki_http) -> None:
    http, _ = tiki_http(build_handler(product={"id": 42, "name": None}))
    adapter = TikiAdapter(http=http)

    product = await adapter.fetch_product(product_ref("42"))

    assert product.title == ""


async def test_fetch_product_falls_back_to_short_url_and_thumbnail(tiki_http) -> None:
    payload = {
        "id": 7,
        "name": "X",
        "short_url": "https://tiki.vn/product-p7.html",
        "thumbnail_url": "https://salt.tikicdn.com/thumb.jpg",
    }
    http, _ = tiki_http(build_handler(product=payload))
    adapter = TikiAdapter(http=http)

    product = await adapter.fetch_product(product_ref("7", canonical_url="https://fallback/"))

    assert product.url == "https://tiki.vn/product-p7.html"
    assert product.images == ["https://salt.tikicdn.com/thumb.jpg"]


async def test_fetch_product_404_raises_not_found(tiki_http) -> None:
    not_found = httpx.Response(404, json=load_fixture("product_not_found.json"))
    http, sleeps = tiki_http(build_handler(product=not_found))
    adapter = TikiAdapter(http=http)

    with pytest.raises(NotFoundError):
        await adapter.fetch_product(product_ref("999999999999"))

    assert sleeps == []


# -- defensive mapping: odd but real-world payload shapes ------------------


async def test_fetch_product_accepts_absolute_url_path(tiki_http) -> None:
    payload = {
        "id": 1,
        "name": "x",
        "url_path": "https://tiki.vn/abs-p1.html?spid=2",
    }
    http, _ = tiki_http(build_handler(product=payload))
    product = await TikiAdapter(http=http).fetch_product(product_ref("1"))

    assert product.url == "https://tiki.vn/abs-p1.html?spid=2"


async def test_fetch_product_accepts_numeric_quantity_sold(tiki_http) -> None:
    http, _ = tiki_http(build_handler(product={"id": 2, "name": "y", "quantity_sold": 1234}))

    product = await TikiAdapter(http=http).fetch_product(product_ref("2"))

    assert product.sold_count == 1234
    assert product.sold_text is None


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {
                "id": 3,
                "name": "z",
                "breadcrumbs": [{"name": "", "category_id": 0}],
                "categories": {"name": "Cat X"},
            },
            "Cat X",
        ),
        ({"id": 3, "name": "z", "primary_category_name": "Prim X"}, "Prim X"),
        (
            {"id": 3, "name": "z", "breadcrumbs": [{"name": "Leaf", "category_id": 99}]},
            "Leaf",
        ),
        ({"id": 3, "name": "z", "breadcrumbs": "not-a-list"}, None),
        (
            {
                "id": 3,
                "name": "z",
                "breadcrumbs": [{"name": "Deep", "category_id": 7}, "not-a-crumb"],
            },
            "Deep",
        ),
    ],
)
async def test_fetch_product_category_fallbacks(tiki_http, payload, expected) -> None:
    http, _ = tiki_http(build_handler(product=payload))

    product = await TikiAdapter(http=http).fetch_product(product_ref("3"))

    assert product.category == expected


async def test_fetch_product_accepts_string_brand(tiki_http) -> None:
    http, _ = tiki_http(build_handler(product={"id": 4, "name": "w", "brand": "Chuỗi"}))

    product = await TikiAdapter(http=http).fetch_product(product_ref("4"))

    assert product.brand == "Chuỗi"


async def test_fetch_product_blank_brand_is_none(tiki_http) -> None:
    http, _ = tiki_http(build_handler(product={"id": 4, "name": "w", "brand": {"name": "  "}}))

    product = await TikiAdapter(http=http).fetch_product(product_ref("4"))

    assert product.brand is None


async def test_fetch_product_specs_skip_malformed_entries(tiki_http) -> None:
    payload = {
        "id": 5,
        "name": "v",
        "specifications": [
            "not-a-group",
            {"attributes": "not-a-list"},
            {
                "attributes": [
                    {"name": "Giữ", "value": "ok"},
                    {"name": "Bỏ", "value": None},
                    {"code": "ma", "value": 5},
                    {"value": "orphan"},
                    "not-an-attr",
                ]
            },
        ],
    }
    http, _ = tiki_http(build_handler(product=payload))

    product = await TikiAdapter(http=http).fetch_product(product_ref("5"))

    assert product.specs == {"Giữ": "ok", "ma": "5"}


async def test_fetch_product_images_skip_malformed_entries(tiki_http) -> None:
    payload = {
        "id": 6,
        "name": "u",
        "images": [
            "a.jpg",
            {"base_url": "b.jpg"},
            {"foo": 1},
            None,
            {"medium_url": "c.jpg"},
        ],
    }
    http, _ = tiki_http(build_handler(product=payload))

    product = await TikiAdapter(http=http).fetch_product(product_ref("6"))

    assert product.images == ["a.jpg", "b.jpg", "c.jpg"]


async def test_fetch_product_seller_without_id_is_ignored(tiki_http) -> None:
    http, _ = tiki_http(
        build_handler(product={"id": 7, "name": "t", "current_seller": {"name": "No ID"}})
    )

    product = await TikiAdapter(http=http).fetch_product(product_ref("7"))

    assert product.shop is None


async def test_fetch_product_200_with_null_id_raises_not_found(tiki_http) -> None:
    http, _ = tiki_http(build_handler(product={"status": 404, "id": None, "name": "gone"}))
    adapter = TikiAdapter(http=http)

    with pytest.raises(NotFoundError, match="not found"):
        await adapter.fetch_product(product_ref())


async def test_fetch_product_403_raises_blocked(tiki_http) -> None:
    http, _ = tiki_http(build_handler(product=httpx.Response(403, text="blocked")))
    adapter = TikiAdapter(http=http)

    with pytest.raises(BlockedError):
        await adapter.fetch_product(product_ref())


async def test_fetch_product_captcha_page_raises_blocked(tiki_http) -> None:
    body = "<html><body><div class='g-recaptcha'></div></body></html>"
    http, _ = tiki_http(
        build_handler(product=httpx.Response(200, text=body, headers={"content-type": "text/html"}))
    )
    adapter = TikiAdapter(http=http)

    with pytest.raises(BlockedError, match="captcha"):
        await adapter.fetch_product(product_ref())


async def test_fetch_product_without_product_id_raises(tiki_http) -> None:
    http, _ = tiki_http(build_handler(product=PRODUCT_DETAIL))
    adapter = TikiAdapter(http=http)
    ref = product_ref()
    ref.product_id = None

    with pytest.raises(UnsupportedURLError, match="product_id"):
        await adapter.fetch_product(ref)


# ---------------------------------------------------------------------------
# fetch_reviews: pagination + field mapping
# ---------------------------------------------------------------------------


async def test_fetch_reviews_reads_every_page_until_last_page(tiki_http) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return paged_reviews(request)

    http, _ = tiki_http(handler)
    adapter = TikiAdapter(http=http)

    reviews = await adapter.fetch_reviews(product_ref(), limit=200)

    assert len(reviews) == 8
    assert [request.url.params["page"] for request in seen] == ["1", "2"]
    assert all(request.url.params["limit"] == "20" for request in seen)
    assert seen[0].url.params["sort"] == "score|desc,id|desc,stars|all"
    assert seen[0].url.params["product_id"] == TIKI_PRODUCT_ID
    assert seen[0].url.params["spid"] == "277880354"


async def test_fetch_reviews_maps_fields_and_sanitized_author(tiki_http) -> None:
    http, _ = tiki_http(build_handler(reviews=paged_reviews))
    adapter = TikiAdapter(http=http)

    reviews = await adapter.fetch_reviews(product_ref(), limit=1)

    first = reviews[0]
    assert first.external_id == "20252985"
    assert first.rating == 5
    assert first.title == "Cực kì hài lòng"
    assert first.content is not None and first.content.startswith("Giao hàng")
    assert first.author == "Người dùng A."
    assert first.helpful_count == 0
    assert first.created_at_source == datetime.fromtimestamp(1786504852, tz=UTC)
    assert first.images and first.images[0].startswith("https://salt.tikicdn.com/")
    # PII removed from the fixture must not reappear.
    assert "avatar_url" not in first.raw.get("created_by", {})


async def test_fetch_reviews_stops_exactly_at_limit(tiki_http) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return paged_reviews(request)

    http, _ = tiki_http(handler)
    adapter = TikiAdapter(http=http)

    reviews = await adapter.fetch_reviews(product_ref(), limit=3)

    assert len(reviews) == 3
    assert len(seen) == 1
    assert seen[0].url.params["limit"] == "3"


async def test_fetch_reviews_shrinks_page_size_to_the_remaining_quota(tiki_http) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return paged_reviews(request)

    http, _ = tiki_http(handler)
    adapter = TikiAdapter(http=http)

    reviews = await adapter.fetch_reviews(product_ref(), limit=6)

    assert len(reviews) == 6
    # 5 reviews on page 1 -> only 1 more is requested on page 2.
    assert [request.url.params["limit"] for request in seen] == ["6", "1"]


async def test_fetch_reviews_limit_zero_makes_no_request(tiki_http) -> None:
    http, _ = tiki_http(build_handler(reviews=paged_reviews))
    adapter = TikiAdapter(http=http)

    assert await adapter.fetch_reviews(product_ref(), limit=0) == []


async def test_fetch_reviews_empty_page_ends_pagination(tiki_http) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [], "paging": {"last_page": 5}})

    http, _ = tiki_http(handler)
    adapter = TikiAdapter(http=http)

    assert await adapter.fetch_reviews(product_ref(), limit=200) == []
    assert len(seen) == 1


async def test_fetch_reviews_without_last_page_uses_page_size_heuristic(tiki_http) -> None:
    """No ``paging.last_page``: a short page (1 < 20) means the end of the list."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": REVIEWS_PAGE1["data"][:1]})

    http, _ = tiki_http(handler)
    adapter = TikiAdapter(http=http)

    reviews = await adapter.fetch_reviews(product_ref(), limit=200)

    assert len(reviews) == 1
    assert len(seen) == 1


async def test_fetch_reviews_deduplicates_repeated_ids(tiki_http) -> None:
    repeated = {"data": [REVIEWS_PAGE1["data"][0], REVIEWS_PAGE1["data"][0]]}

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        if page == 1:
            return httpx.Response(200, json={**repeated, "paging": {"last_page": 2}})
        return httpx.Response(200, json={"data": [], "paging": {"last_page": 2}})

    http, _ = tiki_http(handler)
    adapter = TikiAdapter(http=http)

    reviews = await adapter.fetch_reviews(product_ref(), limit=200)

    assert len(reviews) == 1


async def test_fetch_reviews_skips_items_without_id(tiki_http) -> None:
    payload = {"data": [{"id": None, "rating": 5}, {"rating": 1}], "paging": {"last_page": 1}}
    http, _ = tiki_http(build_handler(reviews=payload))
    adapter = TikiAdapter(http=http)

    assert await adapter.fetch_reviews(product_ref(), limit=10) == []


async def test_fetch_reviews_missing_rating_defaults_to_zero(tiki_http) -> None:
    payload = {"data": [{"id": 1, "content": "x"}], "paging": {"last_page": 1}}
    http, _ = tiki_http(build_handler(reviews=payload))
    adapter = TikiAdapter(http=http)

    reviews = await adapter.fetch_reviews(product_ref(), limit=10)

    assert reviews[0].rating == 0
    assert reviews[0].title is None
    assert reviews[0].author is None


async def test_fetch_reviews_without_product_id_raises(tiki_http) -> None:
    http, _ = tiki_http(build_handler(reviews=paged_reviews))
    adapter = TikiAdapter(http=http)
    ref = product_ref()
    ref.product_id = None

    with pytest.raises(UnsupportedURLError):
        await adapter.fetch_reviews(ref)


# ---------------------------------------------------------------------------
# fetch_shop
# ---------------------------------------------------------------------------


async def test_fetch_shop_resolves_slug_then_enriches_from_widget(tiki_http) -> None:
    seen: list[httpx.Request] = []
    base = build_handler(store=SHOP_INFO, widget=SHOP_WIDGET)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return base(request)

    http, _ = tiki_http(handler)
    adapter = TikiAdapter(http=http)

    shop = await adapter.fetch_shop(shop_ref("tiki-trading"))

    assert shop.external_id == "1"
    assert shop.name == "Tiki Trading"
    assert shop.url == "https://tiki.vn/cua-hang/tiki-trading"
    assert shop.rating == pytest.approx(4.676)
    assert shop.follower_count == 515629
    assert shop.product_count is None
    assert [request.url.path for request in seen] == [
        "/api/v2/seller/stores/tiki-trading",
        "/api/shopping/v2/widgets/seller",
    ]
    assert seen[1].url.params["seller_id"] == "1"


async def test_fetch_shop_uses_seller_id_without_store_lookup(tiki_http) -> None:
    seen: list[httpx.Request] = []
    base = build_handler(store=SHOP_INFO, widget=SHOP_WIDGET)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return base(request)

    http, _ = tiki_http(handler)
    adapter = TikiAdapter(http=http)

    shop = await adapter.fetch_shop(shop_ref("tiki-trading", seller_id="1"))

    assert shop.external_id == "1"
    assert [request.url.path for request in seen] == ["/api/shopping/v2/widgets/seller"]


async def test_fetch_shop_unknown_slug_raises_not_found(tiki_http) -> None:
    """Tiki answers 200 with ``seller_id: null`` for unknown shops."""
    http, _ = tiki_http(build_handler(store=load_fixture("shop_not_found.json")))
    adapter = TikiAdapter(http=http)

    with pytest.raises(NotFoundError, match="not found"):
        await adapter.fetch_shop(shop_ref("this-shop-does-not-exist-99999"))


async def test_fetch_shop_without_slug_or_seller_id_raises(tiki_http) -> None:
    http, _ = tiki_http(build_handler(store=SHOP_INFO))
    adapter = TikiAdapter(http=http)

    ref = shop_ref("x").model_copy(update={"shop_slug": None, "seller_id": None})
    with pytest.raises(UnsupportedURLError, match="shop_slug"):
        await adapter.fetch_shop(ref)


async def test_fetch_shop_falls_back_to_store_title_and_request_url(tiki_http) -> None:
    http, _ = tiki_http(build_handler(store=SHOP_INFO, widget={"data": {"seller": {}}}))
    adapter = TikiAdapter(http=http)

    shop = await adapter.fetch_shop(shop_ref("tiki-trading"))

    assert shop.name == "Tiki Trading"  # " | " suffix stripped from store title
    assert shop.rating is None  # store rating_summary == 0 is not a real rating
    assert shop.url == "https://tiki.vn/cua-hang/tiki-trading"


async def test_fetch_shop_widget_missing_data_falls_back(tiki_http) -> None:
    http, _ = tiki_http(build_handler(store=SHOP_INFO, widget={"data": None}))
    adapter = TikiAdapter(http=http)

    shop = await adapter.fetch_shop(shop_ref("tiki-trading"))

    assert shop.name == "Tiki Trading"
    assert shop.follower_count is None


async def test_fetch_shop_zero_rating_summary_is_treated_as_none(tiki_http) -> None:
    http, _ = tiki_http(build_handler(store=SHOP_INFO, widget={"data": {"seller": {}}}))
    adapter = TikiAdapter(http=http)

    shop = await adapter.fetch_shop(shop_ref("tiki-trading"))

    assert shop.rating is None


# ---------------------------------------------------------------------------
# list_shop_products
# ---------------------------------------------------------------------------


async def test_list_shop_products_maps_listing_items(tiki_http) -> None:
    seen: list[httpx.Request] = []
    base = build_handler(store=SHOP_INFO, listing=SHOP_LISTING)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return base(request)

    http, _ = tiki_http(handler)
    adapter = TikiAdapter(http=http)

    products = await adapter.list_shop_products(shop_ref("tiki-trading"), limit=100)

    assert len(products) == 5
    first = products[0]
    assert first.external_id == "1559585"
    assert first.title == "Phiếu Quà Tặng 500.000đ"
    assert first.price == Decimal("500000")
    assert first.list_price == Decimal("500000")
    assert first.spid == "1559587"
    assert first.sold_count == 122583
    assert first.sold_text == "Đã bán 122583"
    assert first.rating_avg == 4.5
    assert first.review_count == 81
    assert first.url == "https://tiki.vn/phieu-qua-tang-500-000d-p1559585.html?spid=1559587"
    assert first.images == [SHOP_LISTING["data"][0]["thumbnail_url"]]
    assert first.shop is None

    assert seen[1].url.path == "/api/v2/products"
    assert seen[1].url.params["seller"] == "1"
    assert seen[1].url.params["sort"] == "top_seller"


async def test_list_shop_products_respects_limit(tiki_http) -> None:
    http, _ = tiki_http(build_handler(listing=SHOP_LISTING, store=SHOP_INFO))
    adapter = TikiAdapter(http=http)

    products = await adapter.list_shop_products(shop_ref("tiki-trading"), limit=2)

    assert [product.external_id for product in products] == ["1559585", "1559589"]


async def test_list_shop_products_limit_zero_makes_no_request(tiki_http) -> None:
    http, _ = tiki_http(build_handler(listing=SHOP_LISTING, store=SHOP_INFO))
    adapter = TikiAdapter(http=http)

    assert await adapter.list_shop_products(shop_ref("tiki-trading"), limit=0) == []


async def test_list_shop_products_can_resolve_seller_from_slug(tiki_http) -> None:
    seen: list[httpx.Request] = []
    base = build_handler(store=SHOP_INFO, listing=SHOP_LISTING)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return base(request)

    http, _ = tiki_http(handler)
    adapter = TikiAdapter(http=http)

    products = await adapter.list_shop_products(shop_ref("tiki-trading"), limit=1)

    assert len(products) == 1
    assert seen[0].url.path == "/api/v2/seller/stores/tiki-trading"
    assert seen[1].url.params["seller"] == "1"


async def test_list_shop_products_deduplicates_and_skips_bad_rows(tiki_http) -> None:
    payload = {
        "data": [
            {"id": 1, "name": "A", "quantity_sold": "Đã bán 2k"},
            {"id": 1, "name": "A"},
            {"id": None, "name": "bad"},
            {"name": "no id"},
        ],
        "paging": {"last_page": 1},
    }
    http, _ = tiki_http(build_handler(listing=payload, store=SHOP_INFO))
    adapter = TikiAdapter(http=http)

    products = await adapter.list_shop_products(shop_ref("tiki-trading"), limit=10)

    assert len(products) == 1
    assert products[0].sold_count == 2000
    assert products[0].sold_text == "Đã bán 2k"


async def test_list_shop_products_without_slug_raises(tiki_http) -> None:
    http, _ = tiki_http(build_handler(listing=SHOP_LISTING))
    adapter = TikiAdapter(http=http)
    ref = shop_ref("x").model_copy(update={"shop_slug": None, "seller_id": None})

    with pytest.raises(UnsupportedURLError, match="shop_slug"):
        await adapter.list_shop_products(ref, limit=1)


async def test_list_shop_products_without_paging_stops_on_short_page(tiki_http) -> None:
    payload = {"data": [{"id": 501, "name": "Chỉ một sản phẩm"}]}
    seen: list[httpx.Request] = []
    base = build_handler(store=SHOP_INFO, listing=payload)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return base(request)

    http, _ = tiki_http(handler)
    products = await TikiAdapter(http=http).list_shop_products(shop_ref("tiki-trading"), limit=100)

    assert [product.external_id for product in products] == ["501"]
    assert len([r for r in seen if r.url.path == "/api/v2/products"]) == 1


async def test_list_shop_products_non_list_payload_is_empty(tiki_http) -> None:
    http, _ = tiki_http(build_handler(store=SHOP_INFO, listing={"data": "not-a-list"}))

    products = await TikiAdapter(http=http).list_shop_products(shop_ref("tiki-trading"), limit=10)

    assert products == []


async def test_list_shop_products_walks_multiple_pages(tiki_http) -> None:
    page1 = {
        "data": [{"id": 601, "name": "A"}, {"id": 602, "name": "B"}],
        "paging": {"last_page": 2, "per_page": 40, "current_page": 1},
    }
    page2 = {
        "data": [{"id": 603, "name": "C"}],
        "paging": {"last_page": 2, "per_page": 40, "current_page": 2},
    }
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/seller/stores/tiki-trading":
            return httpx.Response(200, json=SHOP_INFO)
        seen.append(request)
        page = int(request.url.params["page"])
        return httpx.Response(200, json=page1 if page == 1 else page2)

    http, _ = tiki_http(handler)
    products = await TikiAdapter(http=http).list_shop_products(shop_ref("tiki-trading"), limit=100)

    assert [product.external_id for product in products] == ["601", "602", "603"]
    assert [request.url.params["page"] for request in seen] == ["1", "2"]


# ---------------------------------------------------------------------------
# Adapter lifecycle
# ---------------------------------------------------------------------------


async def test_adapter_creates_and_closes_its_own_http_client() -> None:
    adapter = TikiAdapter()

    assert adapter._owns_http is False
    assert adapter.http is not None
    assert adapter._owns_http is True
    owned = adapter.http

    await adapter.aclose()

    assert adapter._http is None
    assert owned.client.is_closed is True


async def test_adapter_does_not_close_injected_http(tiki_http) -> None:
    http, _ = tiki_http(build_handler(product=PRODUCT_DETAIL))
    adapter = TikiAdapter(http=http)

    await adapter.aclose()

    assert adapter.http is http
    assert http.client.is_closed is False


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (True, None),
        (5, 5),
        (5.6, 6),
        ("12", 12),
        (" 12 ", 12),
        ("", None),
        ("1,2k", 1200),
        ("n/a", None),
        ([], None),
    ],
)
def test_to_int(value: Any, expected: int | None) -> None:
    assert _to_int(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), (True, None), (4, 4.0), ("4.676", 4.676), ("abc", None), ({}, None)],
)
def test_to_float(value: Any, expected: float | None) -> None:
    assert _to_float(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (True, None),
        (191000, Decimal("191000")),
        ("191000.50", Decimal("191000.50")),
        ("abc", None),
        ({}, None),
    ],
)
def test_to_decimal(value: Any, expected: Decimal | None) -> None:
    assert _to_decimal(value) == expected


def test_clean_store_title_strips_site_suffix() -> None:
    assert _clean_store_title("Tiki Trading | Cửa hàng Tiki Trading trên Tiki.vn") == "Tiki Trading"
    assert _clean_store_title("  ") is None
    assert _clean_store_title(None) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        (True, None),
        (1786504852, datetime.fromtimestamp(1786504852, tz=UTC)),
        (1786504852000, datetime.fromtimestamp(1786504852, tz=UTC)),
        ("2026-01-02T03:04:05Z", datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)),
        ("2026-01-02T03:04:05", datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)),
        ("not-a-date", None),
        ("", None),
    ],
)
def test_parse_review_timestamp(raw: Any, expected: datetime | None) -> None:
    assert _parse_review_timestamp(raw) == expected


def test_parse_review_timestamp_out_of_range_returns_none() -> None:
    assert _parse_review_timestamp(1e30) is None
