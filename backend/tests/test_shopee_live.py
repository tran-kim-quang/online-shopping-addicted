"""Opt-in live smoke for the Shopee/Apify mapping (skipped unless explicitly requested).

Run only with a real token and a hard $0.10 ceiling:

    RUN_LIVE=1 APIFY_TOKEN=... uv run pytest tests/test_shopee_live.py -q -m live

Claude/CI runs this; the default suite never does. One small-shop product
(``1801204805/49867965494``, 0 reviews per the fixture README) keeps the cost tiny.
"""

from __future__ import annotations

import os

import pytest

from app.crawl.types import ParsedURL
from app.platforms.plugins.shopee.fetcher import ShopeeFetcher
from app.providers.apify import ApifyClient

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE") != "1" or not os.environ.get("APIFY_TOKEN"),
        reason="live tests need RUN_LIVE=1 and APIFY_TOKEN",
    ),
]

SMALL_SHOP_PRODUCT = "https://shopee.vn/product/1801204805/49867965494"
MAX_COST_USD = 0.10


def _ref() -> ParsedURL:
    return ParsedURL(
        platform="shopee",
        kind="product",
        product_id="49867965494",
        seller_id="1801204805",
        canonical_url=SMALL_SHOP_PRODUCT,
    )


async def test_live_shopee_product_and_reviews_stay_under_the_ceiling() -> None:
    token = os.environ["APIFY_TOKEN"]
    client = ApifyClient(token)
    fetcher = ShopeeFetcher(client, max_cost_usd_per_link=MAX_COST_USD)
    try:
        product = await fetcher.fetch_product(_ref())
        assert product.external_id == "49867965494"
        assert product.title
        assert product.url.endswith("/product/1801204805/49867965494")
        assert product.price is not None and product.price > 0
        assert product.currency == "VND"
        # Shopee reports neither sold nor review counts for this listing.
        assert product.sold_count is None
        assert product.review_count is None

        reviews = await fetcher.fetch_reviews(_ref())
        for review in reviews:
            assert review.external_id
            assert 1 <= review.rating <= 5
            assert review.media_count >= 0
    finally:
        sampling = fetcher.sampling_metadata
        total = round(fetcher.cost_usd, 6)
        print(f"\nlive Shopee cost: ${total:.4f} (ceiling ${MAX_COST_USD:.2f})")
        if sampling is not None:
            assert sampling["provider"] == "apify"
            assert sampling["strategy"] == "stratified"
            assert sampling["cost_usd"] == pytest.approx(total)
            for group in sampling["groups"]:
                assert group["coverage"] in {
                    "complete",
                    "stopped_at_max",
                    "shopee_limit",
                    "budget",
                    "unknown",
                }
        assert total <= MAX_COST_USD + 0.002, "cost must respect the per-link ceiling"
        await fetcher.aclose()
