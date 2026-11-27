"""Tiki.vn public web JSON API adapter."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.crawl.adapters.base import PlatformAdapter
from app.crawl.errors import NotFoundError, UnsupportedURLError
from app.crawl.http import CrawlHttp
from app.crawl.parsing import html_to_text, parse_vn_count
from app.crawl.types import ParsedURL, ProductData, ReviewData, ShopData

_TIKI_BASE = "https://tiki.vn"
_TIKI_HEADERS = {"Referer": "https://tiki.vn/"}
_REVIEW_PAGE_SIZE = 20
_LISTING_PAGE_SIZE = 40


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
            return parse_vn_count(cleaned)
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


def _build_product_url(data: dict[str, Any], fallback: str) -> str:
    url_path = data.get("url_path")
    if isinstance(url_path, str) and url_path.strip():
        cleaned = url_path.strip()
        if cleaned.startswith(("http://", "https://")):
            return cleaned
        return f"{_TIKI_BASE}/{cleaned.lstrip('/')}"
    short_url = data.get("short_url")
    if isinstance(short_url, str) and short_url.strip():
        return short_url.strip()
    return fallback


def _extract_sold(data: dict[str, Any]) -> tuple[int | None, str | None]:
    qs = data.get("quantity_sold")
    sold_count: int | None = None
    sold_text: str | None = None

    if isinstance(qs, dict):
        raw_val = qs.get("value")
        raw_text = qs.get("text")
        if isinstance(raw_text, str) and raw_text.strip():
            sold_text = raw_text.strip()
        sold_count = _to_int(raw_val)
    elif isinstance(qs, (int, float)) and not isinstance(qs, bool):
        sold_count = int(qs)
    elif isinstance(qs, str) and qs.strip():
        sold_text = qs.strip()

    if sold_count is None:
        sold_count = _to_int(data.get("all_time_quantity_sold"))
    if sold_count is None and sold_text is not None:
        sold_count = parse_vn_count(sold_text)

    return sold_count, sold_text


def _extract_category(data: dict[str, Any]) -> str | None:
    breadcrumbs = data.get("breadcrumbs")
    if isinstance(breadcrumbs, list):
        # Tiki's final breadcrumb is the product itself with category_id == 0;
        # pick the deepest actual category entry (category_id != 0).
        for crumb in reversed(breadcrumbs):
            if not isinstance(crumb, dict):
                continue
            cat_id = crumb.get("category_id")
            name = crumb.get("name")
            if cat_id and isinstance(name, str) and name.strip():
                return name.strip()

    categories = data.get("categories")
    if isinstance(categories, dict):
        name = categories.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()

    primary_cat = data.get("primary_category_name")
    if isinstance(primary_cat, str) and primary_cat.strip():
        return primary_cat.strip()

    return None


def _extract_brand(data: dict[str, Any]) -> str | None:
    brand = data.get("brand")
    if isinstance(brand, dict):
        name = brand.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    elif isinstance(brand, str) and brand.strip():
        return brand.strip()
    return None


def _extract_specs(data: dict[str, Any]) -> dict[str, str]:
    specs: dict[str, str] = {}
    groups = data.get("specifications")
    if not isinstance(groups, list):
        return specs

    for group in groups:
        if not isinstance(group, dict):
            continue
        attrs = group.get("attributes")
        if not isinstance(attrs, list):
            continue
        for attr in attrs:
            if not isinstance(attr, dict):
                continue
            raw_key = attr.get("name") or attr.get("code")
            raw_val = attr.get("value")
            if not isinstance(raw_key, str) or not raw_key.strip():
                continue
            if raw_val is None:
                continue
            clean_val = html_to_text(str(raw_val)) if isinstance(raw_val, str) else str(raw_val)
            clean_val = clean_val.strip()
            if clean_val:
                specs[raw_key.strip()] = clean_val
    return specs


def _extract_images(data: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    images = data.get("images")
    if isinstance(images, list):
        for img in images:
            if isinstance(img, str) and img.strip():
                url = img.strip()
            elif isinstance(img, dict):
                url = (
                    img.get("base_url")
                    or img.get("large_url")
                    or img.get("medium_url")
                    or img.get("thumbnail_url")
                    or ""
                )
                url = url.strip() if isinstance(url, str) else ""
            else:
                url = ""
            if url and url not in seen:
                seen.add(url)
                urls.append(url)

    if not urls:
        thumb = data.get("thumbnail_url")
        if isinstance(thumb, str) and thumb.strip():
            urls.append(thumb.strip())
    return urls


def _extract_shop_from_seller(seller: Any) -> ShopData | None:
    if not isinstance(seller, dict):
        return None
    seller_id = seller.get("id")
    if seller_id is None:
        return None
    name = seller.get("name")
    name_str = name.strip() if isinstance(name, str) and name.strip() else f"Seller {seller_id}"
    link = seller.get("link")
    url = (
        link.strip()
        if isinstance(link, str) and link.strip()
        else f"{_TIKI_BASE}/cua-hang/{seller_id}"
    )
    return ShopData(
        platform="tiki",
        external_id=str(seller_id),
        name=name_str,
        url=url,
        raw=seller,
    )


def _parse_review_timestamp(raw_ts: Any) -> datetime | None:
    if raw_ts is None or isinstance(raw_ts, bool):
        return None
    if isinstance(raw_ts, (int, float)):
        ts = float(raw_ts)
        if ts > 1e12:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=UTC)
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(raw_ts, str) and raw_ts.strip():
        try:
            dt = datetime.fromisoformat(raw_ts.strip().replace("Z", "+00:00"))
            return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def _clean_store_title(raw_name: str | None) -> str | None:
    if not raw_name or not raw_name.strip():
        return None
    cleaned = raw_name.strip()
    if " | " in cleaned:
        cleaned = cleaned.split(" | ", 1)[0].strip()
    return cleaned or None


class TikiAdapter(PlatformAdapter):
    """Crawler adapter for Tiki.vn public JSON APIs."""

    platform = "tiki"

    def __init__(self, http: CrawlHttp | None = None) -> None:
        super().__init__(http=http)

    async def _resolve_seller_store(self, slug: str) -> dict[str, Any]:
        store_data = await self.http.get_json(
            f"{_TIKI_BASE}/api/v2/seller/stores/{slug}",
            headers=_TIKI_HEADERS,
        )
        if store_data.get("seller_id") is None:
            raise NotFoundError(f"Tiki shop not found: '{slug}'", status_code=404)
        return store_data

    async def fetch_product(self, ref: ParsedURL) -> ProductData:
        if not ref.product_id:
            raise UnsupportedURLError("Product reference is missing product_id.")

        params: dict[str, Any] = {"platform": "web"}
        if ref.spid:
            params["spid"] = ref.spid

        data = await self.http.get_json(
            f"{_TIKI_BASE}/api/v2/products/{ref.product_id}",
            params=params,
            headers=_TIKI_HEADERS,
        )
        if data.get("status") == 404 or data.get("id") is None:
            raise NotFoundError(f"Tiki product not found: {ref.product_id}", status_code=404)

        sold_count, sold_text = _extract_sold(data)
        desc_html_raw = data.get("description")
        desc_html = (
            desc_html_raw if isinstance(desc_html_raw, str) and desc_html_raw.strip() else None
        )
        desc_text = html_to_text(desc_html) or None if desc_html else None

        short_desc_raw = data.get("short_description")
        short_desc = (
            html_to_text(short_desc_raw) or None
            if isinstance(short_desc_raw, str) and short_desc_raw.strip()
            else None
        )

        seller = data.get("current_seller")
        spid = ref.spid
        if spid is None and isinstance(seller, dict) and seller.get("product_id") is not None:
            spid = str(seller["product_id"])

        list_price = _to_decimal(data.get("list_price"))
        if list_price is None:
            list_price = _to_decimal(data.get("original_price"))

        title_raw = data.get("name")
        title = title_raw.strip() if isinstance(title_raw, str) else ""

        return ProductData(
            platform=self.platform,
            external_id=str(data["id"]),
            title=title,
            url=_build_product_url(data, ref.canonical_url),
            spid=spid,
            price=_to_decimal(data.get("price")),
            list_price=list_price,
            currency="VND",
            sold_count=sold_count,
            sold_text=sold_text,
            rating_avg=_to_float(data.get("rating_average")),
            rating_count=_to_int(data.get("rating_count")),
            review_count=_to_int(data.get("review_count")),
            category=_extract_category(data),
            brand=_extract_brand(data),
            short_description=short_desc,
            description_html=desc_html,
            description_text=desc_text,
            specs=_extract_specs(data),
            images=_extract_images(data),
            shop=_extract_shop_from_seller(seller),
            raw=data,
        )

    async def fetch_reviews(self, ref: ParsedURL, *, limit: int = 200) -> list[ReviewData]:
        if not ref.product_id:
            raise UnsupportedURLError("Product reference is missing product_id.")
        if limit <= 0:
            return []

        reviews: list[ReviewData] = []
        seen_ids: set[str] = set()
        page = 1

        while len(reviews) < limit:
            per_page = min(_REVIEW_PAGE_SIZE, limit - len(reviews))
            params: dict[str, Any] = {
                "product_id": ref.product_id,
                "page": page,
                "limit": per_page,
                "sort": "score|desc,id|desc,stars|all",
                "include": "comments,contribute_info",
            }
            if ref.spid:
                params["spid"] = ref.spid

            data = await self.http.get_json(
                f"{_TIKI_BASE}/api/v2/reviews",
                params=params,
                headers=_TIKI_HEADERS,
            )
            items = data.get("data")
            if not isinstance(items, list) or not items:
                break

            for item in items:
                if not isinstance(item, dict) or item.get("id") is None:
                    continue
                ext_id = str(item["id"])
                if ext_id in seen_ids:
                    continue
                seen_ids.add(ext_id)

                created_by = item.get("created_by")
                author: str | None = None
                if isinstance(created_by, dict):
                    raw_author = created_by.get("full_name") or created_by.get("name")
                    if isinstance(raw_author, str) and raw_author.strip():
                        author = raw_author.strip()

                raw_title = item.get("title")
                raw_content = item.get("content")
                review_images = [
                    img["full_path"].strip()
                    for img in (item.get("images") or [])
                    if isinstance(img, dict)
                    and isinstance(img.get("full_path"), str)
                    and img["full_path"].strip()
                ]

                rating_val = _to_int(item.get("rating"))
                reviews.append(
                    ReviewData(
                        external_id=ext_id,
                        rating=rating_val if rating_val is not None else 0,
                        title=raw_title.strip()
                        if isinstance(raw_title, str) and raw_title.strip()
                        else None,
                        content=raw_content.strip()
                        if isinstance(raw_content, str) and raw_content.strip()
                        else None,
                        author=author,
                        created_at_source=_parse_review_timestamp(item.get("created_at")),
                        helpful_count=_to_int(item.get("thank_count")),
                        media_count=len(review_images),
                        images=review_images,
                        raw=item,
                    )
                )
                if len(reviews) >= limit:
                    break

            paging = data.get("paging")
            last_page = _to_int(paging.get("last_page")) if isinstance(paging, dict) else None
            if last_page is not None:
                if page >= last_page:
                    break
            elif len(items) < per_page:
                break

            page += 1

        return reviews[:limit]

    async def fetch_shop(self, ref: ParsedURL) -> ShopData:
        store_data: dict[str, Any] = {}
        seller_id = ref.seller_id

        if not seller_id:
            if not ref.shop_slug:
                raise UnsupportedURLError("Shop reference requires shop_slug or seller_id.")
            store_data = await self._resolve_seller_store(ref.shop_slug)
            seller_id = str(store_data["seller_id"])

        widget_data = await self.http.get_json(
            f"{_TIKI_BASE}/api/shopping/v2/widgets/seller",
            params={"seller_id": seller_id},
            headers=_TIKI_HEADERS,
        )
        seller_info = (
            widget_data.get("data", {}).get("seller")
            if isinstance(widget_data.get("data"), dict)
            else None
        )
        seller_dict = seller_info if isinstance(seller_info, dict) else {}

        name = (
            (seller_dict.get("name") or "").strip()
            or _clean_store_title(store_data.get("name"))
            or ref.shop_slug
            or f"Seller {seller_id}"
        )
        url = (seller_dict.get("url") or "").strip() or ref.canonical_url

        rating = _to_float(seller_dict.get("avg_rating_point"))
        if rating is None:
            raw_summary = _to_float(store_data.get("rating_summary"))
            rating = raw_summary if raw_summary and raw_summary > 0 else None

        follower_count = _to_int(seller_dict.get("total_follower"))
        product_count = _to_int(seller_dict.get("product_count"))

        raw_payload: dict[str, Any] = {}
        if store_data:
            raw_payload["store"] = store_data
        if widget_data:
            raw_payload["widget"] = widget_data

        return ShopData(
            platform=self.platform,
            external_id=str(seller_id),
            name=name,
            url=url,
            rating=rating,
            follower_count=follower_count,
            product_count=product_count,
            raw=raw_payload,
        )

    async def list_shop_products(self, ref: ParsedURL, *, limit: int = 100) -> list[ProductData]:
        if limit <= 0:
            return []

        seller_id = ref.seller_id
        if not seller_id:
            if not ref.shop_slug:
                raise UnsupportedURLError("Shop reference requires shop_slug or seller_id.")
            store_data = await self._resolve_seller_store(ref.shop_slug)
            seller_id = str(store_data["seller_id"])

        products: list[ProductData] = []
        seen_ids: set[str] = set()
        page = 1

        while len(products) < limit:
            per_page = min(_LISTING_PAGE_SIZE, limit - len(products))
            data = await self.http.get_json(
                f"{_TIKI_BASE}/api/v2/products",
                params={
                    "seller": seller_id,
                    "limit": per_page,
                    "page": page,
                    "sort": "top_seller",
                },
                headers=_TIKI_HEADERS,
            )
            items = data.get("data")
            if not isinstance(items, list) or not items:
                break

            for item in items:
                if not isinstance(item, dict) or item.get("id") is None:
                    continue
                ext_id = str(item["id"])
                if ext_id in seen_ids:
                    continue
                seen_ids.add(ext_id)

                sold_count, sold_text = _extract_sold(item)
                spid_raw = item.get("seller_product_id")
                spid = str(spid_raw) if spid_raw is not None else None
                fallback_url = f"{_TIKI_BASE}/product-p{ext_id}.html"
                if spid:
                    fallback_url = f"{fallback_url}?spid={spid}"

                products.append(
                    ProductData(
                        platform=self.platform,
                        external_id=ext_id,
                        title=(item.get("name") or "").strip(),
                        url=_build_product_url(item, fallback_url),
                        spid=spid,
                        price=_to_decimal(item.get("price")),
                        list_price=_to_decimal(
                            item.get("original_price") or item.get("list_price")
                        ),
                        currency="VND",
                        sold_count=sold_count,
                        sold_text=sold_text,
                        rating_avg=_to_float(item.get("rating_average")),
                        rating_count=_to_int(item.get("rating_count")),
                        review_count=_to_int(item.get("review_count")),
                        category=_extract_category(item),
                        images=_extract_images(item),
                        raw=item,
                    )
                )
                if len(products) >= limit:
                    break

            paging = data.get("paging")
            last_page = _to_int(paging.get("last_page")) if isinstance(paging, dict) else None
            if last_page is not None:
                if page >= last_page:
                    break
            elif len(items) < per_page:
                break

            page += 1

        return products[:limit]
