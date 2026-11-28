"""API endpoints for TikTok brand trend searches."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_job_events, get_job_queue, get_session
from app.db.models import DataProviderProfile, LLMProfile, TrendSearch
from app.jobs.events import TERMINAL_JOB_STATUSES, JobEvents
from app.jobs.queue import JobQueue
from app.trends.config import CACHE_HOURS, ESTIMATED_CREDITS
from app.trends.service import compute_keyword_norm

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/trends", tags=["trends"])


class CreateTrendSearchRequest(BaseModel):
    """Payload for ``POST /api/trends/searches``."""

    model_config = ConfigDict(extra="forbid")

    keyword: str = Field(min_length=2, max_length=100)
    data_provider_profile_id: uuid.UUID
    llm_profile_id: uuid.UUID
    force: bool = False

    @field_validator("keyword")
    @classmethod
    def _strip_keyword(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) < 2:
            raise ValueError("Keyword must be at least 2 characters.")
        return stripped


class TrendSearchQueuedResponse(BaseModel):
    """202 response for ``POST /api/trends/searches``."""

    search_id: uuid.UUID
    job_id: str | None = None
    cached: bool
    estimated_credits: int
    max_credits: int


class TrendBrandSentiment(BaseModel):
    """Aggregated sentiment counts and percentages for a brand."""

    positive: int
    neutral: int
    negative: int
    total: int
    positive_pct: float
    neutral_pct: float
    negative_pct: float


class TrendEvidenceVideo(BaseModel):
    """Representative TikTok video mentioning a brand."""

    url: str
    desc: str
    author_id: str
    plays: int
    likes: int
    created_at: str


class TrendBrand(BaseModel):
    """Ranked brand entry in a completed trend search."""

    name: str
    score: float
    videos: int
    creators: int
    plays: int
    likes: int
    sentiment: TrendBrandSentiment
    ad_share: float
    single_video: bool
    evidence: list[TrendEvidenceVideo] = Field(default_factory=list)


class TrendSearchSummary(BaseModel):
    """History item returned by ``GET /api/trends/searches`` (without ``result``)."""

    id: uuid.UUID
    keyword: str
    platform: str
    status: str
    credits_used: int
    videos_scanned: int
    comments_scanned: int
    window_start: datetime | None = None
    window_end: datetime | None = None
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
    job_id: str | None = None
    created_at: datetime
    finished_at: datetime | None = None


class TrendSearchDetail(TrendSearchSummary):
    """Full trend search detail returned by ``GET /api/trends/searches/{search_id}``."""

    brands: list[TrendBrand] = Field(default_factory=list)
    videos_considered: int = 0


def _to_summary(row: TrendSearch) -> TrendSearchSummary:
    return TrendSearchSummary(
        id=row.id,
        keyword=row.keyword,
        platform=row.platform,
        status=row.status,
        credits_used=row.credits_used,
        videos_scanned=row.videos_scanned,
        comments_scanned=row.comments_scanned,
        window_start=row.window_start,
        window_end=row.window_end,
        warnings=[str(w) for w in (row.warnings or [])],
        error=row.error,
        job_id=row.job_id,
        created_at=row.created_at,
        finished_at=row.finished_at,
    )


def _to_detail(row: TrendSearch) -> TrendSearchDetail:
    raw_result: dict[str, Any] = row.result if isinstance(row.result, dict) else {}
    raw_brands = raw_result.get("brands") or []
    brands = [TrendBrand.model_validate(b) for b in raw_brands]
    videos_considered = int(raw_result.get("videos_considered") or 0)
    return TrendSearchDetail(
        id=row.id,
        keyword=row.keyword,
        platform=row.platform,
        status=row.status,
        credits_used=row.credits_used,
        videos_scanned=row.videos_scanned,
        comments_scanned=row.comments_scanned,
        window_start=row.window_start,
        window_end=row.window_end,
        warnings=[str(w) for w in (row.warnings or [])],
        error=row.error,
        job_id=row.job_id,
        created_at=row.created_at,
        finished_at=row.finished_at,
        brands=brands,
        videos_considered=videos_considered,
    )


async def _job_still_active(job_events: JobEvents, job_id: str | None) -> bool:
    """True when the row's job exists in Redis and is neither terminal nor stale."""
    if not job_id:
        return False
    try:
        info = await job_events.check_stale(job_id)
    except Exception:
        logger.warning("Could not read job status for trend job %s", job_id, exc_info=True)
        return True
    return info is not None and info.get("status") not in TERMINAL_JOB_STATUSES


