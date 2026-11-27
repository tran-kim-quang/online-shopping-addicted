"""Database persistence helpers (PostgreSQL upserts) for crawled entities."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import case, cast, func, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.crawl.types import ProductData, ReviewData, ShopData
from app.db.models import Product, Review, Shop, Source
from app.ingest.quality import score_review_object

_UNSET: Any = object()


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def create_source(
    session: AsyncSession,
    *,
    url: str,
    url_type: str | None = None,
    platform: str | None = None,
    external_ref: dict[str, Any] | None = None,
    status: str = "running",
) -> Source:
    """Insert a new ``sources`` row and flush to obtain its ID."""
    source = Source(
        id=uuid.uuid4(),
        url=url,
        url_type=url_type,
        platform=platform,
        external_ref=dict(external_ref) if external_ref else {},
        status=status,
        stats={},
    )
    session.add(source)
    await session.flush()
    return source


async def update_source(
    session: AsyncSession,
    source_id: uuid.UUID,
    *,
    url_type: str | None = _UNSET,
    platform: str | None = _UNSET,
    external_ref: dict[str, Any] | None = _UNSET,
    status: str | None = _UNSET,
    error: str | None = _UNSET,
    stats: dict[str, Any] | None = _UNSET,
    crawled_at: datetime | None = _UNSET,
) -> None:
    """Update fields on an existing ``sources`` row."""
    values: dict[str, Any] = {"updated_at": _utcnow()}
    if url_type is not _UNSET:
        values["url_type"] = url_type
    if platform is not _UNSET:
        values["platform"] = platform
    if external_ref is not _UNSET:
        values["external_ref"] = external_ref or {}
    if status is not _UNSET:
        values["status"] = status
    if error is not _UNSET:
        values["error"] = error
    if stats is not _UNSET:
        new_stats = dict(stats or {})
        if "last_job_id" in new_stats:
            values["stats"] = new_stats
        else:
            # Preserve ``last_job_id`` (written by ``on_source_created``) across crawl updates.
            values["stats"] = case(
                (
                    Source.stats.has_key("last_job_id"),
                    func.jsonb_build_object("last_job_id", Source.stats["last_job_id"]).op("||")(
                        cast(new_stats, JSONB)
                    ),
                ),
                else_=cast(new_stats, JSONB),
            )
    if crawled_at is not _UNSET:
        values["crawled_at"] = crawled_at

    await session.execute(update(Source).where(Source.id == source_id).values(**values))


async def upsert_shop(
    session: AsyncSession,
    shop: ShopData,
    *,
    crawled_at: datetime | None = None,
) -> Shop:
    """Upsert a ``shops`` row by ``(platform, external_id)``.

    Nullable enrichment columns (``rating``, ``follower_count``, ``product_count``)
    preserve existing values when the incoming payload omits them (e.g. partial
    seller info extracted from a product detail response).
    """
    now = crawled_at or _utcnow()
    stmt = pg_insert(Shop).values(
        id=uuid.uuid4(),
        platform=shop.platform,
        external_id=shop.external_id,
        name=shop.name,
        url=shop.url,
        rating=shop.rating,
        follower_count=shop.follower_count,
        product_count=shop.product_count,
        raw=shop.raw,
        crawled_at=now,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["platform", "external_id"],
        set_={
            "name": stmt.excluded.name,
            "url": stmt.excluded.url,
            "rating": func.coalesce(stmt.excluded.rating, Shop.rating),
            "follower_count": func.coalesce(stmt.excluded.follower_count, Shop.follower_count),
            "product_count": func.coalesce(stmt.excluded.product_count, Shop.product_count),
            "raw": stmt.excluded.raw,
            "crawled_at": stmt.excluded.crawled_at,
        },
    ).returning(Shop)

    result = await session.execute(stmt)
    return result.scalar_one()


async def upsert_product(
    session: AsyncSession,
    product: ProductData,
    *,
    shop_id: uuid.UUID | None = None,
    crawled_at: datetime | None = None,
) -> Product:
    """Upsert a ``products`` row by ``(platform, external_id)``."""
    now = crawled_at or _utcnow()
    stmt = pg_insert(Product).values(
        id=uuid.uuid4(),
        shop_id=shop_id,
        platform=product.platform,
        external_id=product.external_id,
        title=product.title,
        url=product.url,
        price=product.price,
        list_price=product.list_price,
        currency=product.currency,
        sold_count=product.sold_count,
        sold_text=product.sold_text,
        rating_avg=product.rating_avg,
        rating_count=product.rating_count,
        review_count=product.review_count,
        category=product.category,
        brand=product.brand,
        short_description=product.short_description,
        description_html=product.description_html,
        description_text=product.description_text,
        specs=product.specs,
        images=product.images,
        raw=product.raw,
        crawled_at=now,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["platform", "external_id"],
        set_={
            "shop_id": func.coalesce(stmt.excluded.shop_id, Product.shop_id),
            "title": stmt.excluded.title,
            "url": stmt.excluded.url,
            "price": stmt.excluded.price,
            "list_price": stmt.excluded.list_price,
            "currency": stmt.excluded.currency,
            "sold_count": stmt.excluded.sold_count,
            "sold_text": stmt.excluded.sold_text,
            "rating_avg": stmt.excluded.rating_avg,
            "rating_count": stmt.excluded.rating_count,
            "review_count": stmt.excluded.review_count,
            "category": stmt.excluded.category,
            "brand": stmt.excluded.brand,
            "short_description": stmt.excluded.short_description,
            "description_html": stmt.excluded.description_html,
            "description_text": stmt.excluded.description_text,
            "specs": stmt.excluded.specs,
            "images": stmt.excluded.images,
            "raw": stmt.excluded.raw,
            "crawled_at": stmt.excluded.crawled_at,
        },
    ).returning(Product)

    result = await session.execute(stmt)
    return result.scalar_one()


async def upsert_reviews(
    session: AsyncSession,
    product_id: uuid.UUID,
    reviews: Sequence[ReviewData],
    *,
    crawled_at: datetime | None = None,
) -> int:
    """Bulk upsert reviews for ``product_id`` by ``(product_id, external_id)``."""
    if not reviews:
        return 0

    now = crawled_at or _utcnow()
    # Deduplicate by external_id so a single ON CONFLICT statement never collides on itself.
    deduped: dict[str, ReviewData] = {}
    for review in reviews:
        deduped[review.external_id] = review

    rows = [
        {
            "id": uuid.uuid4(),
            "product_id": product_id,
            "external_id": review.external_id,
            "rating": review.rating,
            "title": review.title,
            "content": review.content,
            "author": review.author,
            "created_at_source": review.created_at_source,
            "helpful_count": review.helpful_count,
            "variant": review.variant,
            "tags": review.tags,
            "sub_ratings": review.sub_ratings,
            "seller_reply": review.seller_reply,
            "media_count": review.media_count,
            "quality_score": (
                review.quality_score
                if review.quality_score is not None
                else score_review_object(review)
            ),
            "images": review.images,
            "raw": review.raw,
            "crawled_at": now,
        }
        for review in deduped.values()
    ]

    stmt = pg_insert(Review).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["product_id", "external_id"],
        set_={
            "rating": stmt.excluded.rating,
            "title": stmt.excluded.title,
            "content": stmt.excluded.content,
            "author": stmt.excluded.author,
            "created_at_source": stmt.excluded.created_at_source,
            "helpful_count": stmt.excluded.helpful_count,
            "variant": stmt.excluded.variant,
            "tags": stmt.excluded.tags,
            "sub_ratings": stmt.excluded.sub_ratings,
            "seller_reply": stmt.excluded.seller_reply,
            "media_count": stmt.excluded.media_count,
            "quality_score": stmt.excluded.quality_score,
            "images": stmt.excluded.images,
            "raw": stmt.excluded.raw,
            "crawled_at": stmt.excluded.crawled_at,
        },
    )
    await session.execute(stmt)
    return len(rows)


async def get_latest_review_timestamps_by_group(
    session: AsyncSession,
    *,
    platform: str,
    product_external_id: str | None = None,
    shop_external_id: str | None = None,
) -> dict[str, datetime]:
    """Return latest ``Review.created_at_source`` per stratified group (low 1-3, high 4-5)."""
    stmt = (
        select(
            func.max(Review.created_at_source)
            .filter(Review.rating.in_([1, 2, 3]))
            .label("low_latest"),
            func.max(Review.created_at_source)
            .filter(Review.rating.in_([4, 5]))
            .label("high_latest"),
        )
        .select_from(Review)
        .join(Product, Review.product_id == Product.id)
        .where(Product.platform == platform, Review.created_at_source.is_not(None))
    )
    if product_external_id is not None:
        stmt = stmt.where(Product.external_id == product_external_id)
    elif shop_external_id is not None:
        stmt = stmt.join(Shop, Product.shop_id == Shop.id).where(
            Shop.platform == platform,
            Shop.external_id == shop_external_id,
        )
    else:
        return {}

    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        return {}
    out: dict[str, datetime] = {}
    if row.low_latest is not None:
        out["low"] = row.low_latest
    if row.high_latest is not None:
        out["high"] = row.high_latest
    return out
