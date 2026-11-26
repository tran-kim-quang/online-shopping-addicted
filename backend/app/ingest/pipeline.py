"""Post-crawl ingestion pipeline: sentiment -> chunk -> embed -> aggregate."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.crawl.types import ProgressEvent
from app.db.models import LLMProfile, Product, Review, Shop, Source
from app.ingest.chunking import build_product_chunks, sync_product_chunks
from app.ingest.embedding import Embedder, NullEmbedder, get_embedder
from app.ingest.sentiment import analyze_product_reviews
from app.llm.client import LLMClient
from app.llm.registry import LLMClientRegistry
from app.llm.types import LLMConfig
from app.security.secrets import SecretBox

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
ProgressCallback = Callable[[ProgressEvent], Awaitable[None]]


class IngestTimings(BaseModel):
    """Per-stage elapsed seconds during ingestion."""

    sentiment_seconds: float = 0.0
    chunk_seconds: float = 0.0
    embed_seconds: float = 0.0
    total_seconds: float = 0.0


class IngestStats(BaseModel):
    """Aggregated ingestion counters and timings persisted into ``sources.stats['ingest']``."""

    products_total: int = 0
    products_ingested: int = 0
    products_failed: int = 0
    reviews_analyzed: int = 0
    reviews_llm: int = 0
    reviews_rating_fallback: int = 0
    reviews_skipped: int = 0
    chunks_total: int = 0
    chunks_created: int = 0
    chunks_kept: int = 0
    chunks_deleted: int = 0
    chunks_embedded: int = 0
    embedding_model: str | None = None
    embedding_dim: int | None = None
    timings: IngestTimings = Field(default_factory=IngestTimings)
    errors: list[dict[str, Any]] = Field(default_factory=list)


class IngestResult(BaseModel):
    """Outcome of :func:`ingest_source`."""

    source_id: uuid.UUID
    status: Literal["done", "partial", "failed"]
    product_ids: list[uuid.UUID] = Field(default_factory=list)
    stats: IngestStats = Field(default_factory=IngestStats)
    error: str | None = None


async def _emit(
    on_progress: ProgressCallback | None,
    stage: str,
    done: int,
    total: int | None = None,
    message: str = "",
) -> None:
    if on_progress is not None:
        await on_progress(ProgressEvent(stage=stage, done=done, total=total, message=message))


def _parse_uuid(raw: Any) -> uuid.UUID | None:
    if isinstance(raw, uuid.UUID):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return uuid.UUID(raw.strip())
        except ValueError:
            return None
    return None


async def resolve_source_scope(
    session: AsyncSession,
    source: Source,
) -> tuple[Shop | None, list[Product]]:
    """Resolve the ``Shop`` (if any) and ``Product`` rows associated with ``source``."""
    ext = source.external_ref or {}
    shop: Shop | None = None
    products: list[Product] = []

    shop_uuid = _parse_uuid(ext.get("shop_id"))
    if shop_uuid is not None:
        shop = await session.get(Shop, shop_uuid)

    if shop is None and source.url_type == "shop":
        seller_id = ext.get("seller_id")
        if seller_id and source.platform:
            res = await session.execute(
                select(Shop).where(
                    Shop.platform == source.platform,
                    Shop.external_id == str(seller_id),
                )
            )
            shop = res.scalar_one_or_none()
        if shop is None and ext.get("canonical_url"):
            res = await session.execute(select(Shop).where(Shop.url == str(ext["canonical_url"])))
            shop = res.scalar_one_or_none()
        if shop is None and ext.get("shop_slug"):
            slug = str(ext["shop_slug"])
            res = await session.execute(select(Shop).where(Shop.url.ilike(f"%/cua-hang/{slug}%")))
            shop = res.scalars().first()

    raw_pids = ext.get("product_ids")
    parsed_pids: list[uuid.UUID] = []
    if isinstance(raw_pids, list):
        for item in raw_pids:
            pid = _parse_uuid(item)
            if pid is not None:
                parsed_pids.append(pid)

    if parsed_pids:
        res = await session.execute(
            select(Product).where(Product.id.in_(parsed_pids)).order_by(Product.id.asc())
        )
        products = list(res.scalars().all())
    elif source.url_type == "product" and ext.get("product_id"):
        stmt = select(Product).where(Product.external_id == str(ext["product_id"]))
        if source.platform:
            stmt = stmt.where(Product.platform == source.platform)
        res = await session.execute(stmt)
        products = list(res.scalars().all())
    elif source.url_type == "shop" and shop is not None:
        res = await session.execute(
            select(Product).where(Product.shop_id == shop.id).order_by(Product.id.asc())
        )
        products = list(res.scalars().all())

    if shop is None and products and products[0].shop_id is not None:
        shop = await session.get(Shop, products[0].shop_id)

    return shop, products


async def _resolve_llm_client(
    profile: LLMProfile | None,
    settings: Settings,
) -> LLMClient | None:
    if profile is None:
        return None
    raw_key: str | None = None
    if profile.api_key_enc:
        raw_key = SecretBox().decrypt(profile.api_key_enc)
    config = LLMConfig(
        base_url=profile.base_url,
        api_key=SecretStr(raw_key) if raw_key else None,
        chat_model=profile.chat_model,
        small_model=profile.small_model,
        embedding_model=profile.embedding_model,
        timeout=settings.LLM_TIMEOUT_SECONDS,
        max_retries=settings.LLM_MAX_RETRIES,
    )
    return await LLMClientRegistry().get(config)


async def ingest_source(
    source_id: uuid.UUID,
    *,
    llm_profile_id: uuid.UUID | None = None,
    on_progress: ProgressCallback | None = None,
    session_factory: SessionFactory | None = None,
    embedder: Embedder | None = None,
    llm_client: LLMClient | None = None,
    settings: Settings | None = None,
) -> IngestResult:
    """Run sentiment analysis, chunking, embedding, and aggregation for a crawled source.

    Per-product failures are isolated so remaining products still ingest and the
    source ends in ``partial`` rather than discarding completed work.
    """
    started = time.monotonic()
    active_settings = settings if settings is not None else get_settings()

    if session_factory is None:
        from app.db.database import Database

        session_factory = Database().session_factory

    async with session_factory() as session:
        source = await session.get(Source, source_id)
        if source is None:
            raise ValueError(f"Source {source_id} not found.")

        profile: LLMProfile | None = None
        if llm_profile_id is not None:
            profile = await session.get(LLMProfile, llm_profile_id)
            if profile is None:
                logger.warning(
                    "LLMProfile %s not found for source %s; falling back to rating sentiment.",
                    llm_profile_id,
                    source_id,
                )

        shop, products = await resolve_source_scope(session, source)
        initial_source_status = source.status
        existing_source_stats = dict(source.stats or {})
        product_ids = [p.id for p in products]
        shop_id = shop.id if shop is not None else None

    active_embedder = (
        embedder if embedder is not None else get_embedder(active_settings, profile=profile)
    )
    active_llm_client = llm_client
    if active_llm_client is None and profile is not None:
        try:
            active_llm_client = await _resolve_llm_client(profile, active_settings)
        except Exception as exc:
            logger.warning(
                "Could not initialize LLMClient for profile %s; falling back to rating "
                "sentiment: %s",
                profile.id,
                exc,
            )

    stats = IngestStats(
        products_total=len(product_ids),
        embedding_model=None if isinstance(active_embedder, NullEmbedder) else active_embedder.name,
        embedding_dim=active_embedder.dim,
    )

    total_products = len(product_ids)
    sentiment_total_sec = 0.0
    chunk_total_sec = 0.0
    embed_total_sec = 0.0

    await _emit(
        on_progress,
        "ingest_start",
        0,
        total_products,
        f"Starting ingestion for {total_products} product(s)",
    )

    ingested_ids: list[uuid.UUID] = []

    for idx, prod_id in enumerate(product_ids, start=1):
        try:
            async with session_factory() as session:
                product = await session.get(Product, prod_id)
                if product is None:
                    raise ValueError(f"Product {prod_id} disappeared before ingestion.")

                prod_shop: Shop | None = None
                effective_shop_id = product.shop_id or shop_id
                if effective_shop_id is not None:
                    prod_shop = await session.get(Shop, effective_shop_id)

                # 1. Sentiment analysis + aggregate product counts.
                t_sent = time.monotonic()
                sent_summary = await analyze_product_reviews(
                    session,
                    product.id,
                    llm_client=active_llm_client,
                    profile=profile,
                    batch_size=active_settings.SENTIMENT_BATCH_SIZE,
                )
                sentiment_total_sec += time.monotonic() - t_sent
                stats.reviews_analyzed += sent_summary.analyzed
                stats.reviews_llm += sent_summary.llm_count
                stats.reviews_rating_fallback += sent_summary.rating_fallback_count
                stats.reviews_skipped += sent_summary.skipped

                await _emit(
                    on_progress,
                    "sentiment",
                    idx,
                    total_products,
                    f"Sentiment done for '{product.title}' "
                    f"(+{sent_summary.positive}/={sent_summary.neutral}/-{sent_summary.negative})",
                )

                # 2. Build chunks from product + reviews.
                t_chunk = time.monotonic()
                rev_res = await session.execute(
                    select(Review)
                    .where(Review.product_id == product.id)
                    .order_by(Review.created_at_source.desc().nullslast(), Review.id.asc())
                )
                reviews = list(rev_res.scalars().all())
                drafts = build_product_chunks(
                    product,
                    reviews,
                    shop=prod_shop,
                    max_chars=active_settings.CHUNK_MAX_CHARS,
                    overlap_chars=active_settings.CHUNK_OVERLAP_CHARS,
                )
                chunk_total_sec += time.monotonic() - t_chunk

                # 3. Sync chunks and embed only new/changed ones.
                sync_stats = await sync_product_chunks(
                    session,
                    product,
                    drafts,
                    embedder=active_embedder,
                )
                embed_total_sec += sync_stats.embed_seconds
                stats.chunks_total += sync_stats.total
                stats.chunks_created += sync_stats.created
                stats.chunks_kept += sync_stats.kept
                stats.chunks_deleted += sync_stats.deleted
                stats.chunks_embedded += sync_stats.embedded

                if stats.embedding_dim is None and active_embedder.dim is not None:
                    stats.embedding_dim = active_embedder.dim

                product.ingested_at = datetime.now(UTC)
                await session.commit()

                ingested_ids.append(product.id)
                stats.products_ingested += 1

                await _emit(
                    on_progress,
                    "embed",
                    idx,
                    total_products,
                    f"Chunks synced for '{product.title}' "
                    f"(total={sync_stats.total}, embedded={sync_stats.embedded})",
                )
        except Exception as exc:
            logger.exception(
                "Failed ingesting product %s for source %s: %s",
                prod_id,
                source_id,
                exc,
            )
            stats.products_failed += 1
            stats.errors.append({"product_id": str(prod_id), "error": str(exc)})
            await _emit(
                on_progress,
                "ingest_error",
                idx,
                total_products,
                f"Failed ingesting product {prod_id}: {exc}",
            )

    stats.timings = IngestTimings(
        sentiment_seconds=round(sentiment_total_sec, 3),
        chunk_seconds=round(chunk_total_sec, 3),
        embed_seconds=round(embed_total_sec, 3),
        total_seconds=round(time.monotonic() - started, 3),
    )

    final_status: Literal["done", "partial", "failed"]
    error_summary: str | None = None
    if total_products > 0 and stats.products_ingested == 0 and stats.products_failed > 0:
        final_status = "failed"
        error_summary = f"All {stats.products_failed} product(s) failed during ingestion."
    elif stats.products_failed > 0 or initial_source_status == "partial":
        final_status = "partial"
        if stats.products_failed > 0:
            error_summary = f"{stats.products_failed}/{total_products} product(s) failed ingestion."
    else:
        final_status = "done"

    merged_stats = dict(existing_source_stats)
    merged_stats["ingest"] = stats.model_dump()

    async with session_factory() as session:
        source_row = await session.get(Source, source_id)
        if source_row is not None:
            source_row.status = final_status
            source_row.stats = merged_stats
            if error_summary is not None:
                source_row.error = error_summary
            source_row.updated_at = datetime.now(UTC)
            await session.commit()

    return IngestResult(
        source_id=source_id,
        status=final_status,
        product_ids=ingested_ids,
        stats=stats,
        error=error_summary,
    )
