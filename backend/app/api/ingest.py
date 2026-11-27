"""Ingest submission endpoint (``POST /api/ingest``) with fast classification and TTL cache."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_job_events,
    get_job_queue,
    get_session,
    get_settings,
    get_url_resolver,
)
from app.core.config import Settings
from app.crawl.classifier import resolve_and_classify_url
from app.crawl.errors import (
    ShortLinkUnresolvableError,
    UnsupportedPlatformError,
    UnsupportedURLError,
)
from app.db.models import DataProviderProfile, LLMProfile
from app.jobs.events import JobEvents
from app.jobs.queue import JobQueue
from app.jobs.submit import classify_ingest_url, submit_parsed_ingest
from app.platforms.registry import PlatformRegistry
from app.security.url_validator import DNSResolver

router = APIRouter(prefix="/api", tags=["ingest"])


class IngestRequest(BaseModel):
    """Request payload for ``POST /api/ingest``."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=2048)
    llm_profile_id: uuid.UUID | None = None
    data_provider_profile_id: uuid.UUID | None = None
    max_products: int | None = Field(default=None, ge=1, le=500)
    max_reviews: int | None = Field(default=None, ge=0, le=1000)


class IngestCachedResponse(BaseModel):
    """200 response when a recent completed crawl exists within ``CRAWL_CACHE_TTL_HOURS``."""

    source_id: uuid.UUID
    cached: bool = True
    crawled_at: datetime
    refresh_after: datetime


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
        parsed = await classify_ingest_url(
            body.url,
            settings=settings,
            resolver=resolver,
            resolve_short_url_fn=resolve_and_classify_url,
        )
    except ShortLinkUnresolvableError as exc:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "error": "short_link_unresolvable",
                "detail": exc.message,
            },
        )
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

    plugin = PlatformRegistry().get(parsed.platform)
    if plugin.requires_provider:
        if body.data_provider_profile_id is None:
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={
                    "error": "data_provider_required",
                    "provider": plugin.requires_provider,
                    "detail": (
                        f"Sàn {plugin.display_name} yêu cầu cấu hình nguồn dữ liệu "
                        f"({plugin.requires_provider})."
                    ),
                },
            )
        provider_profile = await session.get(DataProviderProfile, body.data_provider_profile_id)
        if provider_profile is None:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={
                    "error": "data_provider_mismatch",
                    "detail": "Không tìm thấy cấu hình nguồn dữ liệu.",
                },
            )
        if provider_profile.provider != plugin.requires_provider:
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                content={
                    "error": "data_provider_mismatch",
                    "detail": (
                        "Cấu hình nguồn dữ liệu không đúng loại "
                        f"(cần '{plugin.requires_provider}')."
                    ),
                },
            )

    if body.llm_profile_id is not None:
        profile = await session.get(LLMProfile, body.llm_profile_id)
        if profile is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="LLM profile not found.",
            )

    result = await submit_parsed_ingest(
        parsed,
        session=session,
        settings=settings,
        job_events=job_events,
        job_queue=job_queue,
        llm_profile_id=body.llm_profile_id,
        data_provider_profile_id=body.data_provider_profile_id,
        max_products=body.max_products,
        max_reviews=body.max_reviews,
    )
    if result.cached and result.source_id is not None:
        if result.crawled_at is not None and result.refresh_after is not None:
            cached_payload = IngestCachedResponse(
                source_id=result.source_id,
                cached=True,
                crawled_at=result.crawled_at,
                refresh_after=result.refresh_after,
            )
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content=cached_payload.model_dump(mode="json"),
            )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "source_id": str(result.source_id),
                "cached": True,
                "crawled_at": None,
                "refresh_after": None,
            },
        )

    payload: dict[str, Any] = {
        "job_id": result.job_id,
        "source_id": None,
        "cached": False,
    }
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=payload)
