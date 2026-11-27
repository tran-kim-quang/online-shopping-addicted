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
    get_user_id,
)
from app.api.profile_resolver import resolve_data_provider_profile, resolve_llm_profile
from app.api.user_history import upsert_user_source
from app.core.config import Settings
from app.core.errors import SystemQuotaExceededError
from app.core.system_usage import SystemUsage
from app.crawl.classifier import resolve_and_classify_url
from app.crawl.errors import (
    ShortLinkUnresolvableError,
    UnsupportedPlatformError,
    UnsupportedURLError,
)
from app.db.models import DataProviderProfile
from app.jobs.events import JobEvents
from app.jobs.queue import JobQueue
from app.jobs.submit import classify_ingest_url, submit_parsed_ingest
from app.platforms.registry import PlatformRegistry
from app.security.cookies import SYSTEM_USER_ID
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
    user_id: str = Depends(get_user_id),  # noqa: B008
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
    provider_profile: DataProviderProfile | None = None
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
        try:
            provider_profile = await resolve_data_provider_profile(
                session, body.data_provider_profile_id, user_id
            )
        except HTTPException as exc:
            if exc.status_code != status.HTTP_404_NOT_FOUND:
                raise
            # Keep the documented ingest envelope (``data_provider_mismatch``) the
            # frontend maps to an i18n message; the shared resolver speaks plain 404s.
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
        await resolve_llm_profile(session, body.llm_profile_id, user_id)

    async def _check_apify_quota() -> None:
        if (
            provider_profile is not None
            and provider_profile.user_id == SYSTEM_USER_ID
            and provider_profile.daily_limit is not None
        ):
            used = await SystemUsage().used(provider_profile.provider)
            estimated = float(provider_profile.max_cost_usd_per_link)
            if used + estimated > float(provider_profile.daily_limit) + 1e-9:
                raise SystemQuotaExceededError(
                    kind=provider_profile.provider,
                    detail="Hạn mức chi phí Apify dùng chung hôm nay đã hết.",
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
        user_id=user_id,
        before_enqueue=_check_apify_quota,
    )
    if result.cached and result.source_id is not None:
        await upsert_user_source(session, user_id, result.source_id)
        await session.commit()
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
