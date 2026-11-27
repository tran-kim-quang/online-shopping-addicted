"""Job status and Server-Sent Events (SSE) progress streaming endpoints."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.api.deps import get_job_events, get_settings
from app.core.config import Settings
from app.jobs.events import JobEvents, extract_error_code

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

SSE_KEEPALIVE_SECONDS = 15.0


class JobStatusResponse(BaseModel):
    """Response for ``GET /api/jobs/{job_id}``."""

    job_id: str
    status: str
    source_id: str | None = None
    error: str | None = None
    error_code: str | None = None
    url: str | None = None
    updated_at: str | None = None
    heartbeat_at: str | None = None
    last_event: dict[str, Any] | None = None


def _parse_last_event_id(raw: str | None) -> int:
    if not raw:
        return 0
    try:
        parsed = int(raw.strip())
        return max(0, parsed)
    except ValueError:
        return 0


def _format_sse_frame(event_name: str, data: dict[str, Any] | None) -> str:
    if event_name == "keepalive" or data is None:
        return ": keepalive\n\n"
    seq = data.get("seq")
    lines: list[str] = []
    if seq is not None:
        lines.append(f"id: {seq}")
    lines.append(f"event: {event_name}")
    lines.append(f"data: {json.dumps(data, ensure_ascii=False)}")
    return "\n".join(lines) + "\n\n"


@router.get("/{job_id}", response_model=JobStatusResponse)
async def get_job_status(
    job_id: str,
    job_events: JobEvents = Depends(get_job_events),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> JobStatusResponse:
    """Return current job status, ``source_id``, and most recent progress event."""
    check_stale = getattr(job_events, "check_stale", None)
    if callable(check_stale):
        status_info = await check_stale(
            job_id,
            stale_timeout_seconds=settings.JOB_STALE_TIMEOUT_SECONDS,
            queued_timeout_seconds=settings.JOB_QUEUED_TIMEOUT_SECONDS,
        )
    else:
        status_info = await job_events.get_status(job_id)
    if status_info is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    last_ev = await job_events.last_event(job_id)
    error_code = extract_error_code(status_info.get("error"))
    return JobStatusResponse(**status_info, error_code=error_code, last_event=last_ev)


@router.get("/{job_id}/events")
async def stream_job_events(
    job_id: str,
    request: Request,
    job_events: JobEvents = Depends(get_job_events),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> StreamingResponse:
    """Stream job progress events via SSE with replay, ``Last-Event-ID`` resume, and keepalive."""
    status_info = await job_events.get_status(job_id)
    if status_info is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")

    last_event_id = _parse_last_event_id(request.headers.get("last-event-id"))

    async def _event_generator() -> AsyncIterator[str]:
        async for event_name, payload in job_events.stream(
            job_id,
            last_event_id=last_event_id,
            keepalive_seconds=SSE_KEEPALIVE_SECONDS,
            stale_timeout_seconds=settings.JOB_STALE_TIMEOUT_SECONDS,
            queued_timeout_seconds=settings.JOB_QUEUED_TIMEOUT_SECONDS,
            max_stream_seconds=settings.JOB_STREAM_MAX_SECONDS,
        ):
            yield _format_sse_frame(event_name, payload)

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
