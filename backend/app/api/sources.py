"""Source listing and summary endpoints (``GET /api/sources``, ``GET /api/sources/{id}``)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session, get_settings
from app.core.config import Settings
from app.db.models import Chunk, Review, Source
from app.ingest.pipeline import resolve_source_scope
from app.jobs.submit import compile_fresh_cache_sql, compute_refresh_after

router = APIRouter(prefix="/api/sources", tags=["sources"])

_UUID_RE_SQL = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


def compute_display_name(
    *,
    url: str,
    url_type: str | None,
    shop_name: str | None = None,
    first_product_title: str | None = None,
) -> str:
    """Resolve human-readable display name for a source (shop name, product title, or URL)."""
    clean_shop = (shop_name or "").strip()
    clean_product = (first_product_title or "").strip()
    if url_type == "shop":
        return clean_shop or url
    if url_type == "product":
        return clean_product or url
    return clean_shop or clean_product or url


def _extract_last_job_id(stats: dict[str, Any] | None) -> str | None:
    if not isinstance(stats, dict):
        return None
    raw = stats.get("last_job_id")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _normalize_optional_filter(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


class ShopSummary(BaseModel):
    """Public summary of the shop associated with a source."""

    id: uuid.UUID
    platform: str
    external_id: str
    name: str
    url: str
    rating: float | None = None
    follower_count: int | None = None
    product_count: int | None = None


class SentimentDistribution(BaseModel):
    """Review sentiment counts across a source's products."""

    positive: int = 0
    neutral: int = 0
    negative: int = 0
    unanalyzed: int = 0


class SourceListItem(BaseModel):
    """Single row returned by ``GET /api/sources``."""

    id: uuid.UUID
    url: str
    url_type: str | None = None
    platform: str | None = None
    status: str
    error: str | None = None
    display_name: str
    shop_name: str | None = None
    products_count: int = 0
    reviews_collected: int = 0
    chat_sessions_count: int = 0
    crawled_at: datetime | None = None
    refresh_after: datetime | None = None
    ingested: bool = False
    last_job_id: str | None = None
    updated_at: datetime


class SourceListResponse(BaseModel):
    """Paginated response payload for ``GET /api/sources``."""

    items: list[SourceListItem] = Field(default_factory=list)
    total: int = 0


class SourceSummaryResponse(BaseModel):
    """Response payload for ``GET /api/sources/{source_id}``."""

    id: uuid.UUID
    url: str
    url_type: str | None = None
    platform: str | None = None
    status: str
    error: str | None = None
    display_name: str
    shop: ShopSummary | None = None
    products_count: int = 0
    product_ids: list[uuid.UUID] = Field(default_factory=list)
    reviews_count: int = 0
    sentiment_distribution: SentimentDistribution = Field(default_factory=SentimentDistribution)
    chunks_count: int = 0
    chunks_by_type: dict[str, int] = Field(default_factory=dict)
    crawled_at: datetime | None = None
    ingested_at: datetime | None = None
    last_job_id: str | None = None
    stats: dict[str, Any] = Field(default_factory=dict)


