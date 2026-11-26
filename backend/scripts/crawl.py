"""CLI tool to crawl a product or shop URL and print progress + JSON summary.

Usage:
    uv run python scripts/crawl.py <url> [--max-products N] [--max-reviews N] [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Ensure ``backend/`` is importable when invoked as ``python scripts/crawl.py``.
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.core.config import get_settings  # noqa: E402
from app.crawl.adapters.base import AdapterRegistry  # noqa: E402
from app.crawl.service import crawl_url  # noqa: E402
from app.crawl.types import ProgressEvent  # noqa: E402
from app.db.database import Database  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Crawl an e-commerce product or shop URL into Postgres.",
    )
    parser.add_argument("url", help="Target product or shop URL (e.g. Tiki.vn)")
    parser.add_argument(
        "--max-products",
        type=int,
        default=None,
        help="Maximum products to crawl for a shop URL",
    )
    parser.add_argument(
        "--max-reviews",
        type=int,
        default=None,
        help="Maximum reviews to crawl per product",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not write to the database; print the normalized first product",
    )
    return parser.parse_args(argv)


async def _print_progress(event: ProgressEvent) -> None:
    total = f"/{event.total}" if event.total is not None else ""
    print(f"[{event.stage}] {event.done}{total} {event.message}", file=sys.stderr, flush=True)


async def _run(args: argparse.Namespace) -> int:
    registry = AdapterRegistry()
    db = Database()
    db_initialized = False

    if not args.dry_run:
        settings = get_settings()
        db.init(settings.DATABASE_URL)
        db_initialized = True

    try:
        result = await crawl_url(
            args.url,
            session_factory=None if args.dry_run else db.session_factory,
            registry=registry,
            max_products=args.max_products,
            max_reviews=args.max_reviews,
            on_progress=_print_progress,
            dry_run=args.dry_run,
        )
    finally:
        await registry.aclose_all()
        if db_initialized:
            await db.dispose()

    if args.dry_run:
        payload: dict[str, object] = {
            "summary": result.model_dump(mode="json"),
            "first_product": (
                result.preview_product.model_dump(mode="json", exclude={"raw"})
                if result.preview_product is not None
                else None
            ),
            "sample_reviews": [
                r.model_dump(mode="json", exclude={"raw"}) for r in result.preview_reviews[:3]
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))

    return 0 if result.status in ("done", "partial") else 1


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
