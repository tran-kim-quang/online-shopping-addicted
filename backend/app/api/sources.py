"""Source inspection summary endpoint (``GET /api/sources/{source_id}``)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session
from app.db.models import Chunk, Review, Source
from app.ingest.pipeline import resolve_source_scope

router = APIRouter(prefix="/api/sources", tags=["sources"])


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


class SourceSummaryResponse(BaseModel):
    """Response payload for ``GET /api/sources/{source_id}``."""

    id: uuid.UUID
    url: str
    url_type: str | None = None
    platform: str | None = None
    status: str
    error: str | None = None
    shop: ShopSummary | None = None
    products_count: int = 0
    product_ids: list[uuid.UUID] = Field(default_factory=list)
    reviews_count: int = 0
    sentiment_distribution: SentimentDistribution = Field(default_factory=SentimentDistribution)
    chunks_count: int = 0
    chunks_by_type: dict[str, int] = Field(default_factory=dict)
    crawled_at: datetime | None = None
    ingested_at: datetime | None = None
    stats: dict[str, Any] = Field(default_factory=dict)


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

    return SourceSummaryResponse(
        id=source.id,
        url=source.url,
        url_type=source.url_type,
        platform=source.platform,
        status=source.status,
        error=source.error,
        shop=shop_summary,
        products_count=len(products),
        product_ids=product_ids,
        reviews_count=reviews_count,
        sentiment_distribution=sentiment_dist,
        chunks_count=chunks_count,
        chunks_by_type=chunks_by_type,
        crawled_at=source.crawled_at,
        ingested_at=ingested_at,
        stats=source.stats or {},
    )
