"""Hybrid search endpoint (``POST /api/search``) for debugging and Phase 4 QA tools."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_embedder_override, get_session, get_settings
from app.core.config import Settings
from app.db.models import LLMProfile, Source
from app.ingest.embedding import Embedder, get_embedder
from app.ingest.pipeline import resolve_source_scope
from app.ingest.search import SearchHit, hybrid_search

router = APIRouter(prefix="/api", tags=["search"])


class SearchRequest(BaseModel):
    """Request payload for ``POST /api/search``."""

    query: str
    source_id: uuid.UUID | None = None
    product_id: uuid.UUID | None = None
    product_ids: list[uuid.UUID] | None = None
    shop_id: uuid.UUID | None = None
    source_types: list[Literal["description", "specs", "review", "meta"]] | None = None
    k: int = Field(default=8, ge=1, le=50)
    min_similarity: float | None = Field(default=None, ge=0.0, le=1.0)
    llm_profile_id: uuid.UUID | None = None


class SearchResponse(BaseModel):
    """Response payload for ``POST /api/search``."""

    query: str
    total: int
    hits: list[SearchHit]


@router.post("/search", response_model=SearchResponse)
async def search_chunks(
    body: SearchRequest,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
    embedder_override: Embedder | None = Depends(get_embedder_override),  # noqa: B008
) -> SearchResponse:
    """Execute hybrid full-text + vector RRF search scoped by source, product, or shop."""
    explicit_pids: list[uuid.UUID] = []
    if body.product_ids:
        explicit_pids.extend(body.product_ids)
    if body.product_id is not None and body.product_id not in explicit_pids:
        explicit_pids.append(body.product_id)

    effective_product_ids: list[uuid.UUID] | None = None
    if body.source_id is not None:
        source = await session.get(Source, body.source_id)
        if source is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Source not found.",
            )
        _shop, source_products = await resolve_source_scope(session, source)
        source_pids = [p.id for p in source_products]
        if explicit_pids:
            explicit_set = set(explicit_pids)
            effective_product_ids = [pid for pid in source_pids if pid in explicit_set]
        else:
            effective_product_ids = source_pids
    elif explicit_pids:
        effective_product_ids = explicit_pids

    if embedder_override is not None:
        active_embedder: Embedder | None = embedder_override
    else:
        profile: LLMProfile | None = None
        if body.llm_profile_id is not None:
            profile = await session.get(LLMProfile, body.llm_profile_id)
            if profile is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="LLM profile not found.",
                )
        active_embedder = get_embedder(settings, profile=profile)

    hits = await hybrid_search(
        session,
        body.query,
        product_ids=effective_product_ids,
        shop_id=body.shop_id,
        source_types=body.source_types,
        min_similarity=body.min_similarity,
        k=body.k,
        embedder=active_embedder,
    )
    return SearchResponse(query=body.query, total=len(hits), hits=hits)
