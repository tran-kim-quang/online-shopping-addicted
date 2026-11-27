"""Source-report aggregation of ``Product.rating_breakdown`` (R3) — offline unit tests.

The SQL that feeds ``build_source_report`` is locked by
``test_rating_breakdown_integration.py``; here the pure aggregation helpers and the
``rating_counts_partial`` warning are pinned without a database.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from app.db.models import Product
from app.reports.source_report import (
    RatingCountsSummary,
    ReportTotals,
    _aggregate_source_rating_counts,
    _build_product_rating_counts,
    _build_warnings,
    _compute_pct,
)
from tests.report_fakes import make_product, make_source

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)
SINCE = "2025-11-27T05:13:58Z"


def shopee_product(
    *,
    external_id: str = "48571216193",
    rating_count: int | None = 54,
    stars: dict[str, Any] | None = None,
    since: str | None = SINCE,
    until: str | None = "2026-11-27T05:14:56Z",
) -> Product:
    product = make_product(external_id=external_id, rating_count=rating_count)
    product.platform = "shopee"
    if stars is not None:
        product.rating_breakdown = {  # type: ignore[assignment]
            "source": "apify_review_summary",
            "since": since,
            "until": until,
            "until_by_group": {"low": until, "high": until},
            "stars": stars,
        }
    return product


def complete_stars(counts: dict[int, int]) -> dict[str, dict[str, Any]]:
    return {
        str(star): {"count": counts[star], "complete": True}
        for star in range(1, 6)
        if star in counts
    }


def all_stars(counts: dict[int, int]) -> dict[str, dict[str, Any]]:
    return {str(star): {"count": counts.get(star, 0), "complete": True} for star in range(1, 6)}


# ---------------------------------------------------------------------------
# Per-product summary
# ---------------------------------------------------------------------------


def test_product_rating_counts_sums_five_stars_and_marks_it_complete() -> None:
    product = shopee_product(stars=all_stars({5: 50, 4: 3, 1: 1}))

    summary = _build_product_rating_counts(product)

    assert summary is not None
    assert (summary.positive, summary.neutral, summary.negative) == (53, 0, 1)
    assert summary.total == 54
    assert summary.complete is True
    assert summary.since == SINCE
    assert summary.until == "2026-11-27T05:14:56Z"
    assert summary.products_with_counts == 1
    assert summary.products_total == 1


def test_product_rating_counts_is_partial_when_a_star_is_missing() -> None:
    product = shopee_product(stars=complete_stars({1: 2, 2: 0, 3: 2, 4: 23}))

    summary = _build_product_rating_counts(product)

    assert summary is not None
    assert summary.total == 27
    assert summary.complete is False, "only 1–4★ were reported; 5★ is unknown"
    assert summary.positive == 23, "a missing star contributes 0 but never becomes complete"


def test_product_rating_counts_is_none_without_a_breakdown() -> None:
    assert _build_product_rating_counts(shopee_product(stars=None)) is None
    empty = shopee_product(stars={})
    assert _build_product_rating_counts(empty) is None


# ---------------------------------------------------------------------------
# Source aggregation
# ---------------------------------------------------------------------------


def test_source_aggregation_computes_percentages_only_when_all_five_stars_are_complete() -> None:
    products = [
        shopee_product(external_id="p1", stars=all_stars({5: 50, 4: 30, 3: 20, 2: 10, 1: 10})),
        shopee_product(external_id="p2", stars=all_stars({5: 10, 4: 0, 3: 0, 2: 0, 1: 0})),
    ]

    buckets, summary, incomplete = _aggregate_source_rating_counts(products)

    assert [b.stars for b in buckets] == [5, 4, 3, 2, 1]
    assert [b.count for b in buckets] == [60, 30, 20, 10, 10]
    assert all(b.lower_bound is False for b in buckets)
    assert [b.pct for b in buckets] == [_compute_pct(c, 130) for c in (60, 30, 20, 10, 10)]
    assert summary is not None
    assert summary.total == 130
    assert (summary.positive, summary.neutral, summary.negative) == (90, 20, 20)
    assert summary.complete is True
    assert summary.products_with_counts == 2
    assert summary.products_total == 2
    assert summary.since == SINCE
    assert incomplete == []


def test_source_with_a_product_missing_a_breakdown_is_partial_for_every_star() -> None:
    products = [
        shopee_product(external_id="p1", stars=all_stars({5: 50, 4: 3, 1: 1})),
        shopee_product(external_id="p2", stars=None),
    ]

    buckets, summary, incomplete = _aggregate_source_rating_counts(products)

    assert summary is not None
    assert summary.products_with_counts == 1
    assert summary.products_total == 2
    assert summary.complete is False
    assert all(b.lower_bound is True for b in buckets)
    assert all(b.pct is None for b in buckets), "a partial source never publishes percentages"
    assert [b.count for b in buckets] == [50, 3, 0, 0, 1]
    assert incomplete == [1, 2, 3, 4, 5]


def test_one_incomplete_star_drops_every_percentage_but_only_flags_that_star() -> None:
    products = [
        shopee_product(
            external_id="p1",
            stars={
                "1": {"count": 2, "complete": True},
                "2": {"count": 0, "complete": True},
                "3": {"count": 2, "complete": True},
                "4": {"count": 23, "complete": True},
                "5": {"count": 123, "complete": False},
            },
        )
    ]

    buckets, summary, incomplete = _aggregate_source_rating_counts(products)

    assert summary is not None
    assert summary.complete is False
    by_star = {b.stars: b for b in buckets}
    assert by_star[5].lower_bound is True
    assert by_star[5].count == 123
    assert all(by_star[star].lower_bound is False for star in (1, 2, 3, 4))
    assert all(b.pct is None for b in buckets)
    assert incomplete == [5]


def test_source_without_any_breakdown_keeps_the_old_empty_distribution() -> None:
    products = [shopee_product(external_id="p1", stars=None), shopee_product(external_id="p2")]

    assert _aggregate_source_rating_counts(products) == ([], None, [])


def test_source_aggregation_of_zero_counts_reports_null_percentages() -> None:
    products = [shopee_product(stars=all_stars({}))]

    buckets, summary, incomplete = _aggregate_source_rating_counts(products)

    assert summary is not None
    assert summary.total == 0
    assert summary.complete is True
    assert all(b.count == 0 and b.pct is None for b in buckets)
    assert incomplete == []


# ---------------------------------------------------------------------------
# rating_counts_partial warning
# ---------------------------------------------------------------------------


STRATIFIED_STATS = {
    "sampling": {
        "provider": "apify",
        "strategy": "stratified",
        "window_months": 12,
        "groups": [
            {"name": "low", "coverage": "complete_to_date"},
            {"name": "high", "coverage": "complete_to_date"},
        ],
    }
}


def build_warnings(rating_counts: RatingCountsSummary | None, incomplete: list[int]) -> Any:
    return _build_warnings(
        source=make_source(platform="shopee", stats=STRATIFIED_STATS),
        totals=ReportTotals(),
        product_rows=[],
        unanalyzed_product_ids=[],
        has_any_aspects=True,
        ttl_hours=48,
        now_utc=NOW,
        rating_counts=rating_counts,
        incomplete_stars=incomplete,
    )


def test_partial_counts_emit_a_rating_counts_partial_warning_with_the_missing_stars() -> None:
    warnings = build_warnings(
        RatingCountsSummary(total=150, complete=False, products_with_counts=1, products_total=1),
        [4, 5],
    )

    warning = next(w for w in warnings if w.code == "rating_counts_partial")
    assert warning.params == {"stars": "4★, 5★"}
    assert "4★, 5★" in warning.message
    assert warning.product_ids is None


def test_complete_counts_do_not_emit_the_partial_warning() -> None:
    warnings = build_warnings(
        RatingCountsSummary(total=54, complete=True, products_with_counts=1, products_total=1),
        [],
    )

    assert "rating_counts_partial" not in {w.code for w in warnings}
    assert "stratified_sample" in {w.code for w in warnings}


def test_no_breakdown_means_no_partial_warning_even_for_a_stratified_source() -> None:
    warnings = build_warnings(None, [])

    assert "rating_counts_partial" not in {w.code for w in warnings}


@pytest.mark.parametrize("code", ["partial_reviews", "reported_count_lower"])
def test_stratified_sources_never_emit_the_natural_sample_warnings(code: str) -> None:
    warnings = build_warnings(None, [])

    assert code not in {w.code for w in warnings}


def test_warning_is_scoped_by_product_ids_only_when_relevant() -> None:
    product_id = uuid.uuid4()
    warnings = _build_warnings(
        source=make_source(platform="shopee", stats=STRATIFIED_STATS),
        totals=ReportTotals(),
        product_rows=[],
        unanalyzed_product_ids=[product_id],
        has_any_aspects=True,
        ttl_hours=48,
        now_utc=NOW,
        rating_counts=RatingCountsSummary(total=10, complete=False),
        incomplete_stars=[1],
    )

    partial = next(w for w in warnings if w.code == "rating_counts_partial")
    assert partial.product_ids is None
    assert partial.params == {"stars": "1★"}
