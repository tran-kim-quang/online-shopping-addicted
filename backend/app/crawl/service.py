"""End-to-end crawl orchestration service for product and shop URLs."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.crawl.adapters.base import AdapterRegistry
from app.crawl.classifier import resolve_and_classify_url
from app.crawl.errors import CrawlError, UnsupportedPlatformError
from app.crawl.repository import (
    create_source,
    update_source,
    upsert_product,
    upsert_reviews,
    upsert_shop,
)
from app.crawl.types import (
    CrawlResult,
    CrawlStats,
    ParsedURL,
    ProductData,
    ProgressEvent,
    ReviewData,
)
from app.security.url_validator import UnsafeURLError

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
ProgressCallback = Callable[[ProgressEvent], Awaitable[None]]


async def _emit(
    on_progress: ProgressCallback | None,
    stage: str,
    done: int,
    total: int | None = None,
    message: str = "",
) -> None:
    if on_progress is not None:
        await on_progress(ProgressEvent(stage=stage, done=done, total=total, message=message))


def _resolve_defaults(
    max_products: int | None,
    max_reviews: int | None,
    concurrency: int | None,
) -> tuple[int, int, int, bool]:
    try:
        settings = get_settings()
    except Exception:
        settings = None

    eff_products = (
        max_products
        if max_products is not None
        else (settings.CRAWL_MAX_PRODUCTS if settings is not None else 100)
    )
    eff_reviews = (
        max_reviews
        if max_reviews is not None
        else (settings.CRAWL_MAX_REVIEWS_PER_PRODUCT if settings is not None else 200)
    )
    eff_concurrency = (
        concurrency
        if concurrency is not None
        else (settings.CRAWL_CONCURRENCY if settings is not None else 2)
    )
    allow_private = settings.ALLOW_PRIVATE_CRAWL_HOSTS if settings is not None else False
    return eff_products, eff_reviews, max(1, eff_concurrency), allow_private


async def crawl_url(
    url: str,
    *,
    session_factory: SessionFactory | None = None,
    registry: AdapterRegistry | None = None,
    max_products: int | None = None,
    max_reviews: int | None = None,
    on_progress: ProgressCallback | None = None,
    concurrency: int | None = None,
    dry_run: bool = False,
) -> CrawlResult:
    """Crawl a product or shop URL and persist normalized records to Postgres.

    When ``dry_run=True`` no database writes occur and ``result.preview_product``
    contains the normalized first product payload.
    """
    eff_products, eff_reviews, eff_concurrency, allow_private = _resolve_defaults(
        max_products,
        max_reviews,
        concurrency,
    )
    active_registry = registry if registry is not None else AdapterRegistry()
    started = time.monotonic()
    stats = CrawlStats()
    source_id: uuid.UUID | None = None
    shop_id: uuid.UUID | None = None
    product_ids: list[uuid.UUID] = []
    preview_product: ProductData | None = None
    preview_reviews: list[ReviewData] = []

    if not dry_run and session_factory is None:
        from app.db.database import Database

        session_factory = Database().session_factory

    if not dry_run and session_factory is not None:
        async with session_factory() as session:
            source = await create_source(session, url=url, status="running")
            source_id = source.id
            await session.commit()

    async def _fail(
        error_msg: str,
        *,
        platform: str | None = None,
    ) -> CrawlResult:
        stats.duration_seconds = round(time.monotonic() - started, 3)
        if not dry_run and session_factory is not None and source_id is not None:
            async with session_factory() as session:
                kwargs: dict[str, object] = {
                    "status": "failed",
                    "error": error_msg,
                    "stats": stats.model_dump(),
                }
                if platform is not None:
                    kwargs["platform"] = platform
                await update_source(session, source_id, **kwargs)  # type: ignore[arg-type]
                await session.commit()
        await _emit(on_progress, "failed", 0, None, error_msg)
        return CrawlResult(
            source_id=source_id,
            status="failed",
            shop_id=shop_id,
            product_ids=product_ids,
            stats=stats,
            error=error_msg,
        )

    # 1. Classify URL (and safely expand short links if needed).
    await _emit(on_progress, "classify", 0, 1, f"Classifying {url}")
    try:
        parsed = await resolve_and_classify_url(url, allow_private=allow_private)
    except UnsupportedPlatformError as exc:
        return await _fail(str(exc), platform=exc.platform)
    except (CrawlError, UnsafeURLError) as exc:
        return await _fail(str(exc))

    await _emit(
        on_progress,
        "classify",
        1,
        1,
        f"Classified as {parsed.platform}:{parsed.kind}",
    )

    if not dry_run and session_factory is not None and source_id is not None:
        async with session_factory() as session:
            await update_source(
                session,
                source_id,
                url_type=parsed.kind,
                platform=parsed.platform,
                external_ref=parsed.model_dump(exclude_none=True),
            )
            await session.commit()

    try:
        adapter = active_registry.get(parsed.platform)
    except UnsupportedPlatformError as exc:
        return await _fail(str(exc), platform=exc.platform)

    # 2. Product URL flow.
    if parsed.kind == "product":
        stats.products_total = 1
        try:
            product_data = await adapter.fetch_product(parsed)
        except (CrawlError, UnsafeURLError) as exc:
            stats.products_failed = 1
            return await _fail(str(exc))

        await _emit(on_progress, "product", 1, 1, product_data.title)

        reviews_data: list[ReviewData] = []
        try:
            reviews_data = await adapter.fetch_reviews(parsed, limit=eff_reviews)
        except (CrawlError, UnsafeURLError) as exc:
            stats.errors.append(
                {
                    "stage": "reviews",
                    "product_id": product_data.external_id,
                    "error": str(exc),
                }
            )
            logger.warning(
                "Failed to fetch reviews for %s: %s",
                product_data.external_id,
                exc,
            )

        await _emit(
            on_progress,
            "reviews",
            len(reviews_data),
            len(reviews_data),
            f"Fetched {len(reviews_data)} reviews",
        )

        if dry_run:
            preview_product = product_data
            preview_reviews = reviews_data
        elif session_factory is not None:
            now = datetime.now(UTC)
            async with session_factory() as session:
                if product_data.shop is not None:
                    shop_row = await upsert_shop(session, product_data.shop, crawled_at=now)
                    shop_id = shop_row.id
                prod_row = await upsert_product(
                    session,
                    product_data,
                    shop_id=shop_id,
                    crawled_at=now,
                )
                product_ids.append(prod_row.id)
                await upsert_reviews(session, prod_row.id, reviews_data, crawled_at=now)
                await session.commit()

        stats.products_succeeded = 1
        stats.reviews_count = len(reviews_data)
        stats.duration_seconds = round(time.monotonic() - started, 3)
        final_status: Literal["done", "partial"] = "partial" if stats.errors else "done"

        if not dry_run and session_factory is not None and source_id is not None:
            async with session_factory() as session:
                await update_source(
                    session,
                    source_id,
                    status=final_status,
                    stats=stats.model_dump(),
                    crawled_at=datetime.now(UTC),
                )
                await session.commit()

        await _emit(on_progress, "done", 1, 1, f"Completed with status={final_status}")
        return CrawlResult(
            source_id=source_id,
            status=final_status,
            shop_id=shop_id,
            product_ids=product_ids,
            stats=stats,
            preview_product=preview_product,
            preview_reviews=preview_reviews,
        )

    # 3. Shop URL flow.
    try:
        shop_data = await adapter.fetch_shop(parsed)
    except (CrawlError, UnsafeURLError) as exc:
        return await _fail(str(exc))

    shop_ref = parsed.model_copy(update={"seller_id": shop_data.external_id})

    if not dry_run and session_factory is not None:
        async with session_factory() as session:
            shop_row = await upsert_shop(session, shop_data, crawled_at=datetime.now(UTC))
            shop_id = shop_row.id
            await session.commit()

    await _emit(on_progress, "shop", 1, 1, shop_data.name)

    try:
        listings = await adapter.list_shop_products(shop_ref, limit=eff_products)
    except (CrawlError, UnsafeURLError) as exc:
        return await _fail(str(exc))

    stats.products_total = len(listings)
    await _emit(
        on_progress,
        "listing",
        len(listings),
        len(listings),
        f"Found {len(listings)} products",
    )

    if dry_run:
        # In dry-run mode, fetch full details + reviews for the first product only.
        if listings:
            first = listings[0]
            first_ref = ParsedURL(
                platform=parsed.platform,
                kind="product",
                product_id=first.external_id,
                spid=first.spid,
                canonical_url=first.url,
            )
            try:
                preview_product = await adapter.fetch_product(first_ref)
                preview_reviews = await adapter.fetch_reviews(first_ref, limit=eff_reviews)
                stats.products_succeeded = 1
                stats.reviews_count = len(preview_reviews)
            except (CrawlError, UnsafeURLError) as exc:
                stats.products_failed = 1
                stats.errors.append(
                    {"product_id": first.external_id, "url": first.url, "error": str(exc)}
                )
        stats.duration_seconds = round(time.monotonic() - started, 3)
        dry_status: Literal["done", "partial"] = "partial" if stats.errors else "done"
        await _emit(on_progress, "done", stats.products_succeeded, stats.products_total, "Dry run")
        return CrawlResult(
            source_id=None,
            status=dry_status,
            shop_id=None,
            product_ids=[],
            stats=stats,
            preview_product=preview_product,
            preview_reviews=preview_reviews,
        )

    semaphore = asyncio.Semaphore(eff_concurrency)
    progress_lock = asyncio.Lock()
    completed = 0

    async def _process_item(item: ProductData) -> None:
        nonlocal completed
        prod_ref = ParsedURL(
            platform=parsed.platform,
            kind="product",
            product_id=item.external_id,
            spid=item.spid,
            canonical_url=item.url,
        )
        async with semaphore:
            try:
                detail = await adapter.fetch_product(prod_ref)
                reviews = await adapter.fetch_reviews(prod_ref, limit=eff_reviews)
                now = datetime.now(UTC)
                assert session_factory is not None
                async with session_factory() as session:
                    prod_row = await upsert_product(
                        session,
                        detail,
                        shop_id=shop_id,
                        crawled_at=now,
                    )
                    await upsert_reviews(session, prod_row.id, reviews, crawled_at=now)
                    await session.commit()

                async with progress_lock:
                    product_ids.append(prod_row.id)
                    stats.products_succeeded += 1
                    stats.reviews_count += len(reviews)
                    completed += 1
                    done_now = completed
                await _emit(
                    on_progress,
                    "product",
                    done_now,
                    len(listings),
                    f"Saved {detail.title} ({len(reviews)} reviews)",
                )
            except Exception as exc:
                logger.warning(
                    "Failed crawling product %s (%s): %s",
                    item.external_id,
                    item.url,
                    exc,
                )
                async with progress_lock:
                    stats.products_failed += 1
                    stats.errors.append(
                        {
                            "product_id": item.external_id,
                            "url": item.url,
                            "error": str(exc),
                        }
                    )
                    completed += 1
                    done_now = completed
                await _emit(
                    on_progress,
                    "product",
                    done_now,
                    len(listings),
                    f"Failed {item.external_id}: {exc}",
                )

    await asyncio.gather(*(_process_item(item) for item in listings))

    stats.duration_seconds = round(time.monotonic() - started, 3)
    shop_status: Literal["done", "partial"] = "partial" if stats.errors else "done"

    if session_factory is not None and source_id is not None:
        async with session_factory() as session:
            await update_source(
                session,
                source_id,
                status=shop_status,
                stats=stats.model_dump(),
                crawled_at=datetime.now(UTC),
            )
            await session.commit()

    await _emit(
        on_progress,
        "done",
        stats.products_succeeded,
        stats.products_total,
        f"Completed shop crawl with status={shop_status}",
    )
    return CrawlResult(
        source_id=source_id,
        status=shop_status,
        shop_id=shop_id,
        product_ids=product_ids,
        stats=stats,
    )
