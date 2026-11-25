"""Live smoke test against the real Tiki API.

**Skipped by default.** Run explicitly with ``RUN_LIVE=1``::

    RUN_LIVE=1 uv run pytest tests/test_tiki_live.py -q -m live

Only two requests are made (product detail + one reviews page) and they go
through ``CrawlHttp``, so the 1 request/second/domain rate limit applies.
"""

from __future__ import annotations

import os

import pytest

from app.crawl.adapters.tiki import TikiAdapter
from app.crawl.classifier import classify_url
from app.crawl.http import CrawlHttp

pytestmark = pytest.mark.live

LIVE_PRODUCT_URL = "https://tiki.vn/gao-st25-harmony-vinh-hien-5kg-p277880353.html?spid=277880354"
RUN_LIVE = os.environ.get("RUN_LIVE") == "1"


@pytest.mark.skipif(not RUN_LIVE, reason="live test: set RUN_LIVE=1 to call the real Tiki API")
async def test_live_tiki_product_fetch() -> None:
    ref = classify_url(LIVE_PRODUCT_URL)
    assert ref.product_id == "277880353"

    http = CrawlHttp(max_retries=1)
    adapter = TikiAdapter(http=http)
    try:
        product = await adapter.fetch_product(ref)
        reviews = await adapter.fetch_reviews(ref, limit=3)
    finally:
        await adapter.aclose()

    assert product.platform == "tiki"
    assert product.external_id == "277880353"
    assert product.title.strip()
    assert product.price is not None and product.price > 0
    assert product.url.startswith("https://tiki.vn/")
    assert product.images

    assert isinstance(reviews, list)
    assert len(reviews) <= 3
