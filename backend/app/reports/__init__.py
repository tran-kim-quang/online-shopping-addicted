"""Deterministic SQL-backed data overview reports and streaming CSV exports for sources."""

from __future__ import annotations

from app.reports.csv_export import (
    DEFAULT_CSV_BATCH_SIZE,
    PRODUCTS_CSV_COLUMNS,
    REVIEWS_CSV_BASE_COLUMNS,
    build_csv_filename,
    format_aspects_cell,
    iter_products_csv,
    iter_reviews_csv,
    sanitize_csv_cell,
    slugify_ascii,
)
from app.reports.source_report import (
    AspectCount,
    ChunkCounts,
    ProductReportRow,
    RankingItem,
    RatingBucket,
    ReportAspects,
    ReportRankings,
    ReportShopInfo,
    ReportSourceInfo,
    ReportTotals,
    ReportWarning,
    SentimentCounts,
    SentimentSourceCounts,
    SourceReport,
    build_source_report,
)

__all__ = [
    "AspectCount",
    "ChunkCounts",
    "DEFAULT_CSV_BATCH_SIZE",
    "PRODUCTS_CSV_COLUMNS",
    "ProductReportRow",
    "REVIEWS_CSV_BASE_COLUMNS",
    "RankingItem",
    "RatingBucket",
    "ReportAspects",
    "ReportRankings",
    "ReportShopInfo",
    "ReportSourceInfo",
    "ReportTotals",
    "ReportWarning",
    "SentimentCounts",
    "SentimentSourceCounts",
    "SourceReport",
    "build_csv_filename",
    "build_source_report",
    "format_aspects_cell",
    "iter_products_csv",
    "iter_reviews_csv",
    "sanitize_csv_cell",
    "slugify_ascii",
]
