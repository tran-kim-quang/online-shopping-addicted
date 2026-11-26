"""Arq background tasks orchestrating crawl + post-crawl ingestion."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from typing import Any

from app.core.config import get_settings
from app.crawl.service import crawl_url
from app.crawl.types import ProgressEvent
from app.ingest.pipeline import ingest_source
from app.jobs.events import TERMINAL_JOB_STATUSES, JobCancelledError, JobEvents

logger = logging.getLogger(__name__)


def _resolve_heartbeat_interval(settings: Any) -> float:
    if settings is not None and hasattr(settings, "JOB_HEARTBEAT_INTERVAL_SECONDS"):
        return max(0.05, float(settings.JOB_HEARTBEAT_INTERVAL_SECONDS))
    try:
        return max(0.05, float(get_settings().JOB_HEARTBEAT_INTERVAL_SECONDS))
    except Exception:
        return 15.0


async def _heartbeat_loop(
    events: JobEvents,
    job_id: str,
    interval: float,
    work_task: asyncio.Task[Any],
    cancel_holder: list[JobCancelledError],
) -> None:
    """Periodically update ``heartbeat_at`` in Redis until cancelled or job turns terminal."""
    touch = getattr(events, "touch_heartbeat", None)
    if not callable(touch):
        return
    while True:
        await asyncio.sleep(interval)
        try:
            await touch(job_id)
        except asyncio.CancelledError:
            raise
        except JobCancelledError as exc:
            cancel_holder.append(exc)
            if not work_task.done():
                work_task.cancel()
            return
        except Exception as exc:
            logger.warning("Heartbeat update failed for job %s: %s", job_id, exc)


def _parse_profile_id(raw: str | uuid.UUID | None) -> uuid.UUID | None:
    if raw is None:
        return None
    if isinstance(raw, uuid.UUID):
        return raw
    stripped = str(raw).strip()
    return uuid.UUID(stripped) if stripped else None


async def _safe_get_status(events: JobEvents, job_id: str) -> dict[str, Any] | None:
    get_status = getattr(events, "get_status", None)
    if not callable(get_status):
        return None
    return await get_status(job_id)


async def _stop_heartbeat(heartbeat_task: asyncio.Task[None] | None) -> None:
    if heartbeat_task is not None and not heartbeat_task.done():
        heartbeat_task.cancel()
    if heartbeat_task is not None:
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task


async def _set_status_or_cancel(
    events: JobEvents,
    job_id: str,
    status: str,
    *,
    source_id: str | None = None,
    error: str | None = None,
) -> None:
    """Call ``events.set_status`` and raise :class:`JobCancelledError` if CAS rejected it."""
    updated = await events.set_status(job_id, status, source_id=source_id, error=error)
    if updated is False:
        latest = await _safe_get_status(events, job_id)
        raise JobCancelledError(
            job_id,
            status=str((latest or {}).get("status") or "failed"),
            error=(latest or {}).get("error"),
        )


async def run_ingest(
    ctx: dict[str, Any],
    job_id: str,
    url: str,
    llm_profile_id: str | uuid.UUID | None = None,
    max_products: int | None = None,
    max_reviews: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Arq task: crawl ``url`` then run ``ingest_source``, streaming progress via ``JobEvents``.

    Dependencies may be injected via ``ctx`` for unit/integration testing:
    ``session_factory``, ``registry``, ``embedder``, ``llm_client``, ``job_events``, ``settings``.
    """
    _ = force  # Cache bypass is enforced at enqueue time; kept in signature per spec.
    events: JobEvents = ctx.get("job_events") or JobEvents()
    session_factory = ctx.get("session_factory")
    registry = ctx.get("registry")
    embedder = ctx.get("embedder")
    llm_client = ctx.get("llm_client")
    settings = ctx.get("settings")

    source_id_str: str | None = None
    heartbeat_task: asyncio.Task[None] | None = None
    cancel_holder: list[JobCancelledError] = []

    try:
        # G1(a): Skip immediately if the job already reached a terminal state (e.g. worker_timeout
        # before the worker restarted and Arq retried the task).
        existing = await _safe_get_status(events, job_id)
        if existing is not None and existing.get("status") in TERMINAL_JOB_STATUSES:
            logger.warning(
                "Skipping run_ingest for job %s: already in terminal status '%s' (error=%s).",
                job_id,
                existing.get("status"),
                existing.get("error"),
            )
            return {
                "job_id": job_id,
                "status": existing["status"],
                "source_id": existing.get("source_id"),
                "error": existing.get("error"),
            }

        await _set_status_or_cancel(events, job_id, "running")
        await events.emit(
            job_id,
            stage="started",
            done=0,
            total=None,
            message=f"Starting crawl and ingestion for {url}",
        )

        async def _on_source_created(created_source_id: uuid.UUID) -> None:
            # G2: Record source_id on the job hash as soon as crawl creates the Source row
            # so worker_timeout can also mark sources.status='failed' if the worker dies mid-crawl.
            nonlocal source_id_str
            source_id_str = str(created_source_id)
            await _set_status_or_cancel(events, job_id, "running", source_id=source_id_str)

        async def _on_crawl_progress(ev: ProgressEvent) -> None:
            # Remap terminal crawl stages so SSE stays open until ingestion finishes.
            stage = ev.stage
            if stage == "done":
                stage = "crawl_done"
            elif stage == "failed":
                stage = "crawl_failed"
            await events.emit(
                job_id,
                stage=stage,
                done=ev.done,
                total=ev.total,
                message=ev.message,
            )

        async def _on_ingest_progress(ev: ProgressEvent) -> None:
            await events.emit(
                job_id,
                stage=ev.stage,
                done=ev.done,
                total=ev.total,
                message=ev.message,
            )

        async def _execute_pipeline() -> dict[str, Any]:
            nonlocal source_id_str
            crawl_result = await crawl_url(
                url,
                session_factory=session_factory,
                registry=registry,
                max_products=max_products,
                max_reviews=max_reviews,
                on_progress=_on_crawl_progress,
                on_source_created=_on_source_created,
            )

            if crawl_result.source_id is not None:
                resolved_source_id = str(crawl_result.source_id)
                if resolved_source_id != source_id_str:
                    source_id_str = resolved_source_id
                    await _set_status_or_cancel(events, job_id, "running", source_id=source_id_str)

            if crawl_result.status == "failed" or crawl_result.source_id is None:
                error_msg = crawl_result.error or "Crawl failed."
                await _stop_heartbeat(heartbeat_task)
                await _set_status_or_cancel(
                    events,
                    job_id,
                    "failed",
                    source_id=source_id_str,
                    error=error_msg,
                )
                await events.emit(
                    job_id,
                    stage="failed",
                    done=0,
                    total=None,
                    message=error_msg,
                )
                return {
                    "job_id": job_id,
                    "status": "failed",
                    "source_id": source_id_str,
                    "error": error_msg,
                }

            parsed_profile_id = _parse_profile_id(llm_profile_id)
            ingest_result = await ingest_source(
                crawl_result.source_id,
                llm_profile_id=parsed_profile_id,
                on_progress=_on_ingest_progress,
                session_factory=session_factory,
                embedder=embedder,
                llm_client=llm_client,
                settings=settings,
            )

            final_status = ingest_result.status
            final_error = ingest_result.error or ""
            await _stop_heartbeat(heartbeat_task)
            await _set_status_or_cancel(
                events,
                job_id,
                final_status,
                source_id=source_id_str,
                error=final_error,
            )
            await events.emit(
                job_id,
                stage=final_status,
                done=ingest_result.stats.products_ingested,
                total=ingest_result.stats.products_total,
                message=(
                    f"Completed with status={final_status} "
                    f"(products={ingest_result.stats.products_ingested}/"
                    f"{ingest_result.stats.products_total}, "
                    f"chunks={ingest_result.stats.chunks_total}, "
                    f"embedded={ingest_result.stats.chunks_embedded})"
                ),
            )
            return {
                "job_id": job_id,
                "status": final_status,
                "source_id": source_id_str,
                "error": ingest_result.error,
            }

        work_task = asyncio.create_task(_execute_pipeline())
        heartbeat_interval = _resolve_heartbeat_interval(settings)
        heartbeat_task = asyncio.create_task(
            _heartbeat_loop(events, job_id, heartbeat_interval, work_task, cancel_holder)
        )
        try:
            return await work_task
        except asyncio.CancelledError:
            if cancel_holder:
                raise cancel_holder[0] from None
            raise
    except JobCancelledError as exc:
        logger.warning(
            "Aborting run_ingest for job %s without modifying terminal status: %s",
            job_id,
            exc,
        )
        latest = await _safe_get_status(events, job_id)
        return {
            "job_id": job_id,
            "status": str((latest or {}).get("status") or exc.status),
            "source_id": (latest or {}).get("source_id") or source_id_str,
            "error": (latest or {}).get("error") or exc.error,
        }
    except Exception as exc:
        logger.exception("Unhandled error in run_ingest job %s: %s", job_id, exc)
        err = str(exc)
        try:
            await _stop_heartbeat(heartbeat_task)
            updated = await events.set_status(
                job_id,
                "failed",
                source_id=source_id_str,
                error=err,
            )
            if updated is not False:
                await events.emit(job_id, stage="failed", done=0, total=None, message=err)
        except JobCancelledError:
            logger.warning(
                "Job %s already reached terminal state while reporting error; keeping original.",
                job_id,
            )
            latest = await _safe_get_status(events, job_id)
            if latest is not None:
                return {
                    "job_id": job_id,
                    "status": str(latest.get("status") or "failed"),
                    "source_id": latest.get("source_id") or source_id_str,
                    "error": latest.get("error") or err,
                }
        except Exception:
            logger.exception("Failed publishing failure event for job %s", job_id)
        return {
            "job_id": job_id,
            "status": "failed",
            "source_id": source_id_str,
            "error": err,
        }
    finally:
        await _stop_heartbeat(heartbeat_task)