@router.post(
    "/searches",
    response_model=TrendSearchQueuedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_trend_search(
    body: CreateTrendSearchRequest,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    job_events: JobEvents = Depends(get_job_events),  # noqa: B008
    job_queue: JobQueue = Depends(get_job_queue),  # noqa: B008
) -> JSONResponse:
    """Create or return a cached/in-progress TikTok brand trend search."""
    data_profile = await session.get(DataProviderProfile, body.data_provider_profile_id)
    if data_profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Data provider profile not found.",
        )
    if data_profile.provider != "scrapecreators":
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "error": "wrong_provider",
                "detail": "Trend search requires a 'scrapecreators' data provider profile.",
            },
        )

    llm_profile = await session.get(LLMProfile, body.llm_profile_id)
    if llm_profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="LLM profile not found.",
        )

    max_credits = data_profile.max_credits_per_search or 15
    keyword_norm = compute_keyword_norm(body.keyword)
    if not keyword_norm:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Keyword must contain alphanumeric characters.",
        )

    user_id = "local"
    now = datetime.now(UTC)

    if not body.force:
        cutoff = now - timedelta(hours=CACHE_HOURS)
        cached_stmt = (
            select(TrendSearch)
            .where(
                TrendSearch.user_id == user_id,
                TrendSearch.keyword_norm == keyword_norm,
                TrendSearch.status == "completed",
                TrendSearch.finished_at >= cutoff,
            )
            .order_by(TrendSearch.finished_at.desc())
            .limit(1)
        )
        cached_row = (await session.execute(cached_stmt)).scalar_one_or_none()
        if cached_row is not None:
            payload = TrendSearchQueuedResponse(
                search_id=cached_row.id,
                job_id=None,
                cached=True,
                estimated_credits=ESTIMATED_CREDITS,
                max_credits=max_credits,
            )
            return JSONResponse(
                status_code=status.HTTP_202_ACCEPTED,
                content=payload.model_dump(mode="json"),
            )

    running_stmt = (
        select(TrendSearch)
        .where(
            TrendSearch.user_id == user_id,
            TrendSearch.keyword_norm == keyword_norm,
            TrendSearch.status.in_(("queued", "running")),
        )
        .order_by(TrendSearch.created_at.desc())
        .limit(1)
    )
    running_row = (await session.execute(running_stmt)).scalar_one_or_none()
    if running_row is not None and not await _job_still_active(job_events, running_row.job_id):
        # The worker died or the job ended without updating the row: close it so the
        # keyword is not blocked forever.
        running_row.status = "failed"
        running_row.error = running_row.error or "worker_lost"
        running_row.finished_at = now
        await session.commit()
        running_row = None
    if running_row is not None:
        payload = TrendSearchQueuedResponse(
            search_id=running_row.id,
            job_id=running_row.job_id,
            cached=False,
            estimated_credits=ESTIMATED_CREDITS,
            max_credits=max_credits,
        )
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=payload.model_dump(mode="json"),
        )

    job_id = str(uuid.uuid4())
    acquired, holder_job_id = await job_events.acquire_trend_lock(user_id, keyword_norm, job_id)
    if not acquired:
        existing_running = (await session.execute(running_stmt)).scalar_one_or_none()
        if existing_running is not None:
            payload = TrendSearchQueuedResponse(
                search_id=existing_running.id,
                job_id=existing_running.job_id or holder_job_id,
                cached=False,
                estimated_credits=ESTIMATED_CREDITS,
                max_credits=max_credits,
            )
            return JSONResponse(
                status_code=status.HTTP_202_ACCEPTED,
                content=payload.model_dump(mode="json"),
            )

    search_row = TrendSearch(
        user_id=user_id,
        keyword=body.keyword,
        keyword_norm=keyword_norm,
        platform="tiktok",
        status="queued",
        data_provider_profile_id=data_profile.id,
        llm_profile_id=llm_profile.id,
        job_id=job_id,
    )
    session.add(search_row)
    await session.commit()
    await session.refresh(search_row)

    try:
        await job_events.create_job(
            job_id,
            url=body.keyword,
            source_id=str(search_row.id),
            job_type="trend_search",
        )
        await job_queue.enqueue_trend_search(
            job_id=job_id,
            trend_search_id=str(search_row.id),
        )
    except Exception:
        await job_events.release_trend_lock(user_id, keyword_norm, job_id)
        search_row.status = "failed"
        search_row.error = "enqueue_failed"
        search_row.finished_at = datetime.now(UTC)
        await session.commit()
        raise

    payload = TrendSearchQueuedResponse(
        search_id=search_row.id,
        job_id=job_id,
        cached=False,
        estimated_credits=ESTIMATED_CREDITS,
        max_credits=max_credits,
    )
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=payload.model_dump(mode="json"),
    )


@router.get("/searches", response_model=list[TrendSearchSummary])
async def list_trend_searches(
    limit: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[TrendSearchSummary]:
    """List recent TikTok brand trend searches without full result payloads."""
    stmt = (
        select(TrendSearch)
        .where(TrendSearch.user_id == "local")
        .order_by(TrendSearch.created_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [_to_summary(row) for row in rows]


@router.get("/searches/{search_id}", response_model=TrendSearchDetail)
async def get_trend_search(
    search_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> TrendSearchDetail:
    """Return full details and ranked brands for a trend search."""
    row = await session.get(TrendSearch, search_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Trend search not found.",
        )
    return _to_detail(row)
