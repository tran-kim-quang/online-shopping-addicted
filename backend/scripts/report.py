"""CLI tool to print a source overview report and optionally export products/reviews CSVs.

Usage:
    uv run python scripts/report.py <source_id> [--csv-dir DIR] [--top-n 10] [--include-author]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

# Ensure ``backend/`` is importable when invoked as ``python scripts/report.py``.
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.core.config import get_settings  # noqa: E402
from app.db.database import Database  # noqa: E402
from app.db.models import Source  # noqa: E402
from app.ingest.pipeline import resolve_source_scope  # noqa: E402
from app.reports.csv_export import (  # noqa: E402
    build_csv_filename,
    iter_products_csv,
    iter_reviews_csv,
)
from app.reports.source_report import SourceReport, build_source_report  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Print a deterministic overview report for a source and optionally export CSV.",
    )
    parser.add_argument("source_id", help="UUID of the source to inspect")
    parser.add_argument(
        "--csv-dir",
        type=Path,
        default=None,
        help="Directory to write products and reviews CSV exports",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=10,
        help="Number of entries per ranking / aspect table (1-50, default: 10)",
    )
    parser.add_argument(
        "--include-author",
        action="store_true",
        help="Include reviewer author column in reviews CSV export",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print raw JSON report instead of formatted tables",
    )
    return parser.parse_args(argv)


def _fmt_num(value: int | float | None) -> str:
    if value is None:
        return "-"
    if isinstance(value, float) and value.is_integer():
        return f"{int(value):,}"
    if isinstance(value, float):
        return f"{value:,.2f}"
    return f"{value:,}"


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.1f}%"


def _truncate(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    return text[: max(1, width - 1)].rstrip() + "…"


def format_report_text(report: SourceReport, *, top_n: int = 10) -> str:
    """Render a human-readable terminal overview of ``report``."""
    lines: list[str] = []
    src = report.source
    totals = report.totals

    lines.append("=" * 78)
    lines.append(f"SOURCE REPORT: {src.id}")
    lines.append(
        f"URL: {src.url}  |  Platform: {src.platform or '-'}  |  "
        f"Type: {src.url_type or '-'}  |  Status: {src.status}"
    )
    lines.append(
        f"Crawled: {src.crawled_at.isoformat() if src.crawled_at else '-'}  |  "
        f"Ingested: {src.ingested_at.isoformat() if src.ingested_at else '-'}"
    )
    if report.shop is not None:
        shop = report.shop
        lines.append(
            f"Shop: {shop.name}  |  Rating: {shop.rating if shop.rating is not None else '-'}  |  "
            f"Followers: {_fmt_num(shop.follower_count)}  |  "
            f"Reported Products: {_fmt_num(shop.product_count)}"
        )
    lines.append("=" * 78)

    lines.append("\n[TONG QUAN / TOTALS]")
    lines.append(
        f"  Products: {totals.products} (with price: {totals.products_with_price})  |  "
        f"Sold total: {_fmt_num(totals.sold_total)}"
    )
    lines.append(
        f"  Price min / median / max: {_fmt_num(totals.price_min)} / "
        f"{_fmt_num(totals.price_median)} / {_fmt_num(totals.price_max)}"
    )
    lines.append(
        f"  Weighted rating: "
        f"{totals.rating_avg_weighted if totals.rating_avg_weighted is not None else '-'}"
    )
    lines.append(
        f"  Reviews: collected={_fmt_num(totals.reviews_collected)} / "
        f"reported={_fmt_num(totals.review_count_reported)} "
        f"(coverage={_fmt_pct(totals.coverage_pct)})"
    )
    lines.append(
        f"  Sentiment: +{totals.sentiment.positive} / ={totals.sentiment.neutral} / "
        f"-{totals.sentiment.negative} / unanalyzed={totals.sentiment.unanalyzed}"
    )
    lines.append(
        f"  Sentiment source: llm={totals.sentiment_source.llm}, "
        f"rating={totals.sentiment_source.rating}, none={totals.sentiment_source.none}"
    )
    chunks_detail = ", ".join(f"{k}={v}" for k, v in sorted(totals.chunks.by_type.items())) or "-"
    lines.append(f"  Chunks: total={totals.chunks.total} ({chunks_detail})")

    lines.append("\n[PHAN BO SAO / RATING DISTRIBUTION]")
    for bucket in report.rating_distribution:
        bar_len = int(round((bucket.pct or 0.0) / 5.0))
        bar = "#" * bar_len
        lines.append(f"  {bucket.stars}★  {bucket.count:>6}  ({_fmt_pct(bucket.pct):>6})  {bar}")

    lines.append(f"\n[TOP {min(top_n, len(report.products))} SAN PHAM (theo sold_count)]")
    if not report.products:
        lines.append("  (Khong co san pham)")
    else:
        lines.append(
            f"  {'#':>2}  {'Title':<40} {'Price':>10} {'Sold':>7} "
            f"{'Rating':>6} {'Reviews':>11} {'Pos%':>6}"
        )
        for idx, prod in enumerate(report.products[:top_n], start=1):
            rev_str = f"{prod.reviews_collected}/{prod.review_count_reported or 0}"
            rating_str = f"{prod.rating_avg:.1f}" if prod.rating_avg is not None else "-"
            lines.append(
                f"  {idx:>2}  {_truncate(prod.title, 40):<40} "
                f"{_fmt_num(prod.price):>10} {_fmt_num(prod.sold_count):>7} "
                f"{rating_str:>6} {rev_str:>11} {_fmt_pct(prod.positive_pct):>6}"
            )

    if report.aspects.top_positive or report.aspects.top_negative:
        lines.append("\n[KHIA CANH / ASPECTS]")
        pos_str = ", ".join(f"{a.aspect} ({a.count})" for a in report.aspects.top_positive) or "-"
        neg_str = ", ".join(f"{a.aspect} ({a.count})" for a in report.aspects.top_negative) or "-"
        lines.append(f"  Top positive: {pos_str}")
        lines.append(f"  Top negative: {neg_str}")

    lines.append("\n[CANH BAO / WARNINGS]")
    if not report.warnings:
        lines.append("  (Khong co canh bao)")
    else:
        for warning in report.warnings:
            extra = f" [{len(warning.product_ids)} SP]" if warning.product_ids else ""
            lines.append(f"  - [{warning.code}] {warning.message}{extra}")

    return "\n".join(lines)


async def _run(args: argparse.Namespace) -> int:
    try:
        source_uuid = uuid.UUID(args.source_id)
    except ValueError:
        print(f"Invalid source_id UUID: {args.source_id}", file=sys.stderr)
        return 2

    if not (1 <= args.top_n <= 50):
        print("--top-n must be between 1 and 50.", file=sys.stderr)
        return 2

    settings = get_settings()
    db = Database()
    db.init(settings.DATABASE_URL)

    try:
        async with db.session() as session:
            source = await session.get(Source, source_uuid)
            if source is None:
                print(f"Source {source_uuid} not found.", file=sys.stderr)
                return 1

            report = await build_source_report(
                session,
                source,
                top_n=args.top_n,
                settings=settings,
            )

            if args.json:
                print(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2))
            else:
                print(format_report_text(report, top_n=args.top_n))

            if args.csv_dir is not None:
                out_dir: Path = args.csv_dir
                out_dir.mkdir(parents=True, exist_ok=True)
                shop, products = await resolve_source_scope(session, source)
                product_ids = [p.id for p in products]

                products_path = out_dir / build_csv_filename(source, shop, products, "products")
                reviews_path = out_dir / build_csv_filename(source, shop, products, "reviews")

                with products_path.open("wb") as fh:
                    async for chunk in iter_products_csv(
                        session,
                        product_ids,
                        default_shop_name=shop.name if shop is not None else None,
                    ):
                        fh.write(chunk)

                with reviews_path.open("wb") as fh:
                    async for chunk in iter_reviews_csv(
                        session,
                        product_ids,
                        include_author=args.include_author,
                    ):
                        fh.write(chunk)

                print(f"\nExported products CSV: {products_path}")
                print(f"Exported reviews CSV:  {reviews_path}")
    finally:
        await db.dispose()

    return 0


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
