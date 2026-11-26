"""``ReportWarning.params`` — machine-readable values the web UI translates per locale.

``docs/design-system/shop-qa/UI-SPEC.md`` §6 requires the frontend to translate warnings
from ``code`` + ``params`` (never by rendering the Vietnamese ``message`` when the UI is in
English). These tests lock the params payload for every warning code that has one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.db.models import Source
from app.reports.source_report import (
    ProductReportRow,
    ReportTotals,
    ReportWarning,
    SentimentCounts,
    SentimentSourceCounts,
    _build_warnings,
)
from tests.report_fakes import make_row, make_source

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


def _totals(
    *,
    positive: int = 1,
    neutral: int = 0,
    negative: int = 0,
    unanalyzed: int = 0,
    src_llm: int = 1,
    src_rating: int = 0,
) -> ReportTotals:
    return ReportTotals(
        sentiment=SentimentCounts(
            positive=positive, neutral=neutral, negative=negative, unanalyzed=unanalyzed
        ),
        sentiment_source=SentimentSourceCounts(llm=src_llm, rating=src_rating, none=0),
    )


def _warnings(
    *,
    source: Source | None = None,
    totals: ReportTotals | None = None,
    rows: list[ProductReportRow] | None = None,
    unanalyzed_ids: list[uuid.UUID] | None = None,
    has_aspects: bool = True,
    ttl_hours: int = 24,
) -> list[ReportWarning]:
    return _build_warnings(
        source=source if source is not None else make_source(crawled_at=NOW),
        totals=totals if totals is not None else _totals(),
        product_rows=(
            rows if rows is not None else [make_row(reviews_collected=1, review_count_reported=1)]
        ),
        unanalyzed_product_ids=unanalyzed_ids or [],
        has_any_aspects=has_aspects,
        ttl_hours=ttl_hours,
        now_utc=NOW,
    )


def _by_code(warnings: list[ReportWarning], code: str) -> ReportWarning:
    return next(w for w in warnings if w.code == code)


def test_partial_reviews_params_carry_collected_reported_and_product_count() -> None:
    rows = [
        make_row(reviews_collected=2, review_count_reported=10),
        make_row(reviews_collected=3, review_count_reported=7),
        make_row(reviews_collected=5, review_count_reported=5),  # complete, not partial
    ]

    warning = _by_code(_warnings(rows=rows), "partial_reviews")

    assert warning.params == {"collected": 5, "reported": 17, "products": 2}
    assert all(isinstance(value, int) for value in warning.params.values())
    # The Vietnamese message stays for API clients that do not translate.
    assert "5/17" in warning.message


def test_reported_count_lower_params_carry_product_count() -> None:
    rows = [make_row(reviews_collected=9, review_count_reported=3)]

    warning = _by_code(_warnings(rows=rows), "reported_count_lower")

    assert warning.params == {"products": 1}


def test_unanalyzed_reviews_params_carry_count_and_ids() -> None:
    ids = [uuid.uuid4(), uuid.uuid4()]

    warning = _by_code(
        _warnings(totals=_totals(unanalyzed=4), unanalyzed_ids=ids), "unanalyzed_reviews"
    )

    assert warning.params == {"count": 4}
    assert warning.product_ids == ids


def test_stale_data_params_carry_ttl_hours() -> None:
    stale_source = make_source(crawled_at=NOW - timedelta(hours=48))

    warning = _by_code(_warnings(source=stale_source, ttl_hours=24), "stale_data")

    assert warning.params == {"ttl_hours": 24}


def test_missing_price_and_sold_count_params_carry_product_counts() -> None:
    rows = [
        make_row(price=None, sold_count=None),
        make_row(price=1000.0, sold_count=None),
        make_row(price=2000.0, sold_count=10),
    ]

    warnings = _warnings(rows=rows)

    assert _by_code(warnings, "missing_price").params == {"products": 1}
    assert _by_code(warnings, "missing_sold_count").params == {"products": 2}


def test_warnings_without_numeric_detail_expose_empty_params() -> None:
    warnings = _warnings(
        source=make_source(stats={}, crawled_at=NOW),
        totals=_totals(positive=2, negative=1, src_llm=0, src_rating=3),
        has_aspects=False,
    )

    for code in ("sentiment_rating_only", "no_aspects", "not_ingested"):
        assert _by_code(warnings, code).params == {}


def test_params_are_json_serialisable_ints_and_strings() -> None:
    rows = [make_row(reviews_collected=1, review_count_reported=4)]
    warning = _by_code(_warnings(rows=rows, ttl_hours=12), "partial_reviews")

    for key, value in warning.params.items():
        assert isinstance(key, str)
        assert isinstance(value, (int, float, str))
        assert not isinstance(value, bool)


def test_every_warning_code_stays_documented_by_the_frontend_mapping() -> None:
    """Guard: codes emitted here must exist in ``components/report/warnings-banner.tsx``."""
    from pathlib import Path

    banner = (
        Path(__file__).resolve().parents[2]
        / "frontend"
        / "components"
        / "report"
        / "warnings-banner.tsx"
    )
    if not banner.exists():  # pragma: no cover - frontend checkout missing
        return

    known_codes = {
        line.strip().strip('",')
        for line in banner.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith('"') and line.strip().endswith('",')
    }
    emitted = {
        "partial_reviews",
        "reported_count_lower",
        "sentiment_rating_only",
        "no_aspects",
        "unanalyzed_reviews",
        "not_ingested",
        "stale_data",
        "missing_price",
        "missing_sold_count",
    }
    assert emitted <= known_codes
