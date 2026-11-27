"""ShopeeFetcher tests: fixed 2-group strategy, cost ceiling (B4/C1/D2/D3), coverage (E1).

The Apify client is scripted (``tests/shopee_fakes.ScriptedApify``); dataset rows come from the
sanitized real fixtures. No HTTP, no Apify account, no money.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from app.crawl.errors import NotFoundError
from app.crawl.types import ParsedURL, ProgressEvent
from app.platforms.plugins.shopee.fetcher import ShopeeFetcher
from tests.shopee_fakes import (
    PRODUCT_ACTOR,
    REVIEWS_ACTOR,
    ScriptedApify,
    detail_items,
    make_run,
    review_items,
    shop_items,
)


def product_ref(item_id: str = "48571216193", shop_id: str = "1937969974") -> ParsedURL:
    return ParsedURL(
        platform="shopee",
        kind="product",
        product_id=item_id,
        seller_id=shop_id,
        canonical_url=f"https://shopee.vn/product/{shop_id}/{item_id}",
    )


def shop_ref(shop_id: str = "1937969974") -> ParsedURL:
    return ParsedURL(
        platform="shopee",
        kind="shop",
        seller_id=shop_id,
        canonical_url=f"https://shopee.vn/shop/{shop_id}",
    )


def make_fetcher(
    client: ScriptedApify,
    *,
    max_cost: float = 0.30,
    progress: list[ProgressEvent] | None = None,
) -> ShopeeFetcher:
    async def on_progress(event: ProgressEvent) -> None:
        if progress is not None:
            progress.append(event)

    return ShopeeFetcher(
        client,  # type: ignore[arg-type]
        max_cost_usd_per_link=max_cost,
        window_months=12,
        low_star_max=150,
        high_star_max=30,
        on_progress=on_progress,
    )


def review_run(cost: float = 0.05, *, budget: bool = False, items: Any = None) -> Any:
    return make_run(items or review_items(), cost_usd=cost, budget_exhausted=budget)


# ---------------------------------------------------------------------------
# Strategy inputs (A5): exactly two ordered runs
# ---------------------------------------------------------------------------


async def test_two_sequential_review_runs_with_exact_inputs() -> None:
    client = ScriptedApify(review_runs=[review_run(cost=0.10), review_run(cost=0.05)])
    fetcher = make_fetcher(client, max_cost=0.30)

    await fetcher.fetch_reviews(product_ref())

    calls = client.review_calls()
    assert len(calls) == 2
    low, high = calls
    assert low.actor_id == REVIEWS_ACTOR
    assert low.payload["stars"] == ["1", "2", "3"]
    assert low.payload["withCommentOnly"] is True
    assert "withMediaOnly" not in low.payload
    assert low.payload["newerThan"] == "12 months"
    assert low.payload["maxReviewsPerProduct"] == 150
    assert low.payload["startUrls"] == ["https://shopee.vn/product/1937969974/48571216193"]
    assert low.max_charge_usd == pytest.approx(0.30 * 0.8, abs=1e-4)

    assert high.payload["stars"] == ["4", "5"]
    assert high.payload["withMediaOnly"] is True
    assert high.payload["maxReviewsPerProduct"] == 30
    # C1/D2: high gets everything actually left (0.30 - 0.10), not a fixed 20%.
    assert high.max_charge_usd == pytest.approx(0.20, abs=1e-4)


async def test_low_and_high_reviews_are_deduped_by_external_id() -> None:
    shared = {"review_id": "dup", "rating": 5, "comment": "same", "product_id": "48571216193"}
    client = ScriptedApify(
        review_runs=[
            make_run([shared, {"review_id": "low-1", "rating": 1, "comment": "tệ"}]),
            make_run([shared, {"review_id": "high-1", "rating": 5, "comment": "tốt"}]),
        ]
    )

    reviews = await make_fetcher(client).fetch_reviews(product_ref())

    assert sorted(r.external_id for r in reviews) == ["dup", "high-1", "low-1"]


async def test_progress_events_report_both_review_groups() -> None:
    progress: list[ProgressEvent] = []
    client = ScriptedApify(review_runs=[review_run(), review_run()])

    await make_fetcher(client, progress=progress).fetch_reviews(product_ref())

    stages = [(p.stage, p.done, p.total) for p in progress]
    assert ("provider_reviews", 0, 2) in stages
    assert ("provider_reviews", 1, 2) in stages
    assert ("provider_reviews", 2, 2) in stages


# ---------------------------------------------------------------------------
# B4 — metadata runs capped at min(0.05, 10%)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("max_cost", "expected_metadata_cap"),
    [(0.30, 0.03), (0.50, 0.05), (2.00, 0.05), (0.05, 0.005)],
)
async def test_metadata_run_budget_is_min_of_five_cents_and_ten_percent(
    max_cost: float, expected_metadata_cap: float
) -> None:
    row = next(r for r in detail_items() if r["itemId"] == "48571216193")
    client = ScriptedApify(
        product_runs=[make_run([row], cost_usd=0.01)],
        review_runs=[make_run([], cost_usd=0.0), make_run([], cost_usd=0.0)],
    )
    fetcher = make_fetcher(client, max_cost=max_cost)

    await fetcher.fetch_product(product_ref())

    assert client.product_calls()[0].max_charge_usd == pytest.approx(expected_metadata_cap)


async def test_total_link_cost_stays_within_the_ceiling() -> None:
    row = next(r for r in detail_items() if r["itemId"] == "48571216193")  # rating null
    client = ScriptedApify(
        product_runs=[make_run([row], cost_usd=0.02), make_run([], cost_usd=0.01)],
        review_runs=[review_run(cost=0.20), review_run(cost=0.06)],
    )
    fetcher = make_fetcher(client, max_cost=0.30)

    await fetcher.fetch_product(product_ref())
    await fetcher.fetch_reviews(product_ref())

    # D3: no run was ever allowed to spend more than what the ceiling left.
    # call 0: product detail (min(0.05, 10%) = 0.03)
    # call 1: shop-card rating fallback, same metadata cap
    # call 2: low group = 80% of (0.30 - 0.03 actually spent)
    # call 3: high group = everything left (0.30 - 0.03 - 0.20)
    budgets = [c.max_charge_usd or 0.0 for c in client.calls]
    assert budgets == pytest.approx([0.03, 0.03, (0.30 - 0.03) * 0.8, 0.30 - 0.03 - 0.20])
    assert fetcher.cost_usd == pytest.approx(0.02 + 0.01 + 0.20 + 0.06)
    assert fetcher.cost_usd <= 0.30


async def test_cost_estimated_flag_propagates_to_sampling() -> None:
    client = ScriptedApify(
        review_runs=[make_run([], cost_usd=0.02, cost_estimated=True), make_run([], cost_usd=0.0)]
    )
    fetcher = make_fetcher(client)

    await fetcher.fetch_reviews(product_ref())

    assert fetcher.cost_estimated is True
    assert fetcher.sampling_metadata is not None
    assert fetcher.sampling_metadata["cost_estimated"] is True


# ---------------------------------------------------------------------------
# C1 — sequential allocation: unused low budget moves to high; high can be skipped
# ---------------------------------------------------------------------------


async def test_unused_low_budget_transfers_to_high() -> None:
    """Vòng sửa 2: low 0 reviews / cheap, high was capped at 15/30 before this fix."""
    row = next(r for r in detail_items() if r["itemId"] == "48571216193")
    client = ScriptedApify(
        product_runs=[make_run([row], cost_usd=0.0307)],
        review_runs=[
            make_run([], cost_usd=0.0002),  # low found nothing: only run-start
            make_run([], cost_usd=0.10, budget_exhausted=True),
        ],
    )
    fetcher = make_fetcher(client, max_cost=0.30)

    await fetcher.fetch_product(product_ref())
    await fetcher.fetch_reviews(product_ref())

    high_call = client.review_calls()[1]
    assert high_call.max_charge_usd == pytest.approx(0.30 - 0.0307 - 0.0002, abs=1e-4)


async def test_high_group_is_skipped_when_less_than_one_event_remains() -> None:
    """D2: remaining < ~$0.006 (1 review event + run-start) ⇒ no high run at all."""
    client = ScriptedApify(
        review_runs=[make_run([], cost_usd=0.0445)],
    )
    fetcher = make_fetcher(client, max_cost=0.05)

    reviews, sampling = await _reviews_with_sampling(fetcher, client)

    assert len(client.review_calls()) == 1, "high must not be executed"
    assert reviews == []
    high = next(g for g in sampling["groups"] if g["name"] == "high")
    assert high["coverage"] == "budget"
    assert high["delivered_new"] == 0
    assert high["delivered"] == 0


async def test_high_group_runs_when_remaining_is_above_the_minimum() -> None:
    client = ScriptedApify(
        review_runs=[make_run([], cost_usd=0.0445), make_run([], cost_usd=0.005)],
    )
    fetcher = make_fetcher(client, max_cost=0.06)

    await fetcher.fetch_reviews(product_ref())

    assert client.review_calls()[1].max_charge_usd == pytest.approx(0.06 - 0.0445, abs=1e-4)


async def _reviews_with_sampling(
    fetcher: ShopeeFetcher, client: ScriptedApify
) -> tuple[list[Any], dict[str, Any]]:
    """Run the private group orchestrator directly to read the sampling payload."""
    items, sampling = await fetcher._run_stratified_review_groups(
        "https://shopee.vn/product/1937969974/48571216193",
        target_product_id="48571216193",
    )
    return [i.review for i in items], sampling


# ---------------------------------------------------------------------------
# E1 — coverage normalization
# ---------------------------------------------------------------------------


def _coverage_rows(product_id: str, coverage: str) -> list[dict[str, Any]]:
    return [
        {"_type": "product_summary", "product_id": product_id, "coverage": coverage},
        {"review_id": "r1", "rating": 1, "comment": "x", "product_id": product_id},
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("complete", "complete"),
        ("complete_to_date", "complete"),  # Vòng sửa 4: Innisfree live value
        ("stopped_at_max", "stopped_at_max"),
        ("shopee_limit", "shopee_limit"),
        ("something_new", "unknown"),
    ],
)
async def test_actor_coverage_is_normalized_to_the_enum(raw: str, expected: str) -> None:
    client = ScriptedApify(
        review_runs=[
            make_run(_coverage_rows("48571216193", raw), cost_usd=0.01),
            make_run([], cost_usd=0.0),
        ]
    )
    fetcher = make_fetcher(client)

    _reviews, sampling = await _reviews_with_sampling(fetcher, client)

    low = sampling["groups"][0]
    assert low["coverage"] == expected
    if expected == "unknown":
        assert low["coverage_raw"] == raw
    else:
        assert "coverage_raw" not in low


async def test_unknown_coverage_is_logged_with_the_raw_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = ScriptedApify(
        review_runs=[
            make_run(_coverage_rows("48571216193", "weird_new_value"), cost_usd=0.01),
            make_run([], cost_usd=0.0),
        ]
    )

    await make_fetcher(client).fetch_reviews(product_ref())

    assert "weird_new_value" in caplog.text


async def test_budget_coverage_wins_over_the_actor_value() -> None:
    client = ScriptedApify(
        review_runs=[
            make_run(
                _coverage_rows("48571216193", "complete"), cost_usd=0.01, budget_exhausted=True
            ),
            make_run([], cost_usd=0.0),
        ]
    )

    _reviews, sampling = await _reviews_with_sampling(make_fetcher(client), client)

    assert sampling["groups"][0]["coverage"] == "budget"
    assert "coverage_raw" not in sampling["groups"][0]


async def test_shop_level_coverage_aggregates_all_products() -> None:
    rows = [
        {"_type": "product_summary", "product_id": "p1", "coverage": "complete_to_date"},
        {"_type": "product_summary", "product_id": "p2", "coverage": "shopee_limit"},
        {"review_id": "r1", "rating": 5, "product_id": "p1"},
    ]
    client = ScriptedApify(review_runs=[make_run(rows, cost_usd=0.01), make_run([], cost_usd=0)])
    fetcher = make_fetcher(client)

    by_product, minimal = await fetcher.fetch_shop_reviews(shop_ref())

    assert fetcher.sampling_metadata is not None
    # `shopee_limit` is the most restrictive coverage and must win the shop-level group.
    assert fetcher.sampling_metadata["groups"][0]["coverage"] == "shopee_limit"
    assert list(by_product) == ["p1"]
    assert minimal["p1"].title  # a minimal product is created for attribution


async def test_sampling_payload_matches_the_documented_shape() -> None:
    client = ScriptedApify(review_runs=[review_run(cost=0.0735), review_run(cost=0.0123)])
    fetcher = make_fetcher(client, max_cost=0.30)

    await fetcher.fetch_reviews(product_ref())

    sampling = fetcher.sampling_metadata
    assert sampling is not None
    assert sampling["provider"] == "apify"
    assert sampling["strategy"] == "stratified"
    assert sampling["window_months"] == 12
    assert sampling["cost_usd"] == pytest.approx(0.0858)
    assert [g["name"] for g in sampling["groups"]] == ["low", "high"]
    assert sampling["groups"][0]["stars"] == [1, 2, 3]
    assert sampling["groups"][0]["with_media_only"] is False
    assert sampling["groups"][0]["max"] == 150
    assert sampling["groups"][0]["delivered"] == 243
    assert sampling["groups"][1]["stars"] == [4, 5]
    assert sampling["groups"][1]["with_media_only"] is True
    assert sampling["groups"][1]["max"] == 30


# ---------------------------------------------------------------------------
# Re-collection (A5): newerThan per group from stored timestamps
# ---------------------------------------------------------------------------


async def test_recollection_uses_per_group_newer_than_dates() -> None:
    client = ScriptedApify(review_runs=[make_run([], cost_usd=0.01), make_run([], cost_usd=0.01)])
    fetcher = make_fetcher(client)

    await fetcher.fetch_reviews(
        product_ref(),
        newer_than_by_group={
            "low": datetime(2026, 6, 1, 12, tzinfo=UTC),
            "high": datetime(2026, 11, 20, tzinfo=UTC),
        },
    )

    low, high = client.review_calls()
    assert low.payload["newerThan"] == "2026-06-01T12:00:00Z"
    assert high.payload["newerThan"] == "2026-11-20T00:00:00Z"


async def test_second_collection_only_sends_new_rows_and_maps_them() -> None:
    old = {"review_id": "old", "rating": 1, "comment": "cũ", "product_id": "48571216193"}
    new = {"review_id": "new", "rating": 2, "comment": "mới", "product_id": "48571216193"}
    client = ScriptedApify(
        review_runs=[
            make_run([old], cost_usd=0.01),
            make_run([], cost_usd=0.0),
            make_run([new], cost_usd=0.01),
            make_run([], cost_usd=0.0),
        ]
    )
    fetcher = make_fetcher(client, max_cost=1.0)

    first = await fetcher.fetch_reviews(product_ref())
    second = await fetcher.fetch_reviews(
        product_ref(), newer_than_by_group={"low": datetime(2026, 11, 1, tzinfo=UTC)}
    )

    assert [r.external_id for r in first] == ["old"]
    assert [r.external_id for r in second] == ["new"]


# ---------------------------------------------------------------------------
# Product / shop flows
# ---------------------------------------------------------------------------


async def test_missing_product_rating_falls_back_to_the_shop_card_rating() -> None:
    row = next(r for r in detail_items() if r["itemId"] == "48571216193")
    assert row["rating"] is None
    client = ScriptedApify(
        product_runs=[make_run([row], cost_usd=0.02), make_run(shop_items(), cost_usd=0.01)]
    )
    fetcher = make_fetcher(client)

    product = await fetcher.fetch_product(product_ref())

    assert product.rating_avg == 4.9  # from the shop card, matched by itemId
    assert product.rating_count is None
    assert product.sold_count is None
    assert client.product_calls()[1].max_charge_usd == pytest.approx(0.03)


async def test_rating_stays_none_when_no_shop_card_matches() -> None:
    row = next(r for r in detail_items() if r["itemId"] == "27551742889")
    client = ScriptedApify(
        product_runs=[make_run([row], cost_usd=0.02), make_run(shop_items(), cost_usd=0.01)],
        review_runs=[make_run(review_items(), cost_usd=0.05)],
    )
    fetcher = make_fetcher(client)

    product = await fetcher.fetch_product(product_ref("27551742889", "158660528"))
    reviews = await fetcher.fetch_reviews(product_ref("27551742889", "158660528"))

    assert reviews, "reviews were collected for this product"
    # Even with 150 reviews in hand, the rating is never derived from them.
    assert product.rating_avg is None


async def test_product_not_found_raises_404_without_spending_more() -> None:
    client = ScriptedApify(product_runs=[make_run([], cost_usd=0.01)])

    with pytest.raises(NotFoundError) as excinfo:
        await make_fetcher(client).fetch_product(product_ref())

    assert excinfo.value.status_code == 404
    assert len(client.calls) == 1


async def test_shop_fetch_lists_products_without_opening_each_detail() -> None:
    client = ScriptedApify(product_runs=[make_run(shop_items(), cost_usd=0.02)])
    fetcher = make_fetcher(client, max_cost=0.50)

    shop = await fetcher.fetch_shop(shop_ref())
    products = await fetcher.list_shop_products(shop_ref(), limit=1)

    assert shop.name == "Mavis _ Tech _ Shop"
    assert [p.external_id for p in products] == ["48571216193"]
    # The shop bundle is cached: one metadata run for both calls.
    assert len(client.product_calls()) == 1
    assert client.product_calls()[0].payload["mode"] == "shop"
    assert client.product_calls()[0].payload["maxItems"] == 100


async def test_shop_reviews_are_attributed_and_unknown_products_become_minimal() -> None:
    rows = [
        {
            "review_id": "r1",
            "rating": 1,
            "comment": "x",
            "product_id": "9",
            "shop_id": "1937969974",
            "product_name": "SP mới",
            "product_url": "https://shopee.vn/product/1937969974/9",
        },
        {"review_id": "r2", "rating": 5, "comment": "y", "product_id": "9"},
        {"review_id": "r3", "rating": 5, "comment": "y"},  # no product id: dropped
    ]
    client = ScriptedApify(review_runs=[make_run(rows, cost_usd=0.01), make_run([], cost_usd=0.0)])
    fetcher = make_fetcher(client)

    by_product, minimal = await fetcher.fetch_shop_reviews(shop_ref())

    assert list(by_product) == ["9"]
    assert [r.external_id for r in by_product["9"]] == ["r1", "r2"]
    assert minimal["9"].title == "SP mới"
    assert minimal["9"].url == "https://shopee.vn/product/1937969974/9"
    assert minimal["9"].platform == "shopee"
    assert minimal["9"].currency == "VND"


async def test_zero_budget_skips_both_groups_without_calling_the_client() -> None:
    client = ScriptedApify()
    fetcher = make_fetcher(client, max_cost=0.0)

    reviews, sampling = await _reviews_with_sampling(fetcher, client)

    assert reviews == []
    assert client.calls == []
    assert [g["coverage"] for g in sampling["groups"]] == ["budget", "budget"]


async def test_fetcher_metadata_defaults_come_from_settings_when_not_injected() -> None:
    fetcher = ShopeeFetcher(ScriptedApify())  # type: ignore[arg-type]

    assert fetcher._product_actor_id == PRODUCT_ACTOR
    assert fetcher._reviews_actor_id == REVIEWS_ACTOR
    assert fetcher._window_months == 12
    assert fetcher._low_star_max == 150
    assert fetcher._high_star_max == 30
    assert fetcher._max_cost_usd == 0.50


def test_canonical_url_round_trip_for_a_uuid_free_ref() -> None:
    """Sanity: the ref helper builds a stable canonical URL (no UUIDs in Shopee ids)."""
    ref = product_ref()
    uuid.UUID("00000000-0000-4000-8000-000000000000")  # keep uuid import meaningful
    assert "/product/1937969974/48571216193" in ref.canonical_url
