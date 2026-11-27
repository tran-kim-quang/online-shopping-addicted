"""Shopee fetcher backed by Apify actors with fixed 2-group stratified review strategy."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from app.core.config import get_settings
from app.crawl.errors import NotFoundError, UnsupportedURLError
from app.crawl.types import ParsedURL, ProductData, ProgressEvent, ReviewData, ShopData
from app.platforms.base import PlatformFetcher
from app.platforms.plugins.shopee.mappers import (
    CliqtomediaShopeeMapper,
    MappedReviewItem,
    ParsedReviewsRun,
    ThemineworksReviewsMapper,
)
from app.providers.apify import ApifyClient, HeartbeatFn

ProgressCallback = Callable[[ProgressEvent], Awaitable[None]]

logger = logging.getLogger(__name__)

_SHOP_RATING_FALLBACK_MAX_ITEMS = 60
_METADATA_MAX_COST_USD = 0.05
_METADATA_BUDGET_SHARE = 0.10
_LOW_GROUP_BUDGET_SHARE = 0.80
_MIN_HIGH_GROUP_BUDGET_USD = 0.006

_COVERAGE_MAP: dict[str, str] = {
    "complete": "complete",
    "complete_to_date": "complete",
    "stopped_at_max": "stopped_at_max",
    "shopee_limit": "shopee_limit",
}


def _resolve_group_coverage(
    parsed: ParsedReviewsRun,
    *,
    target_product_id: str | None = None,
) -> tuple[str, str | None]:
    if parsed.budget_exhausted:
        return "budget", None
    if target_product_id and target_product_id in parsed.coverages_by_product:
        raw = parsed.coverages_by_product[target_product_id]
        normalized = _COVERAGE_MAP.get(raw)
        if normalized is not None:
            return normalized, None
        logger.warning("Unknown Shopee actor coverage value: %r", raw)
        return "unknown", raw

    normalized_values: set[str] = set()
    unknown_raws: list[str] = []
    for raw in parsed.coverages_by_product.values():
        normalized = _COVERAGE_MAP.get(raw)
        if normalized is not None:
            normalized_values.add(normalized)
        else:
            normalized_values.add("unknown")
            if raw not in unknown_raws:
                unknown_raws.append(raw)

    for raw in unknown_raws:
        logger.warning("Unknown Shopee actor coverage value: %r", raw)

    if "shopee_limit" in normalized_values:
        return "shopee_limit", None
    if "stopped_at_max" in normalized_values:
        return "stopped_at_max", None
    if "unknown" in normalized_values:
        return "unknown", unknown_raws[0]
    return "complete", None


class ShopeeFetcher(PlatformFetcher):
    """Shopee data fetcher orchestrating product and 2-group stratified review Apify runs.

    ``max_cost_usd_per_link`` is the total budget cap for the entire crawl of a link:
    each metadata run (product detail, shop card fallback, shop listing) is capped at
    ``min(0.05, 10% * max_cost_usd_per_link)``, and the two stratified review runs run
    sequentially: ``low`` (1-3★) runs first with 80% of the remaining budget, and
    ``high`` (4-5★) runs second with all actual remaining budget
    (``max_cost_usd_per_link - stabilized_cost_of_previous_runs``) so any unused ``low``
    budget transfers to ``high``. If the remaining budget for ``high`` is below the
    minimum runnable threshold (``_MIN_HIGH_GROUP_BUDGET_USD``, ≈ 1 event + ``run-start``),
    ``high`` is skipped with ``coverage="budget"`` and ``delivered_new=0``. Total cost
    for one link stays ≤ ``max_cost_usd_per_link`` (any minor variance is only due to
    Apify's rounding).
    """

    supports_shop_detail_skip = True
    supports_shop_review_feed = True

    def __init__(
        self,
        client: ApifyClient,
        *,
        max_cost_usd_per_link: float = 0.50,
        product_actor_id: str | None = None,
        reviews_actor_id: str | None = None,
        window_months: int | None = None,
        low_star_max: int | None = None,
        high_star_max: int | None = None,
        heartbeat: HeartbeatFn | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> None:
        try:
            settings = get_settings()
        except Exception:
            settings = None

        self._client = client
        self._max_cost_usd = float(max_cost_usd_per_link)
        self._product_actor_id = product_actor_id or (
            settings.APIFY_SHOPEE_PRODUCT_ACTOR if settings else "cliqtomedia~shopee-scraper"
        )
        self._reviews_actor_id = reviews_actor_id or (
            settings.APIFY_SHOPEE_REVIEWS_ACTOR
            if settings
            else "themineworks~shopee-reviews-scraper"
        )
        self._window_months = (
            window_months
            if window_months is not None
            else (settings.SHOPEE_REVIEW_WINDOW_MONTHS if settings else 12)
        )
        self._low_star_max = (
            low_star_max
            if low_star_max is not None
            else (settings.SHOPEE_LOW_STAR_MAX if settings else 150)
        )
        self._high_star_max = (
            high_star_max
            if high_star_max is not None
            else (settings.SHOPEE_HIGH_STAR_MAX if settings else 30)
        )
        self._heartbeat = heartbeat
        self._on_progress = on_progress

        self.cost_usd: float = 0.0
        self.cost_estimated: bool = False
        self.sampling_metadata: dict[str, Any] | None = None
        self._shop_cache: dict[str, tuple[ShopData | None, list[ProductData]]] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    def _metadata_run_budget(self) -> float:
        return round(min(_METADATA_MAX_COST_USD, self._max_cost_usd * _METADATA_BUDGET_SHARE), 4)

    async def _emit(
        self,
        stage: str,
        done: int,
        total: int | None = None,
        message: str = "",
    ) -> None:
        if self._on_progress is not None:
            await self._on_progress(
                ProgressEvent(stage=stage, done=done, total=total, message=message)
            )

    def _refresh_sampling_cost(self) -> None:
        if self.sampling_metadata is not None:
            self.sampling_metadata["cost_usd"] = round(self.cost_usd, 6)
            if self.cost_estimated:
                self.sampling_metadata["cost_estimated"] = True

    async def _load_shop_bundle(
        self,
        shop_url: str,
        *,
        shop_id: str | None = None,
        max_items: int,
    ) -> tuple[ShopData | None, list[ProductData]]:
        cached = self._shop_cache.get(shop_url)
        if cached is not None:
            return cached

        payload = CliqtomediaShopeeMapper.build_shop_input(shop_url, max_items=max_items)
        run = await self._client.run_actor(
            self._product_actor_id,
            payload,
            max_charge_usd=self._metadata_run_budget(),
            heartbeat=self._heartbeat,
        )
        self.cost_usd += run.cost_usd
        if run.cost_estimated:
            self.cost_estimated = True
        self._refresh_sampling_cost()

        shop_data, products = CliqtomediaShopeeMapper.map_shop_run_items(
            run.items,
            fallback_shop_id=shop_id,
            fallback_shop_url=shop_url,
        )
        self._shop_cache[shop_url] = (shop_data, products)
        return shop_data, products

    async def fetch_product(self, ref: ParsedURL) -> ProductData:
        if not ref.product_id:
            raise UnsupportedURLError("Shopee product reference is missing product_id.")

        await self._emit("provider_product", 0, 1, "Fetching Shopee product metadata")
        payload = CliqtomediaShopeeMapper.build_product_input(ref.canonical_url)
        run = await self._client.run_actor(
            self._product_actor_id,
            payload,
            max_charge_usd=self._metadata_run_budget(),
            heartbeat=self._heartbeat,
        )
        self.cost_usd += run.cost_usd
        if run.cost_estimated:
            self.cost_estimated = True
        self._refresh_sampling_cost()

        product_rows = [
            row
            for row in run.items
            if isinstance(row, dict)
            and (row.get("recordType") in (None, "product"))
            and (row.get("itemId") is not None or row.get("name"))
        ]
        if not product_rows:
            raise NotFoundError(f"Shopee product not found: {ref.product_id}", status_code=404)

        product = CliqtomediaShopeeMapper.map_product(
            product_rows[0],
            fallback_url=ref.canonical_url,
            fallback_item_id=ref.product_id,
            fallback_shop_id=ref.seller_id,
        )

        # Shopee detail pages sometimes omit rating; check shop card rating if missing.
        # Never infer rating from collected reviews.
        shop_id = ref.seller_id or (product.shop.external_id if product.shop else None)
        if product.rating_avg is None and shop_id:
            shop_url = f"https://shopee.vn/shop/{shop_id}"
            _shop_data, shop_products = await self._load_shop_bundle(
                shop_url,
                shop_id=shop_id,
                max_items=_SHOP_RATING_FALLBACK_MAX_ITEMS,
            )
            for card in shop_products:
                if card.external_id == product.external_id and card.rating_avg is not None:
                    product = product.model_copy(update={"rating_avg": card.rating_avg})
                    break

        await self._emit("provider_product", 1, 1, product.title)
        return product

    async def fetch_shop(self, ref: ParsedURL) -> ShopData:
        shop_id = ref.seller_id
        if not shop_id:
            raise UnsupportedURLError("Shopee shop reference requires shop ID.")

        await self._emit("provider_product", 0, 1, "Fetching Shopee shop and product catalog")
        try:
            max_items = get_settings().CRAWL_MAX_PRODUCTS
        except Exception:
            max_items = 100

        shop_data, _products = await self._load_shop_bundle(
            ref.canonical_url,
            shop_id=shop_id,
            max_items=max_items,
        )
        if shop_data is None:
            raise NotFoundError(f"Shopee shop not found: {shop_id}", status_code=404)
        await self._emit("provider_product", 1, 1, shop_data.name)
        return shop_data

    async def list_shop_products(self, ref: ParsedURL, *, limit: int = 100) -> list[ProductData]:
        if limit <= 0:
            return []
        shop_id = ref.seller_id
        _shop_data, products = await self._load_shop_bundle(
            ref.canonical_url,
            shop_id=shop_id,
            max_items=limit,
        )
        return products[:limit]

    async def _run_stratified_review_groups(
        self,
        target_url: str,
        *,
        target_product_id: str | None = None,
        newer_than_by_group: dict[str, datetime] | None = None,
    ) -> tuple[list[MappedReviewItem], dict[str, Any]]:
        default_newer = f"{self._window_months} months"
        low_newer = (
            newer_than_by_group["low"].astimezone(UTC).date().isoformat()
            if newer_than_by_group and "low" in newer_than_by_group
            else default_newer
        )
        high_newer = (
            newer_than_by_group["high"].astimezone(UTC).date().isoformat()
            if newer_than_by_group and "high" in newer_than_by_group
            else default_newer
        )

        remaining_before_low = max(0.0, self._max_cost_usd - self.cost_usd)
        low_budget = round(remaining_before_low * _LOW_GROUP_BUDGET_SHARE, 4)

        low_input = ThemineworksReviewsMapper.build_input(
            target_url,
            stars=["1", "2", "3"],
            max_reviews=self._low_star_max,
            newer_than=low_newer,
            with_media_only=False,
        )
        high_input = ThemineworksReviewsMapper.build_input(
            target_url,
            stars=["4", "5"],
            max_reviews=self._high_star_max,
            newer_than=high_newer,
            with_media_only=True,
        )

        await self._emit("provider_reviews", 0, 2, "Fetching Shopee reviews (2 stratified groups)")

        async def _run_group(
            group_name: str,
            payload: dict[str, Any],
            budget: float,
            done_now: int,
        ) -> tuple[ParsedReviewsRun, float, bool]:
            if budget <= 0:
                parsed = ParsedReviewsRun(items=[], coverages_by_product={}, budget_exhausted=True)
                await self._emit(
                    "provider_reviews",
                    done_now,
                    2,
                    f"Completed review group '{group_name}' (0 reviews)",
                )
                return parsed, 0.0, False

            run = await self._client.run_actor(
                self._reviews_actor_id,
                payload,
                max_charge_usd=budget,
                heartbeat=self._heartbeat,
            )
            parsed = ThemineworksReviewsMapper.parse_items(
                run.items,
                run_budget_exhausted=run.budget_exhausted,
            )
            await self._emit(
                "provider_reviews",
                done_now,
                2,
                f"Completed review group '{group_name}' ({len(parsed.items)} reviews)",
            )
            return parsed, run.cost_usd, run.cost_estimated

        low_parsed, low_cost, low_estimated = await _run_group("low", low_input, low_budget, 1)
        self.cost_usd += low_cost
        if low_estimated:
            self.cost_estimated = True
        self._refresh_sampling_cost()

        high_budget = round(max(0.0, self._max_cost_usd - self.cost_usd), 4)
        if high_budget < _MIN_HIGH_GROUP_BUDGET_USD:
            high_budget = 0.0
        high_parsed, high_cost, high_estimated = await _run_group(
            "high", high_input, high_budget, 2
        )
        self.cost_usd += high_cost
        if high_estimated:
            self.cost_estimated = True
        self._refresh_sampling_cost()

        def _build_group_entry(
            name: str,
            stars: list[int],
            *,
            with_media_only: bool,
            max_reviews: int,
            parsed: ParsedReviewsRun,
        ) -> dict[str, Any]:
            coverage, coverage_raw = _resolve_group_coverage(
                parsed,
                target_product_id=target_product_id,
            )
            entry: dict[str, Any] = {
                "name": name,
                "stars": stars,
                "with_media_only": with_media_only,
                "max": max_reviews,
                "delivered": len(parsed.items),
                "delivered_new": len(parsed.items),
                "coverage": coverage,
            }
            if coverage_raw is not None:
                entry["coverage_raw"] = coverage_raw
            return entry

        sampling: dict[str, Any] = {
            "provider": "apify",
            "strategy": "stratified",
            "window_months": self._window_months,
            "groups": [
                _build_group_entry(
                    "low",
                    [1, 2, 3],
                    with_media_only=False,
                    max_reviews=self._low_star_max,
                    parsed=low_parsed,
                ),
                _build_group_entry(
                    "high",
                    [4, 5],
                    with_media_only=True,
                    max_reviews=self._high_star_max,
                    parsed=high_parsed,
                ),
            ],
            "cost_usd": round(self.cost_usd, 6),
        }
        if self.cost_estimated:
            sampling["cost_estimated"] = True
        self.sampling_metadata = sampling

        deduped: dict[str, MappedReviewItem] = {}
        for item in [*low_parsed.items, *high_parsed.items]:
            deduped[item.review.external_id] = item
        return list(deduped.values()), sampling

    async def fetch_reviews(
        self,
        ref: ParsedURL,
        *,
        limit: int = 200,
        newer_than_by_group: dict[str, datetime] | None = None,
    ) -> list[ReviewData]:
        _ = limit  # Fixed stratified strategy (low<=150, high<=30) ignores arbitrary user limits.
        if not ref.product_id:
            raise UnsupportedURLError("Shopee product reference is missing product_id.")

        items, _sampling = await self._run_stratified_review_groups(
            ref.canonical_url,
            target_product_id=ref.product_id,
            newer_than_by_group=newer_than_by_group,
        )
        return [item.review for item in items]

    async def fetch_shop_reviews(
        self,
        ref: ParsedURL,
        *,
        newer_than_by_group: dict[str, datetime] | None = None,
    ) -> tuple[dict[str, list[ReviewData]], dict[str, ProductData]]:
        """Run the 2 stratified review groups once on a shop URL and attribute by ``product_id``."""
        items, _sampling = await self._run_stratified_review_groups(
            ref.canonical_url,
            newer_than_by_group=newer_than_by_group,
        )
        by_product: dict[str, list[ReviewData]] = {}
        minimal_products: dict[str, ProductData] = {}
        shop_id = ref.seller_id or ""

        for item in items:
            pid = item.product_id
            if not pid:
                continue
            by_product.setdefault(pid, []).append(item.review)
            if pid not in minimal_products:
                item_shop_id = item.shop_id or shop_id
                fallback_url = (
                    f"https://shopee.vn/product/{item_shop_id}/{pid}"
                    if item_shop_id
                    else ref.canonical_url
                )
                prod_url = item.product_url or fallback_url
                minimal_products[pid] = ProductData(
                    platform="shopee",
                    external_id=pid,
                    title=item.product_name or f"Shopee Product {pid}",
                    url=prod_url,
                    currency="VND",
                )

        return by_product, minimal_products
