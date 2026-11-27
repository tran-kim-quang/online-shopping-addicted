"""Rating breakdown from the actor summary (R1/R2/R6) — offline unit tests.

Covers three layers without any network or database:

* ``ThemineworksReviewsMapper.parse_items`` → ``star_counts_by_product`` from the sanitized
  real fixtures plus synthetic "keyboard" rows described in the task spec;
* ``ShopeeFetcher`` → ``rating_breakdowns_by_product`` (since / until_by_group / skipped
  groups), full ISO ``newerThan`` and the ``sampling.runs`` cost reconciliation;
* the pure incremental-merge helpers in ``app.crawl.repository``.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from app.crawl.repository import extract_newer_than_from_breakdowns, merge_rating_breakdown
from app.crawl.types import ParsedURL
from app.platforms.plugins.shopee.fetcher import ShopeeFetcher
from app.platforms.plugins.shopee.mappers import ThemineworksReviewsMapper
from tests.shopee_fakes import (
    ScriptedApify,
    load_fixture,
    low_star_items,
    make_run,
    review_items,
)

ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
PRODUCT_ID = "48571216193"


# ---------------------------------------------------------------------------
# R1 — mapper: per-product star counts from the real fixtures
# ---------------------------------------------------------------------------


def review_4products_rows() -> list[dict[str, Any]]:
    return review_items()


def summary_row(rows: list[dict[str, Any]], product_id: str) -> dict[str, Any]:
    return next(
        row
        for row in rows
        if row.get("_type") == "product_summary" and row.get("product_id") == product_id
    )


def test_four_product_fixture_marks_complete_stopped_and_limit_reached() -> None:
    rows = review_4products_rows()

    parsed = ThemineworksReviewsMapper.parse_items(rows)

    complete = parsed.star_counts_by_product["48571216193"]
    assert {star: (sc.count, sc.complete) for star, sc in complete.items()} == {
        1: (1, True),
        2: (0, True),
        3: (0, True),
        4: (3, True),
        5: (50, True),
    }

    empty = parsed.star_counts_by_product["49867965494"]
    assert {star: sc.count for star, sc in empty.items()} == {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    assert all(sc.complete for sc in empty.values()), "a product with 0 reviews is still complete"

    stopped = parsed.star_counts_by_product["27551742889"]
    assert {star: sc.count for star, sc in stopped.items()} == {1: 2, 2: 0, 3: 2, 4: 23, 5: 123}
    # star_lists says stopped (1=1..4) and limit_reached (5): every star is a lower bound.
    assert not any(sc.complete for sc in stopped.values())

    limited = parsed.star_counts_by_product["17979591922"]
    assert {star: sc.count for star, sc in limited.items()} == {
        1: 6,
        2: 10,
        3: 6,
        4: 9,
        5: 8,
    }
    assert not any(sc.complete for sc in limited.values())


def test_comment_only_fixture_counts_rating_events_not_delivered_reviews() -> None:
    rows = load_fixture("apify_themineworks_reviews_comment_only_vn.json")

    parsed = ThemineworksReviewsMapper.parse_items(rows)

    counts = parsed.star_counts_by_product["48571216193"]
    assert sum(sc.count for sc in counts.values()) == 54
    assert len(parsed.items) == 20, "the fixture bills 20 reviews for 54 matched ratings"
    assert counts[5].count == 50
    assert all(sc.complete for sc in counts.values())


def test_stars123_fixture_skips_not_requested_stars() -> None:
    parsed = ThemineworksReviewsMapper.parse_items(low_star_items())

    counts = parsed.star_counts_by_product["27551742889"]

    assert sorted(counts) == [1, 2, 3], "4★/5★ were not_requested and must not be recorded"
    assert {star: sc.count for star, sc in counts.items()} == {1: 3, 2: 3, 3: 15}
    assert not any(sc.complete for sc in counts.values())
    # The summary row echoes stars [1,2,3]; it must not re-add 4/5 through a fallback path.
    assert set(counts) == {1, 2, 3}


def test_budget_exhausted_run_marks_every_star_incomplete() -> None:
    rows = [
        {
            "_type": "product_summary",
            "product_id": PRODUCT_ID,
            "ratings_found_by_star": {"1": 1, "2": 0, "3": 0, "4": 3, "5": 50},
            "star_lists": {str(s): "complete" for s in range(1, 6)},
            "coverage": "complete",
        },
        {"_type": "summary", "billing": {"budget_exhausted": True}},
        {"review_id": "r1", "rating": 5, "comment": "ok", "product_id": PRODUCT_ID},
    ]

    parsed = ThemineworksReviewsMapper.parse_items(rows)

    assert parsed.budget_exhausted is True
    counts = parsed.star_counts_by_product[PRODUCT_ID]
    assert {star: sc.count for star, sc in counts.items()} == {1: 1, 2: 0, 3: 0, 4: 3, 5: 50}
    assert not any(sc.complete for sc in counts.values()), "budget_exhausted overrides star_lists"


def test_missing_star_lists_falls_back_to_counts_complete_for_requested_stars() -> None:
    rows = [
        {
            "_type": "product_summary",
            "product_id": PRODUCT_ID,
            "ratings_found_by_star": {"1": 0, "2": 0, "3": 4, "4": 7, "5": 9},
            "counts_complete": True,
            "coverage": "complete",
        },
        {"_type": "summary", "stars": [1, 2, 3]},
    ]

    parsed = ThemineworksReviewsMapper.parse_items(rows)

    counts = parsed.star_counts_by_product[PRODUCT_ID]
    assert sorted(counts) == [1, 2, 3], "the summary row arrives after the product row"
    assert {star: sc.count for star, sc in counts.items()} == {1: 0, 2: 0, 3: 4}
    assert all(sc.complete for sc in counts.values())


def test_missing_star_lists_with_counts_incomplete_is_partial() -> None:
    rows = [
        {
            "_type": "product_summary",
            "product_id": PRODUCT_ID,
            "ratings_found_by_star": {"1": 2, "2": 0, "3": 1},
            "counts_complete": False,
        },
        {"_type": "summary", "stars": [1, 2, 3]},
    ]

    parsed = ThemineworksReviewsMapper.parse_items(rows)

    counts = parsed.star_counts_by_product[PRODUCT_ID]
    assert {star: sc.count for star, sc in counts.items()} == {1: 2, 2: 0, 3: 1}
    assert not any(sc.complete for sc in counts.values())


def test_unknown_star_list_status_is_incomplete_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rows = [
        {
            "_type": "product_summary",
            "product_id": PRODUCT_ID,
            "ratings_found_by_star": {"1": 4, "5": 6},
            "star_lists": {"1": "brand_new_status", "5": "complete"},
        }
    ]

    parsed = ThemineworksReviewsMapper.parse_items(rows)

    counts = parsed.star_counts_by_product[PRODUCT_ID]
    assert counts[1].count == 4
    assert counts[1].complete is False, "an unknown status is never treated as complete"
    assert counts[5].complete is True
    assert "brand_new_status" in caplog.text


def test_invalid_or_negative_counts_drop_that_star() -> None:
    rows = [
        {
            "_type": "product_summary",
            "product_id": PRODUCT_ID,
            "ratings_found_by_star": {"1": -3, "2": "many", "3": None, "4": 7},
            "star_lists": {str(s): "complete" for s in range(1, 6)},
        }
    ]

    parsed = ThemineworksReviewsMapper.parse_items(rows)

    counts = parsed.star_counts_by_product[PRODUCT_ID]
    assert sorted(counts) == [4]
    assert counts[4].count == 7


def test_keyboard_case_low_zero_and_high_five_star_one_with_nothing_delivered() -> None:
    """Run real shape from the task spec (product 94273103/50219741972)."""
    low_rows = [
        {
            "_type": "product_summary",
            "product_id": "50219741972",
            "coverage": "complete_to_date",
            "ratings_found_by_star": {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0},
            "counts_complete": True,
            "star_lists": {
                "1": "complete_to_date",
                "2": "complete_to_date",
                "3": "complete_to_date",
                "4": "not_requested",
                "5": "not_requested",
            },
        },
        {
            "_type": "summary",
            "stars": [1, 2, 3],
            "reviews_delivered": 0,
            "ratings_found_by_star": {},
            "stopped_by_max_cost": False,
            "billing": {"budget_exhausted": False},
        },
        {"_type": "info"},
    ]
    high_rows = [
        {
            "_type": "product_summary",
            "product_id": "50219741972",
            "coverage": "complete_to_date",
            "ratings_found_by_star": {"4": 0, "5": 1},
            "counts_complete": True,
            "star_lists": {
                "1": "not_requested",
                "2": "not_requested",
                "3": "not_requested",
                "4": "complete_to_date",
                "5": "complete_to_date",
            },
        },
        {
            "_type": "summary",
            "stars": [4, 5],
            "reviews_delivered": 0,
            "stopped_by_max_cost": False,
            "billing": {"budget_exhausted": False},
        },
        {"_type": "info"},
    ]

    low = ThemineworksReviewsMapper.parse_items(low_rows, requested_stars=[1, 2, 3])
    high = ThemineworksReviewsMapper.parse_items(high_rows, requested_stars=[4, 5])

    low_counts = low.star_counts_by_product["50219741972"]
    assert {star: (sc.count, sc.complete) for star, sc in low_counts.items()} == {
        1: (0, True),
        2: (0, True),
        3: (0, True),
    }
    high_counts = high.star_counts_by_product["50219741972"]
    assert {star: (sc.count, sc.complete) for star, sc in high_counts.items()} == {
        4: (0, True),
        5: (1, True),
    }
    assert low.items == [] and high.items == [], "0 reviews delivered in both groups"


def test_five_star_rows_without_star_lists_and_without_summary_stay_conservative() -> None:
    rows = [
        {
            "_type": "product_summary",
            "product_id": PRODUCT_ID,
            "ratings_found_by_star": {"1": 0, "2": 0, "3": 0, "4": 0, "5": 3},
            "counts_complete": True,
        },
        {"_type": "info"},
    ]

    parsed = ThemineworksReviewsMapper.parse_items(rows)

    counts = parsed.star_counts_by_product[PRODUCT_ID]
    assert sorted(counts) == [1, 2, 3, 4, 5], "no summary/requested stars ⇒ all five stars"
    assert all(sc.complete for sc in counts.values())


# ---------------------------------------------------------------------------
# Fetcher doubles: scripted Apify client that also answers GET /actor-runs
# ---------------------------------------------------------------------------


@dataclass
class ReconcilingApify(ScriptedApify):
    """``ScriptedApify`` plus the reconcile read (``get_run_cost``)."""

    run_costs: dict[str, float] = field(default_factory=dict)
    failing_runs: set[str] = field(default_factory=set)
    cost_reads: list[str] = field(default_factory=list)

    async def get_run_cost(self, run_id: str) -> float | None:
        self.cost_reads.append(run_id)
        if run_id in self.failing_runs:
            raise httpx.ConnectError("apify unreachable")
        return self.run_costs.get(run_id)


def product_ref(item_id: str = PRODUCT_ID, shop_id: str = "1937969974") -> ParsedURL:
    return ParsedURL(
        platform="shopee",
        kind="product",
        product_id=item_id,
        seller_id=shop_id,
        canonical_url=f"https://shopee.vn/product/{shop_id}/{item_id}",
    )


def make_fetcher(client: Any, *, max_cost: float = 0.30) -> ShopeeFetcher:
    return ShopeeFetcher(
        client,
        max_cost_usd_per_link=max_cost,
        window_months=12,
        low_star_max=150,
        high_star_max=30,
    )


def keyboard_rows(product_id: str, *, group: str) -> list[dict[str, Any]]:
    if group == "low":
        return [*_keyboard_low_rows(product_id)]
    return [*_keyboard_high_rows(product_id)]


def _keyboard_low_rows(product_id: str) -> list[dict[str, Any]]:
    return [
        {
            "_type": "product_summary",
            "product_id": product_id,
            "coverage": "complete_to_date",
            "ratings_found_by_star": {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0},
            "counts_complete": True,
            "star_lists": {
                "1": "complete_to_date",
                "2": "complete_to_date",
                "3": "complete_to_date",
                "4": "not_requested",
                "5": "not_requested",
            },
        },
        {"_type": "summary", "stars": [1, 2, 3], "reviews_delivered": 0},
        {"_type": "info"},
    ]


def _keyboard_high_rows(product_id: str) -> list[dict[str, Any]]:
    return [
        {
            "_type": "product_summary",
            "product_id": product_id,
            "coverage": "complete_to_date",
            "ratings_found_by_star": {"4": 0, "5": 1},
            "counts_complete": True,
            "star_lists": {
                "1": "not_requested",
                "2": "not_requested",
                "3": "not_requested",
                "4": "complete_to_date",
                "5": "complete_to_date",
            },
        },
        {"_type": "summary", "stars": [4, 5], "reviews_delivered": 0},
        {"_type": "info"},
    ]


# ---------------------------------------------------------------------------
# R2 — since / until_by_group / stars per product
# ---------------------------------------------------------------------------


async def test_breakdown_uses_newer_than_as_since_and_run_start_as_until() -> None:
    keyboard_id = "50219741972"
    client = ScriptedApify(
        review_runs=[
            make_run(_keyboard_low_rows(keyboard_id), cost_usd=0.002, run_id="run-low"),
            make_run(_keyboard_high_rows(keyboard_id), cost_usd=0.002, run_id="run-high"),
        ]
    )
    fetcher = make_fetcher(client)

    before = datetime.now(UTC).replace(microsecond=0)
    await fetcher.fetch_reviews(
        product_ref(keyboard_id, "94273103"),
        newer_than_by_group={"low": datetime(2025, 11, 27, 5, 13, 58, tzinfo=UTC)},
    )
    after = datetime.now(UTC)

    breakdown = fetcher.rating_breakdowns_by_product[keyboard_id]
    assert breakdown["source"] == "apify_review_summary"
    assert breakdown["since"] == "2025-11-27T05:13:58Z"

    until_by_group = breakdown["until_by_group"]
    assert set(until_by_group) == {"low", "high"}
    assert all(ISO_UTC_RE.match(value) for value in until_by_group.values())
    low_until = datetime.fromisoformat(until_by_group["low"].replace("Z", "+00:00"))
    high_until = datetime.fromisoformat(until_by_group["high"].replace("Z", "+00:00"))
    assert before <= low_until <= high_until <= after
    assert breakdown["until"] == min(until_by_group.values())

    assert breakdown["stars"] == {
        "1": {"count": 0, "complete": True},
        "2": {"count": 0, "complete": True},
        "3": {"count": 0, "complete": True},
        "4": {"count": 0, "complete": True},
        "5": {"count": 1, "complete": True},
    }


async def test_first_crawl_since_is_the_window_start_when_no_breakdown_exists() -> None:
    client = ScriptedApify(
        review_runs=[
            make_run(_keyboard_low_rows(PRODUCT_ID), cost_usd=0.01, run_id="run-low"),
            make_run(_keyboard_high_rows(PRODUCT_ID), cost_usd=0.01, run_id="run-high"),
        ]
    )
    fetcher = make_fetcher(client)

    before = datetime.now(UTC).replace(microsecond=0)
    await fetcher.fetch_reviews(product_ref())
    after = datetime.now(UTC)

    since = fetcher.rating_breakdowns_by_product[PRODUCT_ID]["since"]
    assert ISO_UTC_RE.match(since)
    since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
    lower = _subtract_months_local(before, 12)
    upper = _subtract_months_local(after, 12)
    assert lower <= since_dt <= upper
    assert since_dt.tzinfo is UTC


async def test_group_skipped_by_budget_writes_no_stars_and_no_until() -> None:
    """Remaining < ~$0.006 after `low` ⇒ `high` never starts, so 4–5★ are not invented."""
    client = ScriptedApify(
        review_runs=[
            make_run(low_star_items(), cost_usd=0.0445, run_id="run-low"),
        ]
    )
    fetcher = make_fetcher(client, max_cost=0.05)

    await fetcher.fetch_reviews(product_ref("27551742889", "158660528"))

    assert len(client.review_calls()) == 1
    breakdown = fetcher.rating_breakdowns_by_product["27551742889"]
    assert sorted(breakdown["stars"]) == ["1", "2", "3"]
    assert set(breakdown["until_by_group"]) == {"low"}
    assert breakdown["until"] == breakdown["until_by_group"]["low"]


async def test_newer_than_is_sent_as_a_full_iso_datetime_in_utc() -> None:
    client = ScriptedApify(
        review_runs=[
            make_run([], cost_usd=0.01, run_id="run-low"),
            make_run([], cost_usd=0.01, run_id="run-high"),
        ]
    )
    fetcher = make_fetcher(client)

    await fetcher.fetch_reviews(
        product_ref(),
        newer_than_by_group={
            "low": datetime(2026, 6, 1, 19, 30, 15, 987654, tzinfo=timezone(timedelta(hours=7))),
            "high": datetime(2026, 11, 20, 0, 0, 0, tzinfo=UTC),
        },
    )

    low, high = client.review_calls()
    assert low.payload["newerThan"] == "2026-06-01T12:30:15Z"
    assert high.payload["newerThan"] == "2026-11-20T00:00:00Z"
    assert ISO_UTC_RE.match(low.payload["newerThan"])


async def test_sampling_runs_never_store_the_actor_token() -> None:
    client = ScriptedApify(
        product_runs=[
            make_run(
                [{"itemId": "48571216193", "name": "X", "rating": 4.9, "shopId": "1937969974"}],
                cost_usd=0.002,
                run_id="run-meta",
            )
        ],
        review_runs=[
            make_run(_keyboard_low_rows(PRODUCT_ID), cost_usd=0.00359, run_id="run-low"),
            make_run(_keyboard_high_rows(PRODUCT_ID), cost_usd=0.002, run_id="run-high"),
        ],
    )
    fetcher = make_fetcher(client)

    await fetcher.fetch_product(product_ref())
    await fetcher.fetch_reviews(product_ref())

    sampling = fetcher.sampling_metadata
    assert sampling is not None
    runs = sampling["runs"]
    assert [run["kind"] for run in runs] == ["metadata", "low", "high"]
    assert [run["run_id"] for run in runs] == ["run-meta", "run-low", "run-high"]
    assert [run["cost_usd"] for run in runs] == pytest.approx([0.002, 0.00359, 0.002])
    assert set(runs[0]) == {"kind", "run_id", "cost_usd"}
    assert "token" not in repr(sampling).lower()


# ---------------------------------------------------------------------------
# R6 — cost reconciliation
# ---------------------------------------------------------------------------


async def test_late_snapshot_is_reconciled_to_the_billed_cost() -> None:
    client = ReconcilingApify(
        review_runs=[
            make_run(_keyboard_low_rows(PRODUCT_ID), cost_usd=0.00285, run_id="run-low"),
            make_run(_keyboard_high_rows(PRODUCT_ID), cost_usd=0.0, run_id="run-high"),
        ],
        run_costs={"run-low": 0.00345, "run-high": 0.0},
    )
    fetcher = make_fetcher(client)

    await fetcher.fetch_reviews(product_ref())

    assert fetcher.cost_usd == pytest.approx(0.00345)
    assert fetcher.sampling_metadata is not None
    assert fetcher.sampling_metadata["cost_usd"] == pytest.approx(0.00345)
    runs = {run["run_id"]: run["cost_usd"] for run in fetcher.sampling_metadata["runs"]}
    assert runs == {"run-low": 0.00345, "run-high": 0.0}
    assert client.cost_reads == ["run-low", "run-high"], "each run is re-read exactly once"


async def test_reconcile_never_lowers_a_cost_that_was_already_higher() -> None:
    client = ReconcilingApify(
        review_runs=[
            make_run(_keyboard_low_rows(PRODUCT_ID), cost_usd=0.004, run_id="run-low"),
            make_run(_keyboard_high_rows(PRODUCT_ID), cost_usd=0.0, run_id="run-high"),
        ],
        run_costs={"run-low": 0.00345, "run-high": 0.0},
    )
    fetcher = make_fetcher(client)

    await fetcher.fetch_reviews(product_ref())

    assert client.cost_reads == ["run-low", "run-high"]
    assert fetcher.cost_usd == pytest.approx(0.004), "reconcile takes max(), never the minimum"


async def test_reconcile_network_error_keeps_the_previous_cost(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = ReconcilingApify(
        review_runs=[
            make_run(_keyboard_low_rows(PRODUCT_ID), cost_usd=0.00285, run_id="run-low"),
            make_run(_keyboard_high_rows(PRODUCT_ID), cost_usd=0.0, run_id="run-high"),
        ],
        failing_runs={"run-low"},
        run_costs={"run-high": 0.0},
    )
    fetcher = make_fetcher(client)

    reviews = await fetcher.fetch_reviews(product_ref())

    assert reviews == []
    assert fetcher.cost_usd == pytest.approx(0.00285), "a failed reconcile must not fail the job"
    assert fetcher.sampling_metadata is not None
    assert fetcher.sampling_metadata["cost_usd"] == pytest.approx(0.00285)
    assert "run-low" in caplog.text


async def test_reconcile_is_skipped_when_the_client_cannot_read_runs() -> None:
    client = ScriptedApify(
        review_runs=[
            make_run(_keyboard_low_rows(PRODUCT_ID), cost_usd=0.002, run_id="run-low"),
            make_run(_keyboard_high_rows(PRODUCT_ID), cost_usd=0.001, run_id="run-high"),
        ]
    )
    fetcher = make_fetcher(client)

    await fetcher.fetch_reviews(product_ref())

    assert fetcher.cost_usd == pytest.approx(0.003)
    assert fetcher.sampling_metadata is not None
    assert fetcher.sampling_metadata["cost_usd"] == pytest.approx(0.003)


# ---------------------------------------------------------------------------
# R2 — incremental merge (pure helpers)
# ---------------------------------------------------------------------------


def breakdown(
    *,
    stars: dict[str, dict[str, Any]],
    since: str = "2025-11-27T05:13:58Z",
    until_by_group: dict[str, str] | None = None,
) -> dict[str, Any]:
    untils = until_by_group or {"low": "2026-11-27T05:14:56Z", "high": "2026-11-27T05:20:00Z"}
    return {
        "source": "apify_review_summary",
        "since": since,
        "until": min(untils.values()),
        "until_by_group": untils,
        "stars": stars,
    }


def test_merge_adds_counts_ands_completeness_and_keeps_the_old_since() -> None:
    old = breakdown(
        stars={
            "1": {"count": 2, "complete": True},
            "5": {"count": 50, "complete": True},
        },
    )
    new = breakdown(
        stars={
            "1": {"count": 1, "complete": True},
            "5": {"count": 2, "complete": True},
        },
        since="2026-11-27T05:14:56Z",
        until_by_group={"low": "2026-12-01T10:00:00Z", "high": "2026-12-01T10:05:00Z"},
    )

    merged = merge_rating_breakdown(old, new)

    assert merged is not None
    assert merged["stars"]["1"]["count"] == 3, "newerThan = old until ⇒ no double count"
    assert merged["stars"]["5"]["count"] == 52
    assert merged["stars"]["5"]["complete"] is True
    assert merged["since"] == "2025-11-27T05:13:58Z", "since is the first crawl, never replaced"
    assert merged["until_by_group"] == {
        "low": "2026-12-01T10:00:00Z",
        "high": "2026-12-01T10:05:00Z",
    }
    assert merged["until"] == "2026-12-01T10:00:00Z"


def test_merge_ands_completeness_so_one_partial_side_wins() -> None:
    old = breakdown(stars={"5": {"count": 10, "complete": True}})
    new = breakdown(stars={"5": {"count": 4, "complete": False}})

    merged = merge_rating_breakdown(old, new)

    assert merged is not None
    assert merged["stars"]["5"] == {"count": 14, "complete": False}


def test_merge_keeps_old_stars_when_their_group_did_not_run_again() -> None:
    old = breakdown(
        stars={str(s): {"count": s, "complete": True} for s in (1, 2, 3)},
        until_by_group={"low": "2026-11-27T05:14:56Z"},
    )
    # Only the high group ran this time: no `low` key in the new until_by_group.
    new = breakdown(
        stars={"4": {"count": 1, "complete": True}, "5": {"count": 2, "complete": True}},
        until_by_group={"high": "2026-12-01T10:05:00Z"},
    )

    merged = merge_rating_breakdown(old, new)

    assert merged is not None
    for star in (1, 2, 3):
        assert merged["stars"][str(star)] == {"count": star, "complete": True}
    assert merged["stars"]["5"] == {"count": 2, "complete": True}
    assert merged["until_by_group"] == {
        "low": "2026-11-27T05:14:56Z",
        "high": "2026-12-01T10:05:00Z",
    }


def test_merge_marks_a_one_sided_star_incomplete_when_its_group_had_run_before() -> None:
    old = breakdown(
        stars={"1": {"count": 3, "complete": True}, "5": {"count": 9, "complete": True}},
    )
    # The high group ran again but returned no 5★ row for this product: it is now a lower bound.
    new = breakdown(
        stars={"1": {"count": 1, "complete": True}},
        until_by_group={"low": "2026-12-01T10:00:00Z", "high": "2026-12-01T10:05:00Z"},
    )

    merged = merge_rating_breakdown(old, new)

    assert merged is not None
    assert merged["stars"]["1"] == {"count": 4, "complete": True}
    assert merged["stars"]["5"] == {"count": 9, "complete": False}


def test_merge_keeps_a_new_only_star_complete_when_its_group_is_new() -> None:
    old = breakdown(
        stars={"1": {"count": 3, "complete": True}},
        until_by_group={"low": "2026-11-27T05:14:56Z"},
    )
    new = breakdown(
        stars={"1": {"count": 1, "complete": True}, "5": {"count": 2, "complete": True}},
        until_by_group={"low": "2026-11-27T05:14:56Z", "high": "2026-12-01T10:05:00Z"},
    )

    merged = merge_rating_breakdown(old, new)

    assert merged is not None
    assert merged["stars"]["5"] == {"count": 2, "complete": True}


@pytest.mark.parametrize(("old", "new"), [(None, "new"), ("old", None)])
def test_merge_returns_the_present_side_when_the_other_is_absent(
    old: dict[str, Any] | None, new: dict[str, Any] | None
) -> None:
    assert merge_rating_breakdown(None, None) is None
    present = breakdown(stars={"5": {"count": 1, "complete": True}})
    assert merge_rating_breakdown(None, present) is present
    assert merge_rating_breakdown(present, None) is present


# ---------------------------------------------------------------------------
# R2 — newerThan extraction from stored breakdowns
# ---------------------------------------------------------------------------


def test_extract_newer_than_takes_the_earliest_timestamp_per_group() -> None:
    breakdowns = {
        "p1": breakdown(
            stars={},
            until_by_group={"low": "2026-11-27T05:14:56Z", "high": "2026-11-27T06:00:00Z"},
        ),
        "p2": breakdown(
            stars={},
            until_by_group={"low": "2026-11-20T01:02:03Z", "high": "2026-11-28T00:00:00Z"},
        ),
    }

    extracted = extract_newer_than_from_breakdowns(breakdowns)

    assert extracted == {
        "low": datetime(2026, 11, 20, 1, 2, 3, tzinfo=UTC),
        "high": datetime(2026, 11, 27, 6, 0, 0, tzinfo=UTC),
    }
    assert all(dt.tzinfo is UTC for dt in extracted.values())


def test_extract_newer_than_skips_missing_and_malformed_values() -> None:
    breakdowns = {
        "p1": {"until_by_group": {"low": "2026-11-27T05:14:56Z", "high": "not-a-date"}},
        "p2": {"until_by_group": {"low": None, "high": "2026-11-27T06:00:00"}},
        "p3": {"until_by_group": "junk"},
        "p4": {},
    }

    extracted = extract_newer_than_from_breakdowns(breakdowns)

    # A naive timestamp is interpreted as UTC; malformed values are dropped entirely.
    assert extracted["low"] == datetime(2026, 11, 27, 5, 14, 56, tzinfo=UTC)
    assert extracted["high"] == datetime(2026, 11, 27, 6, 0, 0, tzinfo=UTC)
    assert set(extracted) == {"low", "high"}


def _subtract_months_local(dt: datetime, months: int) -> datetime:
    total = dt.year * 12 + (dt.month - 1) - months
    year, month0 = divmod(total, 12)
    month = month0 + 1
    return dt.replace(year=year, month=month, day=min(dt.day, calendar.monthrange(year, month)[1]))
