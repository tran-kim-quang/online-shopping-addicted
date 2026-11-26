"""Ingest submission endpoint (``POST /api/ingest``) with fast classification and TTL cache."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_job_events,
    get_job_queue,
    get_session,
    get_settings,
    get_url_resolver,
)
from app.core.config import Settings
from app.crawl.classifier import classify_url, is_tiki_short_url, resolve_and_classify_url
from app.crawl.errors import UnsupportedPlatformError, UnsupportedURLError
from app.db.models import LLMProfile, Source
from app.jobs.events import JobEvents
from app.jobs.queue import JobQueue
from app.security.url_validator import DNSResolver

router = APIRouter(prefix="/api", tags=["ingest"])


class IngestRequest(BaseModel):
    """Request payload for ``POST /api/ingest``."""

    url: str = Field(min_length=1, max_length=2048)
    llm_profile_id: uuid.UUID | None = None
    max_products: int | None = Field(default=None, ge=1, le=500)
    max_reviews: int | None = Field(default=None, ge=0, le=1000)
    force: bool = False


class IngestCachedResponse(BaseModel):
    """200 response when a recent completed crawl exists within ``CRAWL_CACHE_TTL_HOURS``."""

    source_id: uuid.UUID
    cached: bool = True


class IngestQueuedResponse(BaseModel):
    """202 response when an async Arq ingest job has been enqueued."""

    job_id: str
    source_id: uuid.UUID | None = None
    cached: bool = False


@router.post(
    "/ingest",
    responses={
        200: {"model": IngestCachedResponse},
        202: {"model": IngestQueuedResponse},
    },
)
async def create_ingest(
    body: IngestRequest,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
    job_events: JobEvents = Depends(get_job_events),  # noqa: B008
    job_queue: JobQueue = Depends(get_job_queue),  # noqa: B008
    resolver: DNSResolver | None = Depends(get_url_resolver),  # noqa: B008
) -> JSONResponse:
    """Classify ``url``, return cached source when fresh, otherwise enqueue an Arq ingest job."""
    try:
        if is_tiki_short_url(body.url):
            parsed = await resolve_and_classify_url(
                body.url,
                allow_private=settings.ALLOW_PRIVATE_CRAWL_HOSTS,
                resolver=resolver,
            )
        else:
            parsed = classify_url(body.url)
    except UnsupportedPlatformError as exc:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": "unsupported_platform",
                "platform": exc.platform,
                "detail": exc.message,
            },
        )
    except UnsupportedURLError as exc:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": "unsupported_url",
                "detail": exc.message,
            },
        )

    if body.llm_profile_id is not None:
        profile = await session.get(LLMProfile, body.llm_profile_id)
        if profile is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="LLM profile not found.",
            )

    if not body.force and settings.CRAWL_CACHE_TTL_HOURS > 0:
        cutoff = datetime.now(UTC) - timedelta(hours=settings.CRAWL_CACHE_TTL_HOURS)
        cache_stmt = (
            select(Source)
            .where(
                Source.status == "done",
                Source.crawled_at.is_not(None),
                Source.crawled_at >= cutoff,
                # Crawl-only sources (e.g. Phase 2 CLI runs) have no chunks yet: not a cache hit.
                Source.stats.has_key("ingest"),
                or_(
                    Source.external_ref["canonical_url"].astext == parsed.canonical_url,
                    Source.url == parsed.canonical_url,
                ),
            )
            .order_by(Source.crawled_at.desc())
            .limit(1)
        )
        cached_source = (await session.execute(cache_stmt)).scalar_one_or_none()
        if cached_source is not None:
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={"source_id": str(cached_source.id), "cached": True},
            )

    job_id = str(uuid.uuid4())
    await job_events.create_job(job_id, url=parsed.canonical_url)
    await job_events.emit(
        job_id,
        stage="queued",
        done=0,
        total=None,
        message=f"Queued ingest for {parsed.canonical_url}",
    )
    await job_queue.enqueue_ingest(
        job_id=job_id,
        url=parsed.canonical_url,
        llm_profile_id=str(body.llm_profile_id) if body.llm_profile_id is not None else None,
        max_products=body.max_products,
        max_reviews=body.max_reviews,
        force=body.force,
    )

    payload: dict[str, Any] = {
        "job_id": job_id,
        "source_id": None,
        "cached": False,
    }
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=payload)
