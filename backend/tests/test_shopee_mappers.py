"""Mapping tests from the sanitized real Shopee/Apify fixtures (no network)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.platforms.plugins.shopee.mappers import (
    CliqtomediaShopeeMapper,
    ThemineworksReviewsMapper,
)
from tests.shopee_fakes import detail_items, load_fixture, review_items, shop_items

# ---------------------------------------------------------------------------
# Product mapping (cliqtomedia~shopee-scraper, real fixtures)
# ---------------------------------------------------------------------------


def test_product_mapping_uses_only_fields_the_actor_returns() -> None:
    row = next(r for r in detail_items() if r["itemId"] == "48571216193")

    product = CliqtomediaShopeeMapper.map_product(row)

    assert product.platform == "shopee"
    assert product.external_id == "48571216193"
    assert product.title.startswith("Tai Nghe Bluetooth JBL Tune 230NC")
    assert product.url == "https://shopee.vn/product/1937969974/48571216193"
    assert product.price == Decimal("298000")
    assert product.list_price is None
    assert product.currency == "VND"
    assert product.brand == "JBL"
    # Last category in the actor's list is the most specific one.
    assert product.category == "Tai nghe nhét tai & chụp tai"
    assert product.description_text
    assert product.images and len(product.images) == 7
    assert product.specs, "attributes must become specs"
    assert all(isinstance(v, str) for v in product.specs.values())
    assert product.shop is not None
    assert product.shop.external_id == "1937969974"

    # Shopee does not report these in this actor's product payload: never estimate them.
    assert product.rating_avg is None
    assert product.rating_count is None
    assert product.sold_count is None
    assert product.review_count is None


def test_product_rating_is_taken_verbatim_when_the_actor_reports_it() -> None:
    row = next(r for r in detail_items() if r["itemId"] == "17979591922")

    product = CliqtomediaShopeeMapper.map_product(row)

    assert product.rating_avg == 4.94
    assert product.rating_count == 57292
    assert product.sold_count is None


def test_product_url_falls_back_to_ids_and_rejects_missing_ids() -> None:
    product = CliqtomediaShopeeMapper.map_product(
        {"name": "X", "shopId": "11", "itemId": "22"},
        fallback_url="https://shopee.vn/whatever",
    )
    assert product.url == "https://shopee.vn/product/11/22"

    no_ids = CliqtomediaShopeeMapper.map_product(
        {"name": "X"}, fallback_url="https://shopee.vn/fallback"
    )
    assert no_ids.url == "https://shopee.vn/fallback"
    assert no_ids.title == "X"


# ---------------------------------------------------------------------------
# Shop mapping
# ---------------------------------------------------------------------------


def test_shop_mapping_keeps_the_mall_flag_in_raw() -> None:
    rows = shop_items()
    shop_row = next(r for r in rows if r["recordType"] == "shop")

    shop, products = CliqtomediaShopeeMapper.map_shop_run_items(rows)

    assert shop is not None
    assert shop.external_id == "1937969974"
    assert shop.name == "Mavis _ Tech _ Shop"
    assert shop.rating == 4.81
    assert shop.url == "https://shopee.vn/shop/1937969974"
    assert shop.follower_count is None
    assert shop.product_count is None
    assert shop_row["shopUsername"] == shop.raw["shopUsername"]
    assert "isOfficialShop" not in shop.raw  # null flags are not persisted
    assert len(products) == 2
    card = next(p for p in products if p.external_id == "48571216193")
    # Shop cards do report a rating (the fallback source for null product detail ratings).
    assert card.rating_avg == 4.9


def test_shop_run_dedupes_products_and_derives_shop_from_a_product_card() -> None:
    rows = [r for r in shop_items() if r["recordType"] == "product"]
    duplicated = [*rows, rows[0]]

    shop, products = CliqtomediaShopeeMapper.map_shop_run_items(duplicated)

    # No `recordType:"shop"` row: the shop is derived from the product card's seller fields.
    assert shop is not None
    assert shop.external_id == "1937969974"
    assert [p.external_id for p in products] == ["48571216193", "48871212409"]


def test_shop_without_any_id_is_not_mapped() -> None:
    shop, _products = CliqtomediaShopeeMapper.map_shop_run_items(
        [{"recordType": "shop", "shopName": "nameless"}]
    )
    assert shop is None


# ---------------------------------------------------------------------------
# Review mapping (themineworks~shopee-reviews-scraper, real fixtures)
# ---------------------------------------------------------------------------


def test_review_rows_with_type_are_never_reviews() -> None:
    rows = review_items()
    type_rows = [r for r in rows if r.get("_type")]

    parsed = ThemineworksReviewsMapper.parse_items(rows)

    assert len(type_rows) == 6  # 4 product_summary + summary + info
    assert len(parsed.items) == 243
    assert all(
        item.review.external_id not in {str(r.get("review_id")) for r in type_rows}
        for item in parsed.items
    )


def test_product_summary_coverage_is_read_from_the_real_fixture() -> None:
    parsed = ThemineworksReviewsMapper.parse_items(review_items())

    assert parsed.coverages_by_product == {
        "48571216193": "complete",
        "49867965494": "complete",
        "27551742889": "stopped_at_max",
        "17979591922": "shopee_limit",
    }
    assert parsed.budget_exhausted is False


def test_review_field_mapping_matches_the_real_payload() -> None:
    parsed = ThemineworksReviewsMapper.parse_items(review_items())
    first = parsed.items[0]

    assert first.review.external_id == "119251835047"
    assert first.review.rating == 5
    assert first.review.author == "user_1"
    assert first.review.variant == "Màu Đen"
    assert first.review.sub_ratings == {
        "product_quality": 5,
        "seller_service": 5,
        "delivery_service": 5,
    }
    assert first.review.media_count == 0
    assert first.review.helpful_count == 0
    assert first.review.tags == []
    assert first.review.seller_reply is None
    assert first.review.created_at_source is not None
    assert first.review.created_at_source.tzinfo is not None
    assert first.product_id == "48571216193"
    assert first.product_name
    assert first.product_url == "https://shopee.vn/product/1937969974/48571216193"
    assert first.shop_id == "1937969974"


def test_anonymous_reviewers_lose_the_author_and_media_is_counted() -> None:
    rows = load_fixture("apify_themineworks_reviews_stars123_6months_vn.json")
    parsed = ThemineworksReviewsMapper.parse_items(rows)

    anon = next(i for i in parsed.items if i.review.external_id == "119214882021")

    assert anon.review.author is None
    assert anon.review.media_count == 1  # 0 images + 1 video
    assert anon.review.variant is None
    assert anon.review.content
    assert anon.review.rating == 3


def test_absent_likes_become_zero_and_seller_reply_is_kept() -> None:
    rows = [
        {
            "review_id": "1",
            "rating": 4,
            "comment": "ok",
            "likes": None,
            "seller_reply": "Cảm ơn bạn",
        },
        {"review_id": "2", "rating": 2, "comment": "tệ"},
    ]

    parsed = ThemineworksReviewsMapper.parse_items(rows)

    assert parsed.items[0].review.helpful_count == 0
    assert parsed.items[0].review.seller_reply == "Cảm ơn bạn"
    assert parsed.items[1].review.helpful_count == 0


def test_summary_budget_flag_marks_the_run_as_budget_exhausted() -> None:
    rows = [
        {"_type": "summary", "stopped_by_max_cost": True},
        {"_type": "summary", "billing": {"budget_exhausted": True}},
        {"review_id": "1", "rating": 5},
    ]

    parsed = ThemineworksReviewsMapper.parse_items(rows)

    assert parsed.budget_exhausted is True
    assert len(parsed.items) == 1


def test_rows_without_review_id_or_rating_are_skipped() -> None:
    rows = [
        {"review_id": None, "rating": 5},
        {"review_id": "2", "rating": None},
        {"review_id": "3", "rating": "4", "comment": "ok"},
        "not-a-dict",
    ]

    parsed = ThemineworksReviewsMapper.parse_items(rows)  # type: ignore[arg-type]

    assert [i.review.external_id for i in parsed.items] == ["3"]


# ---------------------------------------------------------------------------
# Review strategy input (A5)
# ---------------------------------------------------------------------------


def test_low_group_input_uses_string_stars_and_no_media_filter() -> None:
    payload = ThemineworksReviewsMapper.build_input(
        "https://shopee.vn/product/1/2",
        stars=["1", "2", "3"],
        max_reviews=150,
        newer_than="12 months",
    )

    assert payload == {
        "startUrls": ["https://shopee.vn/product/1/2"],
        "withCommentOnly": True,
        "newerThan": "12 months",
        "includeSummary": True,
        "stars": ["1", "2", "3"],
        "maxReviewsPerProduct": 150,
    }


def test_high_group_input_adds_with_media_only() -> None:
    payload = ThemineworksReviewsMapper.build_input(
        "https://shopee.vn/product/1/2",
        stars=["4", "5"],
        max_reviews=30,
        newer_than="2026-06-01",
        with_media_only=True,
    )

    assert payload["withMediaOnly"] is True
    assert payload["stars"] == ["4", "5"]
    assert payload["maxReviewsPerProduct"] == 30
    assert payload["newerThan"] == "2026-06-01"


@pytest.mark.parametrize("star", ["1", "2", "3", "4", "5"])
def test_stars_are_always_strings(star: str) -> None:
    payload = ThemineworksReviewsMapper.build_input(
        "https://shopee.vn/product/1/2",
        stars=[star],
        max_reviews=10,
        newer_than="12 months",
    )
    assert payload["stars"] == [star]
    assert isinstance(payload["stars"][0], str)
