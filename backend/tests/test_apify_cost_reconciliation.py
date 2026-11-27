"""R7 — cost reconciliation from the dataset summary row (offline, no Apify account).

Live run ``XhT0fRIhBJcOeZczL`` (product ``1797163842/55917277853``) was recorded as $0.0738
while Apify settled $0.1097: ``usageTotalUsd`` still said 0.0738 and ``chargedEventCounts``
still said ``{"review": 20, "run-start": 1}`` when the snapshot loop saw two equal reads,
but the dataset summary row already reported ``charged_for: 30``. Reconcile (R6) runs right
after the run, so it reads the same lagging numbers — the estimate has to come from the
dataset itself.

All HTTP goes through ``httpx.MockTransport``; the fetcher test uses ``ScriptedApify``.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.crawl.types import ParsedURL
from app.platforms.plugins.shopee.fetcher import ShopeeFetcher
from app.providers.apify import (
    APIFY_BASE_URL,
    ApifyClient,
    _estimate_cost_from_items,
)
from tests.shopee_fakes import ScriptedApify, detail_items, make_run

TOKEN = "apify_api_R7TESTTOKEN_neverLeaks_4c1d"
RUN_ID = "XhT0fRIhBJcOeZczL"
DATASET_ID = "ds-r7"
REVIEW_PRICE = 0.00359
RUN_START_PRICE = 0.002
STALE_USAGE = 0.0738
STALE_EVENTS = {"review": 20, "run-start": 1}
CHARGED_FOR = 30
SETTLED_COST = 0.1097  # 30 * 0.00359 + 0.002, what Apify really charged
STALE_COST = 0.0738  # max(usageTotalUsd, 20 * 0.00359 + 0.002) — the wrong value

PRICING = {
    "pricingPerEvent": {
        "actorChargeEvents": {
            "review": {"eventPriceUsd": REVIEW_PRICE},
            "run-start": {"eventPriceUsd": RUN_START_PRICE},
        }
    }
}


def r7_run_data(**overrides: Any) -> dict[str, Any]:
    """Run metadata as the live run reported it after the poll loop gave up."""
    data: dict[str, Any] = {
        "id": RUN_ID,
        "status": "SUCCEEDED",
        "defaultDatasetId": DATASET_ID,
        "usageTotalUsd": STALE_USAGE,
        "chargedEventCounts": dict(STALE_EVENTS),
        "pricingInfo": PRICING,
    }
    data.update(overrides)
    return data


def r7_items(*, charged_for: int = CHARGED_FOR, run_start_charged: Any = True) -> list[Any]:
    """30 review rows plus the summary row that carries the authoritative charge count."""
    rows: list[dict[str, Any]] = [
        {"review_id": f"rev-{i}", "rating": 4 if i % 2 else 5} for i in range(charged_for)
    ]
    rows.append(
        {"_type": "summary", "charged_for": charged_for, "run_start_charged": run_start_charged}
    )
    return rows


def make_apify(
    run_data: dict[str, Any],
    items: list[Any],
    *,
    poll_replies: list[dict[str, Any]] | None = None,
    sleeps: list[float] | None = None,
) -> tuple[ApifyClient, list[httpx.Request]]:
    """Client over a mock transport: start + stable metadata, one dataset page, no sleeping."""
    payload = {"data": run_data}
    replies = list(poll_replies or [])
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(200, json=payload)
        if request.url.path.endswith("/items"):
            return httpx.Response(200, json=items)
        return httpx.Response(200, json=replies.pop(0) if replies else payload)

    async def _sleep(delay: float) -> None:
        if sleeps is not None:
            sleeps.append(delay)

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=APIFY_BASE_URL,
    )
    client = ApifyClient(
        TOKEN,
        http_client=http_client,
        run_timeout_seconds=600.0,
        sleep=_sleep,
    )
    return client, requests


# ---------------------------------------------------------------------------
# (a) the real R7 numbers: stale usage + stale events vs. the summary row
# ---------------------------------------------------------------------------


async def test_lagging_usage_and_events_are_replaced_by_the_summary_estimate() -> None:
    sleeps: list[float] = []
    client, requests = make_apify(r7_run_data(), r7_items(), sleeps=sleeps)

    run = await client.run_actor("themineworks~shopee-reviews-scraper", {})

    # usage says 0.0738, events say 20 * 0.00359 + 0.002 = 0.0738 — both lag behind.
    assert max(STALE_USAGE, STALE_EVENTS["review"] * REVIEW_PRICE + RUN_START_PRICE) == (
        pytest.approx(STALE_COST)
    )
    # 30 * 0.00359 + 0.002 = 0.1097, which is what Apify actually charged.
    assert run.cost_usd == pytest.approx(SETTLED_COST, abs=1e-9)
    assert run.cost_usd > STALE_COST
    assert run.cost_estimated is True
    # The estimate is the dataset value, not a guess: recomputing it from the same rows
    # returns the same money.
    assert _estimate_cost_from_items(run.raw, run.items) == pytest.approx(SETTLED_COST)
    assert len(run.items) == CHARGED_FOR + 1

    # The cost really was re-read (non-blocking poll) before the number was settled.
    cost_reads = [r for r in requests if r.url.path == f"/v2/actor-runs/{RUN_ID}"]
    assert cost_reads, "cost must be re-read after the run finished"
    assert {dict(r.url.params)["waitForFinish"] for r in cost_reads} == {"0"}
    assert sleeps and all(delay == 2.0 for delay in sleeps)


# ---------------------------------------------------------------------------
# (b) settled numbers win (or tie) — nothing is flagged as estimated
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("usage", "events", "expected"),
    [
        # settled events tie with the estimate: max() is not > so the reported number wins
        (STALE_USAGE, {"review": CHARGED_FOR, "run-start": 1}, SETTLED_COST),
        # a settled usageTotalUsd above the estimate is authoritative
        (0.12, {"review": CHARGED_FOR, "run-start": 1}, 0.12),
    ],
)
async def test_settled_reported_cost_is_used_as_is(
    usage: float,
    events: dict[str, int],
    expected: float,
) -> None:
    client, _ = make_apify(
        r7_run_data(usageTotalUsd=usage, chargedEventCounts=events),
        r7_items(),
    )

    run = await client.run_actor("themineworks~shopee-reviews-scraper", {})

    assert run.cost_usd == pytest.approx(expected, abs=1e-9)
    assert run.cost_estimated is False


async def test_event_sum_larger_than_the_estimate_is_not_flagged_as_estimated() -> None:
    """35 charged review events (0.12765 + 0.002) beat the 30-row summary: use the events."""
    client, _ = make_apify(
        r7_run_data(
            usageTotalUsd=STALE_USAGE,
            chargedEventCounts={"review": 35, "run-start": 1},
        ),
        r7_items(),
    )

    run = await client.run_actor("themineworks~shopee-reviews-scraper", {})

    assert run.cost_usd == pytest.approx(35 * REVIEW_PRICE + RUN_START_PRICE, abs=1e-9)
    assert run.cost_estimated is False


# ---------------------------------------------------------------------------
# (c) no summary row -> count every row that is not an actor `_type`
# ---------------------------------------------------------------------------


def test_estimate_without_a_summary_row_counts_plain_rows() -> None:
    items = [{"review_id": f"rev-{i}"} for i in range(CHARGED_FOR)]
    items.append({"_type": "info"})

    assert _estimate_cost_from_items({"pricingInfo": PRICING}, items) == pytest.approx(SETTLED_COST)


async def test_row_count_is_used_when_apify_reports_neither_usage_nor_events() -> None:
    client, _ = make_apify(
        r7_run_data(usageTotalUsd=None, chargedEventCounts=None),
        [{"review_id": f"rev-{i}"} for i in range(CHARGED_FOR)],
    )

    run = await client.run_actor("themineworks~shopee-reviews-scraper", {})

    assert run.cost_usd == pytest.approx(SETTLED_COST, abs=1e-9)
    assert run.cost_estimated is True


# ---------------------------------------------------------------------------
# (d) a summary row without a `review` price must not crash the run
# ---------------------------------------------------------------------------


def test_summary_row_without_a_review_price_is_not_estimated() -> None:
    run_data = {
        "pricingInfo": {
            "pricingPerEvent": {"actorChargeEvents": {"run-start": {"eventPriceUsd": 0.002}}}
        }
    }

    assert _estimate_cost_from_items(run_data, r7_items()) is None


def test_summary_row_with_a_non_numeric_review_price_is_not_estimated() -> None:
    run_data = {
        "pricingInfo": {
            "pricingPerEvent": {
                "actorChargeEvents": {
                    "review": {"eventPriceUsd": "n/a"},
                    "run-start": {"eventPriceUsd": RUN_START_PRICE},
                }
            }
        }
    }

    assert _estimate_cost_from_items(run_data, r7_items()) is None


async def test_missing_review_price_keeps_the_reported_cost_and_does_not_raise() -> None:
    client, _ = make_apify(
        r7_run_data(
            pricingInfo={
                "pricingPerEvent": {"actorChargeEvents": {"run-start": {"eventPriceUsd": 0.002}}}
            }
        ),
        r7_items(),
    )

    run = await client.run_actor("themineworks~shopee-reviews-scraper", {})

    assert run.cost_usd == pytest.approx(STALE_USAGE, abs=1e-9)
    assert run.cost_estimated is False


# ---------------------------------------------------------------------------
# (e) the run-start fee is only added when the summary says it was charged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("run_start_charged", "expected"),
    [
        (True, CHARGED_FOR * 0.00359 + 0.002),
        (False, CHARGED_FOR * 0.00359),
        (None, CHARGED_FOR * 0.00359),
    ],
)
def test_run_start_fee_follows_the_summary_flag(
    run_start_charged: Any,
    expected: float,
) -> None:
    estimate = _estimate_cost_from_items(
        {"pricingInfo": PRICING},
        r7_items(run_start_charged=run_start_charged),
    )

    assert estimate == pytest.approx(expected)
    if run_start_charged:
        assert estimate == pytest.approx(SETTLED_COST)
    else:
        assert estimate == pytest.approx(0.1077)


# ---------------------------------------------------------------------------
# (f) fetcher — the group budgets are built from the cost that was settled
# ---------------------------------------------------------------------------


def product_ref(item_id: str, shop_id: str) -> ParsedURL:
    return ParsedURL(
        platform="shopee",
        kind="product",
        product_id=item_id,
        seller_id=shop_id,
        canonical_url=f"https://shopee.vn/product/{shop_id}/{item_id}",
    )


async def test_high_group_budget_is_the_cap_minus_the_estimated_cost_so_far() -> None:
    # 17979591922 already carries a rating in the fixture, so `fetch_product` does not spend a
    # second metadata run on the shop-card fallback.
    row = next(r for r in detail_items() if r["itemId"] == "17979591922")
    client = ScriptedApify(
        product_runs=[make_run([row], cost_usd=0.03, run_id="run-meta")],
        review_runs=[
            make_run([], cost_usd=SETTLED_COST, cost_estimated=True, run_id="run-low"),
            make_run([], cost_usd=0.04, run_id="run-high"),
        ],
    )
    fetcher = ShopeeFetcher(
        client,  # type: ignore[arg-type]
        max_cost_usd_per_link=0.30,
        window_months=12,
        low_star_max=150,
        high_star_max=30,
    )
    ref = product_ref("17979591922", "18644537")

    await fetcher.fetch_product(ref)
    await fetcher.fetch_reviews(ref)

    assert len(client.product_calls()) == 1, "the fixture row has a rating, no shop fallback"
    low_call, high_call = client.review_calls()
    assert low_call.max_charge_usd == pytest.approx(round((0.30 - 0.03) * 0.8, 4))
    # high gets everything left, counted from the *settled* 0.1097 — not the lagging 0.0738.
    assert (high_call.max_charge_usd or 0.0) == pytest.approx(round(0.30 - 0.03 - SETTLED_COST, 4))
    assert (high_call.max_charge_usd or 0.0) < round(0.30 - 0.03 - STALE_COST, 4)

    assert fetcher.cost_usd == pytest.approx(0.03 + SETTLED_COST + 0.04)
    assert fetcher.cost_usd <= 0.30
    assert fetcher.cost_estimated is True

    sampling = fetcher.sampling_metadata
    assert sampling is not None
    assert sampling["cost_usd"] == pytest.approx(0.03 + SETTLED_COST + 0.04)
    assert sampling["cost_estimated"] is True
    costs = {entry["kind"]: entry["cost_usd"] for entry in sampling["runs"]}
    assert sorted(costs) == ["high", "low", "metadata"]
    assert costs["metadata"] == pytest.approx(0.03)
    assert costs["low"] == pytest.approx(SETTLED_COST)
    assert costs["high"] == pytest.approx(0.04)
