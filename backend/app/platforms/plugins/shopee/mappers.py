"""Input/output mappers for Shopee Apify actors (isolated per actor ID)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.crawl.types import ProductData, ReviewData, ShopData


def _clean_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(round(value))
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        try:
            return int(cleaned)
        except ValueError:
            return None
    return None


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    except ValueError:
        return None


class CliqtomediaShopeeMapper:
    """Input/output mapper for ``cliqtomedia~shopee-scraper``."""

    @staticmethod
    def build_product_input(canonical_url: str) -> dict[str, Any]:
        return {
            "mode": "product",
            "productUrls": [canonical_url],
            "country": "vn",
            "includeDetails": True,
            "includeSellerDetails": True,
            "maxItems": 1,
        }

    @staticmethod
    def build_shop_input(shop_url: str, *, max_items: int) -> dict[str, Any]:
        return {
            "mode": "shop",
            "shopUrls": [shop_url],
            "country": "vn",
            "maxItems": max_items,
        }

    @staticmethod
    def map_shop(
        item: dict[str, Any],
        *,
        fallback_shop_id: str | None = None,
        fallback_url: str | None = None,
    ) -> ShopData | None:
        shop_id = str(item.get("shopId") or fallback_shop_id or "").strip()
        if not shop_id:
            return None
        name = _clean_str(item.get("shopName")) or _clean_str(item.get("name")) or f"Shop {shop_id}"
        url = f"https://shopee.vn/shop/{shop_id}"
        if fallback_url and f"/shop/{shop_id}" in fallback_url:
            url = fallback_url

        raw_flags: dict[str, Any] = {}
        for key in ("isOfficialShop", "isPreferredSeller", "shopRatingCount", "shopUsername"):
            if item.get(key) is not None:
                raw_flags[key] = item[key]

        return ShopData(
            platform="shopee",
            external_id=shop_id,
            name=name,
            url=url,
            rating=_to_float(item.get("shopRating")),
            follower_count=_to_int(item.get("shopFollowers")),
            product_count=_to_int(item.get("shopProductCount")),
            raw=raw_flags,
        )

    @classmethod
    def map_product(
        cls,
        item: dict[str, Any],
        *,
        fallback_url: str | None = None,
        fallback_item_id: str | None = None,
        fallback_shop_id: str | None = None,
    ) -> ProductData:
        external_id = str(item.get("itemId") or fallback_item_id or "").strip()
        shop_id = str(item.get("shopId") or fallback_shop_id or "").strip()
        title = _clean_str(item.get("name")) or f"Shopee Product {external_id}"
        canonical = (
            f"https://shopee.vn/product/{shop_id}/{external_id}"
            if shop_id and external_id
            else (_clean_str(item.get("url")) or fallback_url or "")
        )

        category: str | None = None
        categories = item.get("categories")
        if isinstance(categories, list):
            for cat in reversed(categories):
                if isinstance(cat, dict):
                    cat_name = _clean_str(cat.get("name"))
                    if cat_name:
                        category = cat_name
                        break

        specs: dict[str, str] = {}
        attributes = item.get("attributes")
        if isinstance(attributes, list):
            for attr in attributes:
                if not isinstance(attr, dict):
                    continue
                k = _clean_str(attr.get("name"))
                v = attr.get("value")
                if k and v is not None:
                    v_str = str(v).strip()
                    if v_str:
                        specs[k] = v_str

        images = [
            img.strip()
            for img in (item.get("images") or [])
            if isinstance(img, str) and img.strip()
        ]

        shop_data: ShopData | None = None
        if shop_id and (
            _clean_str(item.get("shopName")) is not None
            or item.get("shopRating") is not None
            or item.get("shopFollowers") is not None
            or item.get("isOfficialShop") is not None
        ):
            shop_data = cls.map_shop(item, fallback_shop_id=shop_id)

        return ProductData(
            platform="shopee",
            external_id=external_id,
            title=title,
            url=canonical,
            price=_to_decimal(item.get("price")),
            list_price=_to_decimal(item.get("originalPrice")),
            currency="VND",
            sold_count=_to_int(item.get("sold")),
            rating_avg=_to_float(item.get("rating")),
            rating_count=_to_int(item.get("ratingCount")),
            review_count=None,
            category=category,
            brand=_clean_str(item.get("brand")),
            description_text=_clean_str(item.get("description")),
            specs=specs,
            images=images,
            shop=shop_data,
            raw=item,
        )

    @classmethod
    def map_shop_run_items(
        cls,
        items: list[dict[str, Any]],
        *,
        fallback_shop_id: str | None = None,
        fallback_shop_url: str | None = None,
    ) -> tuple[ShopData | None, list[ProductData]]:
        shop_data: ShopData | None = None
        products: list[ProductData] = []
        seen_item_ids: set[str] = set()

        for row in items:
            record_type = row.get("recordType")
            if record_type == "shop" and shop_data is None:
                shop_data = cls.map_shop(
                    row,
                    fallback_shop_id=fallback_shop_id,
                    fallback_url=fallback_shop_url,
                )
            elif record_type == "product":
                item_id = str(row.get("itemId") or "").strip()
                if not item_id or item_id in seen_item_ids:
                    continue
                seen_item_ids.add(item_id)
                prod = cls.map_product(row, fallback_shop_id=fallback_shop_id)
                if shop_data is None and prod.shop is not None:
                    shop_data = prod.shop
                products.append(prod)

        return shop_data, products


@dataclass(slots=True)
class MappedReviewItem:
    """Mapped review alongside its source product attribution metadata."""

    review: ReviewData
    product_id: str
    product_name: str | None = None
    product_url: str | None = None
    shop_id: str | None = None


@dataclass(slots=True)
class ParsedReviewsRun:
    """Parsed output of a single ``themineworks~shopee-reviews-scraper`` run."""

    items: list[MappedReviewItem] = field(default_factory=list)
    coverages_by_product: dict[str, str] = field(default_factory=dict)
    budget_exhausted: bool = False


class ThemineworksReviewsMapper:
    """Input/output mapper for ``themineworks~shopee-reviews-scraper``."""

    @staticmethod
    def build_input(
        target_url: str,
        *,
        stars: list[str],
        max_reviews: int,
        newer_than: str,
        with_media_only: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "startUrls": [target_url],
            "withCommentOnly": True,
            "newerThan": newer_than,
            "includeSummary": True,
            "stars": [str(s) for s in stars],
            "maxReviewsPerProduct": max_reviews,
        }
        if with_media_only:
            payload["withMediaOnly"] = True
        return payload

    @staticmethod
    def parse_items(
        raw_items: list[dict[str, Any]],
        *,
        run_budget_exhausted: bool = False,
    ) -> ParsedReviewsRun:
        parsed = ParsedReviewsRun(budget_exhausted=run_budget_exhausted)

        for row in raw_items:
            if not isinstance(row, dict):
                continue
            row_type = row.get("_type")
            if row_type is not None:
                if row_type == "product_summary":
                    pid = str(row.get("product_id") or "").strip()
                    cov = _clean_str(row.get("coverage"))
                    if pid and cov:
                        parsed.coverages_by_product[pid] = cov
                elif row_type == "summary":
                    billing = row.get("billing")
                    billing_exhausted = isinstance(billing, dict) and bool(
                        billing.get("budget_exhausted")
                    )
                    if bool(row.get("stopped_by_max_cost")) or billing_exhausted:
                        parsed.budget_exhausted = True
                continue

            review_id = row.get("review_id")
            rating = _to_int(row.get("rating"))
            if review_id is None or rating is None:
                continue

            is_anon = bool(row.get("reviewer_anonymous"))
            author = None if is_anon else _clean_str(row.get("reviewer_name"))

            helpful = _to_int(row.get("likes"))
            helpful_count = helpful if helpful is not None else 0

            tags = [
                tag.strip()
                for tag in (row.get("review_tags") or [])
                if isinstance(tag, str) and tag.strip()
            ]

            sub_ratings: dict[str, int] = {}
            for key in ("product_quality", "seller_service", "delivery_service"):
                val = _to_int(row.get(key))
                if val is not None:
                    sub_ratings[key] = val

            img_count = _to_int(row.get("image_count")) or 0
            vid_count = _to_int(row.get("video_count")) or 0
            media_count = img_count + vid_count

            images = [
                url.strip()
                for url in (row.get("image_urls") or [])
                if isinstance(url, str) and url.strip()
            ]

            review_data = ReviewData(
                external_id=str(review_id),
                rating=rating,
                content=_clean_str(row.get("comment")),
                author=author,
                created_at_source=_parse_iso_datetime(row.get("created_at")),
                helpful_count=helpful_count,
                variant=_clean_str(row.get("variant")),
                tags=tags,
                sub_ratings=sub_ratings,
                seller_reply=_clean_str(row.get("seller_reply")),
                media_count=media_count,
                images=images,
                raw=row,
            )

            pid = str(row.get("product_id") or "").strip()
            parsed.items.append(
                MappedReviewItem(
                    review=review_data,
                    product_id=pid,
                    product_name=_clean_str(row.get("product_name")),
                    product_url=_clean_str(row.get("product_url")),
                    shop_id=_clean_str(str(row["shop_id"])) if row.get("shop_id") else None,
                )
            )

        return parsed
