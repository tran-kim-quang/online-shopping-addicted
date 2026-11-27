"""End-to-end crawl orchestration service for product and shop URLs."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import JobCancelledError
from app.crawl.classifier import resolve_and_classify_url
from app.crawl.errors import CrawlError, ProviderError, UnsupportedPlatformError
from app.crawl.repository import (
    create_source,
    extract_newer_than_from_breakdowns,
    get_latest_review_timestamps_by_group,
    get_product_rating_breakdowns,
    merge_rating_breakdown,
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
from app.db.models import DataProviderProfile, Product, Review
from app.platforms.registry import PlatformRegistry
from app.security.secrets import SecretBox, SecretBoxError
from app.security.url_validator import UnsafeURLError

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
ProgressCallback = Callable[[ProgressEvent], Awaitable[None]]
SourceCreatedCallback = Callable[[uuid.UUID], Awaitable[None]]
HeartbeatCallback = Callable[[], Awaitable[None]]


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


async def _count_reviews_by_group(
    session: AsyncSession,
    *,
    product_ids: list[uuid.UUID] | None = None,
    shop_id: uuid.UUID | None = None,
) -> dict[str, int]:
    """Count total persisted reviews per stratified group (low 1-3★, high 4-5★)."""
    stmt = select(
        func.count(Review.id).filter(Review.rating.in_([1, 2, 3])).label("low_count"),
        func.count(Review.id).filter(Review.rating.in_([4, 5])).label("high_count"),
    ).select_from(Review)
    if shop_id is not None:
        stmt = stmt.join(Product, Review.product_id == Product.id).where(Product.shop_id == shop_id)
    elif product_ids:
        stmt = stmt.where(Review.product_id.in_(product_ids))
    else:
        return {"low": 0, "high": 0}
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        return {"low": 0, "high": 0}
    return {
        "low": int(row.low_count or 0),
        "high": int(row.high_count or 0),
    }


def _build_sampling_payload(
    fetcher: Any,
    *,
    db_group_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    raw_sampling = getattr(fetcher, "sampling_metadata", None)
    if isinstance(raw_sampling, dict):
        payload = dict(raw_sampling)
        if hasattr(fetcher, "cost_usd"):
            payload["cost_usd"] = round(float(fetcher.cost_usd), 6)
        if getattr(fetcher, "cost_estimated", False):
            payload["cost_estimated"] = True
        raw_groups = payload.get("groups")
        if isinstance(raw_groups, list):
            updated_groups: list[dict[str, Any]] = []
            for group in raw_groups:
                if not isinstance(group, dict):
                    continue
                group_copy = dict(group)
                if "delivered_new" not in group_copy and "delivered" in group_copy:
                    group_copy["delivered_new"] = group_copy["delivered"]
                group_name = group_copy.get("name")
                if (
                    db_group_counts is not None
                    and isinstance(group_name, str)
                    and group_name in db_group_counts
                ):
                    group_copy["delivered"] = db_group_counts[group_name]
                updated_groups.append(group_copy)
            payload["groups"] = updated_groups
        return payload
    return {"provider": None, "strategy": "natural"}


async def _fetch_reviews_compat(
    fetcher: Any,
    ref: ParsedURL,
    *,
    limit: int,
    newer_than_by_group: dict[str, datetime] | None,
) -> list[ReviewData]:
    if newer_than_by_group:
        try:
            sig = inspect.signature(fetcher.fetch_reviews)
            if "newer_than_by_group" in sig.parameters:
                return await fetcher.fetch_reviews(
                    ref,
                    limit=limit,
                    newer_than_by_group=newer_than_by_group,
                )
        except (TypeError, ValueError):
            pass
    return await fetcher.fetch_reviews(ref, limit=limit)


async def crawl_url(
    url: str,
    *,
    session_factory: SessionFactory | None = None,
    registry: Any | None = None,
    max_products: int | None = None,
    max_reviews: int | None = None,
    on_progress: ProgressCallback | None = None,
    on_source_created: SourceCreatedCallback | None = None,
    concurrency: int | None = None,
    dry_run: bool = False,
    data_provider_profile_id: uuid.UUID | None = None,
    heartbeat: HeartbeatCallback | None = None,
    secret_box: SecretBox | None = None,
    apify_client: Any | None = None,
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
    started = time.monotonic()
    stats = CrawlStats()
    source_id: uuid.UUID | None = None
    shop_id: uuid.UUID | None = None
    product_ids: list[uuid.UUID] = []
    preview_product: ProductData | None = None
    preview_reviews: list[ReviewData] = []
    fetcher: Any | None = None
    owns_fetcher = False

    if not dry_run and session_factory is None:
        from app.db.database import Database

        session_factory = Database().session_factory

    if not dry_run and session_factory is not None:
        async with session_factory() as session:
            source = await create_source(session, url=url, status="running")
            source_id = source.id
            await session.commit()
        if on_source_created is not None and source_id is not None:
            await on_source_created(source_id)

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

    try:
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
            try:
                async with session_factory() as session:
                    await update_source(
                        session,
                        source_id,
                        url_type=parsed.kind,
                        platform=parsed.platform,
                        external_ref=parsed.model_dump(exclude_none=True),
                    )
                    await session.commit()
            except JobCancelledError:
                raise
            except Exception as exc:
                logger.exception("Failed updating classified source %s: %s", source_id, exc)
                return await _fail(str(exc), platform=parsed.platform)

        # Resolve fetcher via injected test registry or PlatformRegistry plugin.
        try:
            if registry is not None:
                fetcher = registry.get(parsed.platform)
                owns_fetcher = False
            else:
                plugin = PlatformRegistry().get(parsed.platform)
                provider_profile: DataProviderProfile | None = None
                if plugin.requires_provider:
                    if apify_client is None:
                        if data_provider_profile_id is None:
                            raise ProviderError(
                                "data_provider_required",
                                f"Sàn {plugin.display_name} yêu cầu cấu hình nguồn dữ liệu "
                                f"({plugin.requires_provider}).",
                                status_code=409,
                            )
                        if session_factory is None:
                            raise ProviderError(
                                "data_provider_required",
                                "Không thể tải cấu hình nguồn dữ liệu khi thiếu kết nối DB.",
                                status_code=500,
                            )
                        async with session_factory() as session:
                            provider_profile = await session.get(
                                DataProviderProfile,
                                data_provider_profile_id,
                            )
                        if (
                            provider_profile is None
                            or provider_profile.provider != plugin.requires_provider
                        ):
                            raise ProviderError(
                                "data_provider_mismatch",
                                "Cấu hình nguồn dữ liệu không tồn tại hoặc không đúng loại "
                                f"'{plugin.requires_provider}'.",
                                status_code=422,
                            )

                fetcher = plugin.create_fetcher(
                    provider_profile,
                    heartbeat=heartbeat,
                    on_progress=on_progress,
                    secret_box=secret_box,
                    apify_client=apify_client,
                )
                owns_fetcher = True
        except UnsupportedPlatformError as exc:
            return await _fail(str(exc), platform=exc.platform)
        except ProviderError as exc:
            return await _fail(f"{exc.code}: {exc.message}", platform=parsed.platform)
        except SecretBoxError as exc:
            return await _fail(f"secret_key_mismatch: {exc}", platform=parsed.platform)

        # 2. Product URL flow.
        if parsed.kind == "product":
            stats.products_total = 1
            try:
                product_data = await fetcher.fetch_product(parsed)
            except JobCancelledError:
                raise
            except ProviderError as exc:
                stats.products_failed = 1
                return await _fail(f"{exc.code}: {exc.message}", platform=parsed.platform)
            except Exception as exc:
                stats.products_failed = 1
                return await _fail(str(exc))

            await _emit(on_progress, "product", 1, 1, product_data.title)

            newer_than_by_group: dict[str, datetime] | None = None
            existing_breakdowns: dict[str, dict[str, Any]] = {}
            if (
                not dry_run
                and session_factory is not None
                and getattr(fetcher, "supports_shop_review_feed", False)
            ):
                try:
                    async with session_factory() as session:
                        existing_breakdowns = await get_product_rating_breakdowns(
                            session,
                            platform=parsed.platform,
                            product_external_id=product_data.external_id,
                        )
                        newer_than_by_group = extract_newer_than_from_breakdowns(
                            existing_breakdowns
                        )
                        fallback_newer = await get_latest_review_timestamps_by_group(
                            session,
                            platform=parsed.platform,
                            product_external_id=product_data.external_id,
                        )
                        for grp, ts in fallback_newer.items():
                            newer_than_by_group.setdefault(grp, ts)
                except Exception:
                    newer_than_by_group = None

            reviews_data: list[ReviewData] = []
            try:
                reviews_data = await _fetch_reviews_compat(
                    fetcher,
                    parsed,
                    limit=eff_reviews,
                    newer_than_by_group=newer_than_by_group,
                )
            except JobCancelledError:
                raise
            except ProviderError as exc:
                stats.products_failed = 1
                return await _fail(f"{exc.code}: {exc.message}", platform=parsed.platform)
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

            new_breakdowns = getattr(fetcher, "rating_breakdowns_by_product", None)
            if isinstance(new_breakdowns, dict) and product_data.external_id in new_breakdowns:
                merged_bd = merge_rating_breakdown(
                    existing_breakdowns.get(product_data.external_id),
                    new_breakdowns[product_data.external_id],
                )
                if merged_bd is not None:
                    product_data = product_data.model_copy(update={"rating_breakdown": merged_bd})

            await _emit(
                on_progress,
                "reviews",
                len(reviews_data),
                len(reviews_data),
                f"Fetched {len(reviews_data)} reviews",
            )

            db_group_counts: dict[str, int] | None = None
            if dry_run:
                preview_product = product_data
                preview_reviews = reviews_data
            elif session_factory is not None:
                try:
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
                        if isinstance(getattr(fetcher, "sampling_metadata", None), dict):
                            try:
                                db_group_counts = await _count_reviews_by_group(
                                    session,
                                    product_ids=[prod_row.id],
                                )
                            except Exception:
                                db_group_counts = None
                        await session.commit()
                except JobCancelledError:
                    raise
                except Exception as exc:
                    logger.exception(
                        "Failed persisting crawled product %s for source %s: %s",
                        product_data.external_id,
                        source_id,
                        exc,
                    )
                    stats.products_failed = 1
                    return await _fail(str(exc))

            stats.products_succeeded = 1
            stats.reviews_count = len(reviews_data)
            stats.duration_seconds = round(time.monotonic() - started, 3)
            final_status: Literal["done", "partial"] = "partial" if stats.errors else "done"

            if not dry_run and session_factory is not None and source_id is not None:
                final_ref = parsed.model_dump(exclude_none=True)
                final_ref["product_ids"] = [str(pid) for pid in product_ids]
                if shop_id is not None:
                    final_ref["shop_id"] = str(shop_id)
                stats_payload = stats.model_dump()
                stats_payload["sampling"] = _build_sampling_payload(
                    fetcher,
                    db_group_counts=db_group_counts,
                )
                try:
                    async with session_factory() as session:
                        await update_source(
                            session,
                            source_id,
                            external_ref=final_ref,
                            status=final_status,
                            stats=stats_payload,
                            crawled_at=datetime.now(UTC),
                        )
                        await session.commit()
                except JobCancelledError:
                    raise
                except Exception as exc:
                    logger.exception("Failed finalizing product source %s: %s", source_id, exc)
                    return await _fail(str(exc))

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
            shop_data = await fetcher.fetch_shop(parsed)
        except JobCancelledError:
            raise
        except ProviderError as exc:
            return await _fail(f"{exc.code}: {exc.message}", platform=parsed.platform)
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
            listings = await fetcher.list_shop_products(shop_ref, limit=eff_products)
        except JobCancelledError:
            raise
        except ProviderError as exc:
            return await _fail(f"{exc.code}: {exc.message}", platform=parsed.platform)
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

        # Provider-backed bulk shop flow (no per-product detail calls; 2 shop review runs).
        if getattr(fetcher, "supports_shop_detail_skip", False) and getattr(
            fetcher, "supports_shop_review_feed", False
        ):
            shop_newer_than: dict[str, datetime] | None = None
            existing_shop_breakdowns: dict[str, dict[str, Any]] = {}
            if not dry_run and session_factory is not None:
                try:
                    async with session_factory() as session:
                        existing_shop_breakdowns = await get_product_rating_breakdowns(
                            session,
                            platform=parsed.platform,
                            shop_external_id=shop_data.external_id,
                        )
                        shop_newer_than = extract_newer_than_from_breakdowns(
                            existing_shop_breakdowns
                        )
                        fallback_shop_newer = await get_latest_review_timestamps_by_group(
                            session,
                            platform=parsed.platform,
                            shop_external_id=shop_data.external_id,
                        )
                        for grp, ts in fallback_shop_newer.items():
                            shop_newer_than.setdefault(grp, ts)
                except Exception:
                    shop_newer_than = None

            try:
                reviews_by_prod, minimal_products = await fetcher.fetch_shop_reviews(
                    shop_ref,
                    newer_than_by_group=shop_newer_than,
                )
            except JobCancelledError:
                raise
            except ProviderError as exc:
                return await _fail(f"{exc.code}: {exc.message}", platform=parsed.platform)
            except (CrawlError, UnsafeURLError) as exc:
                return await _fail(str(exc))

            products_to_save: list[ProductData] = list(listings)
            seen_ext_ids = {p.external_id for p in products_to_save}
            for ext_id, minimal_prod in minimal_products.items():
                if ext_id not in seen_ext_ids:
                    products_to_save.append(minimal_prod)
                    seen_ext_ids.add(ext_id)
            new_shop_breakdowns = getattr(fetcher, "rating_breakdowns_by_product", None)
            if isinstance(new_shop_breakdowns, dict) and new_shop_breakdowns:
                products_to_save = [
                    item.model_copy(
                        update={
                            "rating_breakdown": merge_rating_breakdown(
                                existing_shop_breakdowns.get(item.external_id),
                                new_shop_breakdowns.get(item.external_id),
                            )
                        }
                    )
                    if item.external_id in new_shop_breakdowns
                    else item
                    for item in products_to_save
                ]
            stats.products_total = len(products_to_save)

            if dry_run:
                if products_to_save:
                    preview_product = products_to_save[0]
                    preview_reviews = reviews_by_prod.get(preview_product.external_id, [])
                    stats.products_succeeded = len(products_to_save)
                    stats.reviews_count = sum(len(v) for v in reviews_by_prod.values())
                stats.duration_seconds = round(time.monotonic() - started, 3)
                await _emit(
                    on_progress,
                    "done",
                    stats.products_succeeded,
                    stats.products_total,
                    "Dry run",
                )
                return CrawlResult(
                    source_id=None,
                    status="done",
                    shop_id=None,
                    product_ids=[],
                    stats=stats,
                    preview_product=preview_product,
                    preview_reviews=preview_reviews,
                )

            shop_db_group_counts: dict[str, int] | None = None
            if session_factory is not None:
                now = datetime.now(UTC)
                async with session_factory() as session:
                    for item in products_to_save:
                        prod_row = await upsert_product(
                            session,
                            item,
                            shop_id=shop_id,
                            crawled_at=now,
                        )
                        prod_reviews = reviews_by_prod.get(item.external_id, [])
                        await upsert_reviews(session, prod_row.id, prod_reviews, crawled_at=now)
                        product_ids.append(prod_row.id)
                        stats.products_succeeded += 1
                        stats.reviews_count += len(prod_reviews)
                    if isinstance(getattr(fetcher, "sampling_metadata", None), dict):
                        try:
                            shop_db_group_counts = await _count_reviews_by_group(
                                session,
                                product_ids=product_ids,
                                shop_id=shop_id,
                            )
                        except Exception:
                            shop_db_group_counts = None
                    await session.commit()

            await _emit(
                on_progress,
                "reviews",
                stats.reviews_count,
                stats.reviews_count,
                f"Saved {stats.products_succeeded} products and {stats.reviews_count} reviews",
            )
            stats.duration_seconds = round(time.monotonic() - started, 3)
            shop_status_bulk: Literal["done", "partial"] = "partial" if stats.errors else "done"

            if session_factory is not None and source_id is not None:
                final_shop_ref = shop_ref.model_dump(exclude_none=True)
                final_shop_ref["product_ids"] = [str(pid) for pid in product_ids]
                if shop_id is not None:
                    final_shop_ref["shop_id"] = str(shop_id)
                stats_payload = stats.model_dump()
                stats_payload["sampling"] = _build_sampling_payload(
                    fetcher,
                    db_group_counts=shop_db_group_counts,
                )
                async with session_factory() as session:
                    await update_source(
                        session,
                        source_id,
                        external_ref=final_shop_ref,
                        status=shop_status_bulk,
                        stats=stats_payload,
                        crawled_at=datetime.now(UTC),
                    )
                    await session.commit()

            await _emit(
                on_progress,
                "done",
                stats.products_succeeded,
                stats.products_total,
                f"Completed shop crawl with status={shop_status_bulk}",
            )
            return CrawlResult(
                source_id=source_id,
                status=shop_status_bulk,
                shop_id=shop_id,
                product_ids=product_ids,
                stats=stats,
            )

        # Standard per-product shop flow (e.g. Tiki).
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
                    preview_product = await fetcher.fetch_product(first_ref)
                    preview_reviews = await fetcher.fetch_reviews(first_ref, limit=eff_reviews)
                    stats.products_succeeded = 1
                    stats.reviews_count = len(preview_reviews)
                except (CrawlError, UnsafeURLError) as exc:
                    stats.products_failed = 1
                    stats.errors.append(
                        {"product_id": first.external_id, "url": first.url, "error": str(exc)}
                    )
            stats.duration_seconds = round(time.monotonic() - started, 3)
            dry_status: Literal["done", "partial"] = "partial" if stats.errors else "done"
            await _emit(
                on_progress,
                "done",
                stats.products_succeeded,
                stats.products_total,
                "Dry run",
            )
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
                    detail = await fetcher.fetch_product(prod_ref)
                    reviews = await fetcher.fetch_reviews(prod_ref, limit=eff_reviews)
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
                except JobCancelledError:
                    raise
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
            final_shop_ref = shop_ref.model_dump(exclude_none=True)
            final_shop_ref["product_ids"] = [str(pid) for pid in product_ids]
            if shop_id is not None:
                final_shop_ref["shop_id"] = str(shop_id)
            stats_payload = stats.model_dump()
            stats_payload["sampling"] = _build_sampling_payload(fetcher)
            async with session_factory() as session:
                await update_source(
                    session,
                    source_id,
                    external_ref=final_shop_ref,
                    status=shop_status,
                    stats=stats_payload,
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
    finally:
        if owns_fetcher and fetcher is not None:
            await fetcher.aclose()
