"""Unit tests for the deterministic source-report service helpers (offline).

The SQL aggregation itself is locked by ``test_reports_integration.py`` against a
dedicated Postgres database; here we pin the pure numeric/ranking/warning logic and the
empty-scope fast path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from app.reports import source_report as sr
from app.reports.source_report import (
    RatingBucket,
    ReportTotals,
    SentimentCounts,
    SentimentSourceCounts,
    SourceReport,
    _build_rankings,
    _build_warnings,
    _compute_discount_pct,
    _compute_pct,
    _resolve_cache_ttl_hours,
    _to_number,
    _to_utc,
    build_source_report,
)
from tests.report_fakes import NOW, make_row, make_source

# ---------------------------------------------------------------------------
# Numeric helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("numerator", "denominator", "expected"),
    [
        (5, 10, 50.0),
        (1, 3, 33.3),
        (0, 5, 0.0),
        (10, 10, 100.0),
        (0, 0, None),
        (5, None, None),
        (5, -1, None),
        (2, 3, 66.7),
    ],
)
def test_compute_pct_rounds_one_decimal_and_returns_none_on_zero_division(
    numerator: int, denominator: int | None, expected: float | None
) -> None:
    assert _compute_pct(numerator, denominator) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (Decimal("125000.00"), 125000.0),
        (Decimal("125000.567"), 125000.57),
        (7, 7.0),
        (4.5, 4.5),
    ],
)
def test_to_number_serializes_decimals_as_plain_numbers(value: Any, expected: float | None) -> None:
    assert _to_number(value) == expected


@pytest.mark.parametrize(
    ("price", "list_price", "expected"),
    [
        (Decimal("100"), Decimal("200"), 50.0),
        (Decimal("100"), Decimal("100"), 0.0),
        (Decimal("150"), Decimal("100"), 0.0),
        (Decimal("100"), Decimal("0"), None),
        (None, Decimal("200"), None),
        (Decimal("100"), None, None),
        (Decimal("33333"), Decimal("100000"), 66.7),
        (Decimal("45000"), Decimal("50000"), 10.0),
    ],
)
def test_discount_pct_handles_zero_and_inverted_list_prices(
    price: Decimal | None, list_price: Decimal | None, expected: float | None
) -> None:
    assert _compute_discount_pct(price, list_price) == expected


def test_to_utc_normalizes_naive_and_offset_aware_datetimes() -> None:
    naive = datetime(2026, 11, 26, 12, 0)
    offset = datetime(2026, 11, 26, 19, 0, tzinfo=timezone(timedelta(hours=7)))

    assert _to_utc(naive) == datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    assert _to_utc(naive).tzinfo is UTC
    assert _to_utc(offset) == datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


def test_cache_ttl_uses_settings_when_provided_then_falls_back_to_48h(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Settings:
        CRAWL_CACHE_TTL_HOURS = 6

    assert _resolve_cache_ttl_hours(_Settings()) == 6  # type: ignore[arg-type]

    def _boom() -> Any:
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(sr, "get_settings", _boom)
    assert _resolve_cache_ttl_hours(None) == 48


# ---------------------------------------------------------------------------
# Rankings
# ---------------------------------------------------------------------------


def test_best_selling_sorts_desc_and_puts_missing_sold_count_last() -> None:
    top = make_row(title="Top", sold_count=900)
    mid = make_row(title="Mid", sold_count=100)
    missing = make_row(title="Missing", sold_count=None)

    rankings = _build_rankings([missing, mid, top], 10)

    assert [item.title for item in rankings.best_selling] == ["Top", "Mid"]
    assert [item.value for item in rankings.best_selling] == [900, 100]


def test_best_selling_truncates_to_top_n() -> None:
    rows = [make_row(title=f"SP{i}", sold_count=i) for i in range(1, 6)]

    rankings = _build_rankings(rows, 2)

    assert [item.title for item in rankings.best_selling] == ["SP5", "SP4"]


def test_most_praised_only_keeps_products_with_positive_reviews() -> None:
    praised = make_row(title="Praised", positive=7, rating_avg=4.1)
    tie_low_rating = make_row(title="Tie low", positive=7, rating_avg=3.9)
    zero = make_row(title="Zero", positive=0)

    rankings = _build_rankings([zero, tie_low_rating, praised], 10)

    assert [item.title for item in rankings.most_praised] == ["Praised", "Tie low"]
    assert all(item.value > 0 for item in rankings.most_praised)


def test_highest_rated_requires_three_effective_ratings_and_tie_breaks_on_count() -> None:
    low_count = make_row(title="2 phiếu", rating_avg=5.0, rating_count_effective=2)
    tie_small = make_row(title="3 phiếu", rating_avg=4.9, rating_count_effective=3)
    tie_big = make_row(title="40 phiếu", rating_avg=4.9, rating_count_effective=40)
    unknown_count = make_row(title="Không rõ", rating_avg=5.0, rating_count_effective=None)
    no_rating = make_row(title="Chưa có sao", rating_avg=None, rating_count_effective=9)

    rankings = _build_rankings([low_count, tie_small, unknown_count, no_rating, tie_big], 10)

    assert [item.title for item in rankings.highest_rated] == ["40 phiếu", "3 phiếu"]
    assert all(item.value >= 4.9 for item in rankings.highest_rated)


def test_most_complained_is_empty_without_negative_reviews() -> None:
    rows = [make_row(title="A", negative=0), make_row(title="B", negative=0)]

    rankings = _build_rankings(rows, 10)

    assert rankings.most_complained == []


def test_most_complained_keeps_only_products_with_negative_reviews() -> None:
    rows = [
        make_row(title="Nhiều", negative=4, reviews_collected=9),
        make_row(title="Ít", negative=1, reviews_collected=2),
        make_row(title="Không", negative=0),
    ]

    rankings = _build_rankings(rows, 10)

    assert [item.title for item in rankings.most_complained] == ["Nhiều", "Ít"]


def test_biggest_discount_skips_zero_and_missing_discounts() -> None:
    rows = [
        make_row(title="Không giảm", discount_pct=0.0),
        make_row(title="Giảm ít", discount_pct=10.0),
        make_row(title="Giảm nhiều", discount_pct=40.0, sold_count=5),
        make_row(title="Không rõ", discount_pct=None),
    ]

    rankings = _build_rankings(rows, 10)

    assert [item.title for item in rankings.biggest_discount] == ["Giảm nhiều", "Giảm ít"]
    assert all(item.value > 0 for item in rankings.biggest_discount)


def test_cheapest_sorts_ascending_and_skips_missing_prices() -> None:
    rows = [
        make_row(title="Đắt", price=200000.0),
        make_row(title="Rẻ", price=1000.0),
        make_row(title="Không giá", price=None),
    ]

    rankings = _build_rankings(rows, 10)

    assert [item.title for item in rankings.cheapest] == ["Rẻ", "Đắt"]


def test_rankings_are_empty_for_empty_product_list() -> None:
    rankings = _build_rankings([], 10)

    assert rankings.best_selling == []
    assert rankings.biggest_discount == []
    assert rankings.cheapest == []


# ---------------------------------------------------------------------------
# Warnings — one test per stable code
# ---------------------------------------------------------------------------


def _totals(
    *,
    positive: int = 1,
    neutral: int = 0,
    negative: int = 0,
    unanalyzed: int = 0,
    src_llm: int = 1,
    src_rating: int = 0,
    src_none: int = 0,
) -> ReportTotals:
    return ReportTotals(
        sentiment=SentimentCounts(
            positive=positive, neutral=neutral, negative=negative, unanalyzed=unanalyzed
        ),
        sentiment_source=SentimentSourceCounts(llm=src_llm, rating=src_rating, none=src_none),
    )


def _warnings(
    *,
    source: Any = None,
    totals: ReportTotals | None = None,
    rows: list[Any] | None = None,
    unanalyzed_ids: list[Any] | None = None,
    has_aspects: bool = True,
    ttl_hours: int = 24,
    now: datetime = NOW,
) -> list[Any]:
    return _build_warnings(
        source=source if source is not None else make_source(crawled_at=now),
        totals=totals if totals is not None else _totals(),
        product_rows=(
            rows if rows is not None else [make_row(review_count_reported=1, reviews_collected=1)]
        ),
        unanalyzed_product_ids=unanalyzed_ids or [],
        has_any_aspects=has_aspects,
        ttl_hours=ttl_hours,
        now_utc=now,
    )


def _codes(warnings: list[Any]) -> list[str]:
    return [w.code for w in warnings]


def test_fully_healthy_report_emits_no_warning() -> None:
    assert _codes(_warnings()) == []


def test_partial_reviews_warning_reports_collected_over_reported_and_ids() -> None:
    row = make_row(reviews_collected=2, review_count_reported=10)

    warnings = _warnings(rows=[row])

    assert _codes(warnings) == ["partial_reviews"]
    assert warnings[0].product_ids == [row.product_id]
    assert "2/10" in warnings[0].message


def test_reported_count_lower_warning_when_more_reviews_than_reported() -> None:
    row = make_row(reviews_collected=7, review_count_reported=3)

    warnings = _warnings(rows=[row])

    assert "reported_count_lower" in _codes(warnings)
    entry = next(w for w in warnings if w.code == "reported_count_lower")
    assert entry.product_ids == [row.product_id]
    assert "1 sản phẩm" in entry.message


def test_sentiment_rating_only_warning_when_all_sentiment_came_from_stars() -> None:
    warnings = _warnings(totals=_totals(positive=2, negative=1, src_llm=0, src_rating=3))

    entry = next(w for w in warnings if w.code == "sentiment_rating_only")
    assert "Ingest lại với LLM profile" in entry.message


def test_sentiment_rating_only_is_not_raised_when_llm_sentiment_exists() -> None:
    warnings = _warnings(totals=_totals(positive=2, src_llm=1, src_rating=1))

    assert "sentiment_rating_only" not in _codes(warnings)


def test_no_aspects_warning_only_when_nothing_was_extracted() -> None:
    assert "no_aspects" in _codes(_warnings(has_aspects=False))
    assert "no_aspects" not in _codes(_warnings(has_aspects=True))


def test_unanalyzed_reviews_warning_carries_product_ids() -> None:
    ids = [make_row().product_id]

    warnings = _warnings(totals=_totals(unanalyzed=4), unanalyzed_ids=ids)

    entry = next(w for w in warnings if w.code == "unanalyzed_reviews")
    assert entry.product_ids == ids
    assert "4 review" in entry.message


def test_not_ingested_warning_when_source_stats_lack_ingest_key() -> None:
    warnings = _warnings(source=make_source(stats={}))

    assert "not_ingested" in _codes(warnings)


def test_not_ingested_warning_is_absent_when_ingest_stats_present() -> None:
    warnings = _warnings(source=make_source(stats={"ingest": {"chunks": 10}}))

    assert "not_ingested" not in _codes(warnings)


def test_stale_data_warning_uses_configured_ttl_hours() -> None:
    old = NOW - timedelta(hours=30)

    warnings = _warnings(source=make_source(crawled_at=old), ttl_hours=24)

    entry = next(w for w in warnings if w.code == "stale_data")
    assert "hơn 24 giờ" in entry.message
    assert entry.params == {"ttl_hours": 24}
    assert "stale_data" not in _codes(
        _warnings(source=make_source(crawled_at=NOW - timedelta(hours=1)), ttl_hours=24)
    )


def test_stale_data_warning_follows_the_48h_cache_window() -> None:
    just_inside = make_source(crawled_at=NOW - timedelta(hours=47, minutes=59))
    just_outside = make_source(crawled_at=NOW - timedelta(hours=48, minutes=1))

    assert "stale_data" not in _codes(_warnings(source=just_inside, ttl_hours=48))

    warnings = _warnings(source=just_outside, ttl_hours=48)
    entry = next(w for w in warnings if w.code == "stale_data")
    assert entry.params == {"ttl_hours": 48}
    assert "hơn 48 giờ" in entry.message
    assert "lần mở tiếp theo sẽ tự thu thập mới" in entry.message


def test_missing_price_and_sold_count_warnings_list_offending_products() -> None:
    gap = make_row(title="Thiếu giá", price=None, sold_count=None)

    warnings = _warnings(rows=[gap])

    assert "missing_price" in _codes(warnings)
    assert "missing_sold_count" in _codes(warnings)
    assert next(w for w in warnings if w.code == "missing_price").product_ids == [gap.product_id]


# ---------------------------------------------------------------------------
# Empty-scope fast path of the service
# ---------------------------------------------------------------------------


async def test_empty_scope_report_returns_five_buckets_without_any_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_source(external_ref={}, stats={})
    calls: list[str] = []

    async def _empty_scope(_session: Any, _source: Any) -> tuple[None, list[Any]]:
        calls.append("scope")
        return None, []

    class _NoSqlSession:
        async def execute(self, _stmt: Any) -> Any:  # pragma: no cover - must not be hit
            raise AssertionError("aggregate SQL must not run without products")

        async def stream(self, _stmt: Any) -> Any:  # pragma: no cover
            raise AssertionError("no stream expected")

    monkeypatch.setattr(sr, "resolve_source_scope", _empty_scope)

    report = await build_source_report(_NoSqlSession(), source, top_n=10, now=NOW)  # type: ignore[arg-type]

    assert calls == ["scope"]
    assert isinstance(report, SourceReport)
    assert report.totals.products == 0
    assert report.totals.coverage_pct is None
    assert report.products == []
    assert report.rankings.best_selling == []
    assert report.aspects.top_positive == []
    assert [b.stars for b in report.rating_distribution] == [5, 4, 3, 2, 1]
    assert all(
        isinstance(b, RatingBucket) and b.count == 0 and b.pct is None
        for b in report.rating_distribution
    )
    assert report.generated_at == NOW
    assert {"no_aspects", "not_ingested"}.issubset(set(_codes(report.warnings)))


@pytest.mark.parametrize("top_n", [0, -5, 999])
async def test_out_of_range_top_n_is_clamped_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch, top_n: int
) -> None:
    async def _empty_scope(_session: Any, _source: Any) -> tuple[None, list[Any]]:
        return None, []

    monkeypatch.setattr(sr, "resolve_source_scope", _empty_scope)

    class _NoSqlSession:
        async def execute(self, _stmt: Any) -> Any:  # pragma: no cover
            raise AssertionError("no SQL expected")

    report = await build_source_report(
        _NoSqlSession(),
        make_source(),
        top_n=top_n,
        now=NOW,  # type: ignore[arg-type]
    )

    assert report.products == []