@router.get("", response_model=SourceListResponse)
async def list_sources(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    status_filter: str | None = Query(default=None, alias="status"),
    url_type: str | None = Query(default=None),
    platform: str | None = Query(default=None),
    fresh: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> SourceListResponse:
    """List sources with pagination, optional filters, and SQL-aggregated counts (no N+1)."""
    clean_status = _normalize_optional_filter(status_filter)
    clean_url_type = _normalize_optional_filter(url_type)
    clean_platform = _normalize_optional_filter(platform)

    where_clauses: list[str] = []
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if clean_status is not None:
        where_clauses.append("s.status = :status")
        params["status"] = clean_status
    if clean_url_type is not None:
        where_clauses.append("s.url_type = :url_type")
        params["url_type"] = clean_url_type
    if clean_platform is not None:
        where_clauses.append("s.platform = :platform")
        params["platform"] = clean_platform
    if fresh:
        fresh_sql, fresh_params = compile_fresh_cache_sql(
            ttl_hours=settings.CRAWL_CACHE_TTL_HOURS,
            alias="s",
        )
        where_clauses.append(f"({fresh_sql})")
        params.update(fresh_params)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    count_stmt = text(f"SELECT COUNT(*)::int AS total FROM sources s {where_sql}")
    total = int((await session.execute(count_stmt, params)).scalar_one() or 0)
    if total == 0 or offset >= total:
        return SourceListResponse(items=[], total=total)

    list_stmt = text(
        f"""
        WITH page_sources AS (
            SELECT
                s.id,
                s.url,
                s.url_type,
                s.platform,
                s.status,
                s.error,
                COALESCE(s.external_ref, '{{}}'::jsonb) AS external_ref,
                COALESCE(s.stats, '{{}}'::jsonb) AS stats,
                s.crawled_at,
                s.updated_at
            FROM sources s
            {where_sql}
            ORDER BY s.updated_at DESC, s.id DESC
            LIMIT :limit OFFSET :offset
        ),
        matched_shops AS (
            SELECT
                ps.id AS source_id,
                ms.id AS shop_id,
                ms.name AS shop_name
            FROM page_sources ps
            LEFT JOIN LATERAL (
                SELECT sh.id, sh.name
                FROM shops sh
                WHERE
                    (
                        (ps.external_ref->>'shop_id') ~ '{_UUID_RE_SQL}'
                        AND sh.id = (ps.external_ref->>'shop_id')::uuid
                    )
                    OR (
                        ps.url_type = 'shop'
                        AND ps.platform IS NOT NULL
                        AND NULLIF(ps.external_ref->>'seller_id', '') IS NOT NULL
                        AND sh.platform = ps.platform
                        AND sh.external_id = (ps.external_ref->>'seller_id')
                    )
                    OR (
                        ps.url_type = 'shop'
                        AND NULLIF(ps.external_ref->>'canonical_url', '') IS NOT NULL
                        AND sh.url = (ps.external_ref->>'canonical_url')
                    )
                    OR (
                        ps.url_type = 'shop'
                        AND NULLIF(ps.external_ref->>'shop_slug', '') IS NOT NULL
                        AND sh.url ILIKE '%/cua-hang/' || (ps.external_ref->>'shop_slug') || '%'
                    )
                ORDER BY
                    CASE
                        WHEN (ps.external_ref->>'shop_id') ~ '{_UUID_RE_SQL}'
                             AND sh.id = (ps.external_ref->>'shop_id')::uuid THEN 0
                        WHEN ps.url_type = 'shop'
                             AND ps.platform IS NOT NULL
                             AND NULLIF(ps.external_ref->>'seller_id', '') IS NOT NULL
                             AND sh.platform = ps.platform
                             AND sh.external_id = (ps.external_ref->>'seller_id') THEN 1
                        WHEN ps.url_type = 'shop'
                             AND NULLIF(ps.external_ref->>'canonical_url', '') IS NOT NULL
                             AND sh.url = (ps.external_ref->>'canonical_url') THEN 2
                        ELSE 3
                    END
                LIMIT 1
            ) ms ON TRUE
        ),
        explicit_pids AS (
            SELECT DISTINCT
                ps.id AS source_id,
                elem::uuid AS product_id
            FROM page_sources ps
            CROSS JOIN LATERAL jsonb_array_elements_text(
                CASE
                    WHEN jsonb_typeof(ps.external_ref->'product_ids') = 'array'
                    THEN ps.external_ref->'product_ids'
                    ELSE '[]'::jsonb
                END
            ) AS elem
            WHERE elem ~ '{_UUID_RE_SQL}'
        ),
        sources_with_explicit AS (
            SELECT DISTINCT source_id FROM explicit_pids
        ),
        scoped_products AS (
            SELECT DISTINCT
                ep.source_id,
                p.id AS product_id,
                p.title,
                p.shop_id
            FROM explicit_pids ep
            JOIN products p ON p.id = ep.product_id
            UNION
            SELECT DISTINCT
                ps.id AS source_id,
                p.id AS product_id,
                p.title,
                p.shop_id
            FROM page_sources ps
            JOIN products p
              ON p.external_id = (ps.external_ref->>'product_id')
             AND (ps.platform IS NULL OR p.platform = ps.platform)
            WHERE ps.url_type = 'product'
              AND NULLIF(ps.external_ref->>'product_id', '') IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM sources_with_explicit swe WHERE swe.source_id = ps.id
              )
            UNION
            SELECT DISTINCT
                ps.id AS source_id,
                p.id AS product_id,
                p.title,
                p.shop_id
            FROM page_sources ps
            JOIN matched_shops ms ON ms.source_id = ps.id
            JOIN products p ON p.shop_id = ms.shop_id
            WHERE ps.url_type = 'shop'
              AND ms.shop_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM sources_with_explicit swe WHERE swe.source_id = ps.id
              )
        ),
        prod_agg AS (
            SELECT
                sp.source_id,
                COUNT(*)::int AS products_count,
                (ARRAY_AGG(sp.title ORDER BY sp.product_id ASC))[1] AS first_product_title,
                (
                    ARRAY_AGG(sp.shop_id ORDER BY sp.product_id ASC)
                    FILTER (WHERE sp.shop_id IS NOT NULL)
                )[1] AS first_product_shop_id
            FROM scoped_products sp
            GROUP BY sp.source_id
        ),
        rev_agg AS (
            SELECT
                sp.source_id,
                COUNT(r.id)::int AS reviews_collected
            FROM scoped_products sp
            JOIN reviews r ON r.product_id = sp.product_id
            GROUP BY sp.source_id
        ),
        chat_agg AS (
            SELECT
                cs.source_id,
                COUNT(*)::int AS chat_sessions_count
            FROM chat_sessions cs
            JOIN page_sources ps ON ps.id = cs.source_id
            GROUP BY cs.source_id
        )
        SELECT
            ps.id,
            ps.url,
            ps.url_type,
            ps.platform,
            ps.status,
            ps.error,
            COALESCE(ms.shop_name, prod_shop.name) AS shop_name,
            pa.first_product_title,
            COALESCE(pa.products_count, 0) AS products_count,
            COALESCE(ra.reviews_collected, 0) AS reviews_collected,
            COALESCE(ca.chat_sessions_count, 0) AS chat_sessions_count,
            ps.crawled_at,
            jsonb_exists(ps.stats, 'ingest') AS ingested,
            NULLIF(ps.stats->>'last_job_id', '') AS last_job_id,
            ps.updated_at
        FROM page_sources ps
        LEFT JOIN matched_shops ms ON ms.source_id = ps.id
        LEFT JOIN prod_agg pa ON pa.source_id = ps.id
        LEFT JOIN shops prod_shop ON prod_shop.id = pa.first_product_shop_id
        LEFT JOIN rev_agg ra ON ra.source_id = ps.id
        LEFT JOIN chat_agg ca ON ca.source_id = ps.id
        ORDER BY ps.updated_at DESC, ps.id DESC
        """
    )

    rows = (await session.execute(list_stmt, params)).mappings().all()
    items = [
        SourceListItem(
            id=row["id"],
            url=row["url"],
            url_type=row["url_type"],
            platform=row["platform"],
            status=row["status"],
            error=row["error"],
            display_name=compute_display_name(
                url=row["url"],
                url_type=row["url_type"],
                shop_name=row["shop_name"],
                first_product_title=row["first_product_title"],
            ),
            shop_name=row["shop_name"],
            products_count=int(row["products_count"] or 0),
            reviews_collected=int(row["reviews_collected"] or 0),
            chat_sessions_count=int(row["chat_sessions_count"] or 0),
            crawled_at=row["crawled_at"],
            refresh_after=compute_refresh_after(row["crawled_at"], settings.CRAWL_CACHE_TTL_HOURS),
            ingested=bool(row["ingested"]),
            last_job_id=row["last_job_id"],
            updated_at=row["updated_at"],
        )
        for row in rows
    ]
    return SourceListResponse(items=items, total=total)


@router.get("/{source_id}", response_model=SourceSummaryResponse)
async def get_source_summary(
    source_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> SourceSummaryResponse:
    """Return a summary of a crawled/ingested source: shop, counts, sentiment, chunks."""
    source = await session.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found.")

    shop, products = await resolve_source_scope(session, source)
    product_ids = [p.id for p in products]

    shop_summary: ShopSummary | None = None
    if shop is not None:
        shop_summary = ShopSummary(
            id=shop.id,
            platform=shop.platform,
            external_id=shop.external_id,
            name=shop.name,
            url=shop.url,
            rating=shop.rating,
            follower_count=shop.follower_count,
            product_count=shop.product_count,
        )

    reviews_count = 0
    sentiment_dist = SentimentDistribution()
    chunks_count = 0
    chunks_by_type: dict[str, int] = {}

    if product_ids:
        rev_row = (
            await session.execute(
                select(
                    func.count().label("total"),
                    func.count().filter(Review.sentiment == "positive").label("pos"),
                    func.count().filter(Review.sentiment == "neutral").label("neu"),
                    func.count().filter(Review.sentiment == "negative").label("neg"),
                    func.count().filter(Review.sentiment.is_(None)).label("unanalyzed"),
                ).where(Review.product_id.in_(product_ids))
            )
        ).one()
        reviews_count = int(rev_row.total or 0)
        sentiment_dist = SentimentDistribution(
            positive=int(rev_row.pos or 0),
            neutral=int(rev_row.neu or 0),
            negative=int(rev_row.neg or 0),
            unanalyzed=int(rev_row.unanalyzed or 0),
        )

        chunk_rows = (
            await session.execute(
                select(Chunk.source_type, func.count().label("cnt"))
                .where(Chunk.product_id.in_(product_ids))
                .group_by(Chunk.source_type)
            )
        ).all()
        for row in chunk_rows:
            cnt = int(row.cnt or 0)
            chunks_by_type[str(row.source_type)] = cnt
            chunks_count += cnt

    ingested_dates = [p.ingested_at for p in products if p.ingested_at is not None]
    ingested_at = max(ingested_dates) if ingested_dates else None
    first_product_title = products[0].title if products else None
    stats = source.stats or {}

    return SourceSummaryResponse(
        id=source.id,
        url=source.url,
        url_type=source.url_type,
        platform=source.platform,
        status=source.status,
        error=source.error,
        display_name=compute_display_name(
            url=source.url,
            url_type=source.url_type,
            shop_name=shop.name if shop is not None else None,
            first_product_title=first_product_title,
        ),
        shop=shop_summary,
        products_count=len(products),
        product_ids=product_ids,
        reviews_count=reviews_count,
        sentiment_distribution=sentiment_dist,
        chunks_count=chunks_count,
        chunks_by_type=chunks_by_type,
        crawled_at=source.crawled_at,
        ingested_at=ingested_at,
        last_job_id=_extract_last_job_id(stats),
        stats=stats,
    )
