"""Arq background tasks orchestrating crawl + post-crawl ingestion and standalone LLM analysis."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.core.config import get_settings
from app.crawl.service import crawl_url
from app.crawl.types import ProgressEvent
from app.ingest.pipeline import analyze_source, ingest_source
from app.jobs.events import TERMINAL_JOB_STATUSES, JobCancelledError, JobEvents
from app.trends.service import TrendService

logger = logging.getLogger(__name__)


@dataclass
class _JobRuntime:
    """Mutable per-execution state shared between a job runner and its lifecycle harness."""

    source_id_str: str | None = None
    heartbeat_task: asyncio.Task[None] | None = None

    async def stop_heartbeat(self) -> None:
        await _stop_heartbeat(self.heartbeat_task)


async def _run_job_with_lifecycle(
    *,
    task_name: str,
    job_id: str,
    events: JobEvents,
    settings: Any,
    started_message: str,
    initial_source_id: str | None,
    work_fn: Callable[[_JobRuntime], Awaitable[dict[str, Any]]],
    on_finally: Callable[[], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Shared Arq task lifecycle: terminal skip, heartbeat, CAS transitions, and cancellation."""
    runtime = _JobRuntime(source_id_str=initial_source_id)
    cancel_holder: list[JobCancelledError] = []

    try:
        # G1(a): Skip immediately if the job already reached a terminal state (e.g. worker_timeout
        # before the worker restarted and Arq retried the task).
        existing = await _safe_get_status(events, job_id)
        if existing is not None and existing.get("status") in TERMINAL_JOB_STATUSES:
            logger.warning(
                "Skipping %s for job %s: already in terminal status '%s' (error=%s).",
                task_name,
                job_id,
                existing.get("status"),
                existing.get("error"),
            )
            return {
                "job_id": job_id,
                "status": existing["status"],
                "source_id": existing.get("source_id") or runtime.source_id_str,
                "error": existing.get("error"),
            }

        await _set_status_or_cancel(
            events,
            job_id,
            "running",
            source_id=runtime.source_id_str,
        )
        await events.emit(
            job_id,
            stage="started",
            done=0,
            total=None,
            message=started_message,
        )

        work_task = asyncio.create_task(work_fn(runtime))
        heartbeat_interval = _resolve_heartbeat_interval(settings)
        runtime.heartbeat_task = asyncio.create_task(
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
            "Aborting %s for job %s without modifying terminal status: %s",
            task_name,
            job_id,
            exc,
        )
        latest = await _safe_get_status(events, job_id)
        return {
            "job_id": job_id,
            "status": str((latest or {}).get("status") or exc.status),
            "source_id": (latest or {}).get("source_id") or runtime.source_id_str,
            "error": (latest or {}).get("error") or exc.error,
        }
    except Exception as exc:
        logger.exception("Unhandled error in %s job %s: %s", task_name, job_id, exc)
        err = str(exc)
        try:
            await runtime.stop_heartbeat()
            updated = await events.set_status(
                job_id,
                "failed",
                source_id=runtime.source_id_str,
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
                    "source_id": latest.get("source_id") or runtime.source_id_str,
                    "error": latest.get("error") or err,
                }
        except Exception:
            logger.exception("Failed publishing failure event for job %s", job_id)
        return {
            "job_id": job_id,
            "status": "failed",
            "source_id": runtime.source_id_str,
            "error": err,
        }
    finally:
        await runtime.stop_heartbeat()
        if on_finally is not None:
            try:
                await on_finally()
            except Exception as exc:
                logger.warning("Cleanup hook failed for %s job %s: %s", task_name, job_id, exc)


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


async def _record_source_last_job_id(
    source_id: uuid.UUID,
    job_id: str,
    *,
    session_factory: Any = None,
) -> None:
    """Persist ``sources.stats['last_job_id'] = job_id`` so UI can resume SSE on running sources."""
    factory = session_factory
    if factory is None:
        from app.db.database import Database

        try:
            factory = Database().session_factory
        except RuntimeError:
            return

    try:
        from app.db.models import Source

        async with factory() as session:
            source = await session.get(Source, source_id)
            if source is not None:
                merged = dict(source.stats or {})
                merged["last_job_id"] = job_id
                source.stats = merged
                await session.commit()
    except Exception as exc:
        logger.warning(
            "Failed recording last_job_id=%s on source %s: %s",
            job_id,
            source_id,
            exc,
        )


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
    data_provider_profile_id: str | uuid.UUID | None = None,
    **legacy_kwargs: Any,
) -> dict[str, Any]:
    """Arq task: crawl ``url`` then run ``ingest_source``, streaming progress via ``JobEvents``.

    Dependencies may be injected via ``ctx`` for unit/integration testing:
    ``session_factory``, ``registry``, ``embedder``, ``llm_client``, ``job_events``, ``settings``,
    ``secret_box``, ``apify_client``.

    ``legacy_kwargs`` keeps jobs enqueued before the cache-bypass removal (they still carry
    ``force=...`` in their serialized Arq payload) from dying with ``TypeError``: unknown
    kwargs are logged and dropped.
    """
    if legacy_kwargs:
        logger.warning(
            "run_ingest job %s received unexpected kwargs %s; ignoring them.",
            job_id,
            sorted(legacy_kwargs),
        )
    events: JobEvents = ctx.get("job_events") or JobEvents()
    session_factory = ctx.get("session_factory")
    registry = ctx.get("registry")
    embedder = ctx.get("embedder")
    llm_client = ctx.get("llm_client")
    secret_box = ctx.get("secret_box")
    apify_client = ctx.get("apify_client")
    settings = ctx.get("settings")

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

    async def _execute_pipeline(runtime: _JobRuntime) -> dict[str, Any]:
        async def _on_source_created(created_source_id: uuid.UUID) -> None:
            # G2: Record source_id on the job hash as soon as crawl creates the Source row
            # so worker_timeout can also mark sources.status='failed' if the worker dies mid-crawl,
            # and store last_job_id in sources.stats so the UI can follow SSE for running sources.
            runtime.source_id_str = str(created_source_id)
            await _set_status_or_cancel(
                events,
                job_id,
                "running",
                source_id=runtime.source_id_str,
            )
            await _record_source_last_job_id(
                created_source_id,
                job_id,
                session_factory=session_factory,
            )

        async def _crawl_heartbeat() -> None:
            touch = getattr(events, "touch_heartbeat", None)
            if callable(touch):
                await touch(job_id)

        parsed_provider_id = _parse_profile_id(data_provider_profile_id)
        crawl_kwargs: dict[str, Any] = {
            "session_factory": session_factory,
            "registry": registry,
            "max_products": max_products,
            "max_reviews": max_reviews,
            "on_progress": _on_crawl_progress,
            "on_source_created": _on_source_created,
        }
        if parsed_provider_id is not None:
            crawl_kwargs["data_provider_profile_id"] = parsed_provider_id
        if secret_box is not None:
            crawl_kwargs["secret_box"] = secret_box
        if apify_client is not None:
            crawl_kwargs["apify_client"] = apify_client
        crawl_kwargs["heartbeat"] = _crawl_heartbeat

        crawl_result = await crawl_url(url, **crawl_kwargs)

        if crawl_result.source_id is not None:
            resolved_source_id = str(crawl_result.source_id)
            if resolved_source_id != runtime.source_id_str:
                runtime.source_id_str = resolved_source_id
                await _set_status_or_cancel(
                    events,
                    job_id,
                    "running",
                    source_id=runtime.source_id_str,
                )

        if crawl_result.status == "failed" or crawl_result.source_id is None:
            error_msg = crawl_result.error or "Crawl failed."
            await runtime.stop_heartbeat()
            await _set_status_or_cancel(
                events,
                job_id,
                "failed",
                source_id=runtime.source_id_str,
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
                "source_id": runtime.source_id_str,
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
        await runtime.stop_heartbeat()
        await _set_status_or_cancel(
            events,
            job_id,
            final_status,
            source_id=runtime.source_id_str,
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
            "source_id": runtime.source_id_str,
            "error": ingest_result.error,
        }

    return await _run_job_with_lifecycle(
        task_name="run_ingest",
        job_id=job_id,
        events=events,
        settings=settings,
        started_message=f"Starting crawl and ingestion for {url}",
        initial_source_id=None,
        work_fn=_execute_pipeline,
    )


async def run_analyze(
    ctx: dict[str, Any],
    job_id: str,
    source_id: str | uuid.UUID,
    llm_profile_id: str | uuid.UUID,
) -> dict[str, Any]:
    """Arq task: run standalone LLM sentiment & aspect analysis for an existing source.

    Reuses the shared ``JobEvents`` lifecycle (heartbeat, stale timeout, terminal CAS,
    ``JobCancelledError``) and releases the per-source Redis analyze lock on completion.
    """
    events: JobEvents = ctx.get("job_events") or JobEvents()
    session_factory = ctx.get("session_factory")
    llm_client = ctx.get("llm_client")
    secret_box = ctx.get("secret_box")
    settings = ctx.get("settings")

    parsed_source_id = source_id if isinstance(source_id, uuid.UUID) else uuid.UUID(str(source_id))
    parsed_profile_id = _parse_profile_id(llm_profile_id)
    if parsed_profile_id is None:
        raise ValueError("llm_profile_id is required for run_analyze.")
    source_id_str = str(parsed_source_id)

    async def _on_analyze_progress(ev: ProgressEvent) -> None:
        await events.emit(
            job_id,
            stage=ev.stage,
            done=ev.done,
            total=ev.total,
            message=ev.message,
        )

    async def _execute_analyze(runtime: _JobRuntime) -> dict[str, Any]:
        result = await analyze_source(
            parsed_source_id,
            llm_profile_id=parsed_profile_id,
            on_progress=_on_analyze_progress,
            session_factory=session_factory,
            llm_client=llm_client,
            secret_box=secret_box,
            settings=settings,
        )

        final_status = result.status
        final_error = result.error or ""
        await runtime.stop_heartbeat()
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
            done=result.stats.reviews_llm,
            total=result.stats.reviews_to_analyze,
            message=(
                f"Completed LLM analysis with status={final_status} "
                f"(llm={result.stats.reviews_llm}/{result.stats.reviews_to_analyze}, "
                f"fallback={result.stats.reviews_rating_fallback}, "
                f"summaries_invalidated={result.stats.summaries_invalidated})"
            ),
        )
        return {
            "job_id": job_id,
            "status": final_status,
            "source_id": source_id_str,
            "error": result.error,
        }

    async def _release_lock() -> None:
        release_fn = getattr(events, "release_source_lock", None)
        if callable(release_fn):
            await release_fn(source_id_str, job_id)

    return await _run_job_with_lifecycle(
        task_name="run_analyze",
        job_id=job_id,
        events=events,
        settings=settings,
        started_message=f"Starting LLM sentiment analysis for source {source_id_str}",
        initial_source_id=source_id_str,
        work_fn=_execute_analyze,
        on_finally=_release_lock,
    )


async def run_trend_search(
    ctx: dict[str, Any],
    job_id: str,
    trend_search_id: str | uuid.UUID,
) -> dict[str, Any]:
    """Arq task: execute TikTok brand trend discovery for a ``TrendSearch`` row."""
    from app.db.database import Database
    from app.db.models import TrendSearch

    events: JobEvents = ctx.get("job_events") or JobEvents()
    session_factory = ctx.get("session_factory")
    sc_client_factory = ctx.get("sc_client_factory")
    llm_client = ctx.get("llm_client")
    secret_box = ctx.get("secret_box")
    settings = ctx.get("settings")

    parsed_id = (
        trend_search_id
        if isinstance(trend_search_id, uuid.UUID)
        else uuid.UUID(str(trend_search_id))
    )
    search_id_str = str(parsed_id)

    lock_identity: list[tuple[str, str]] = []
    sf = session_factory if session_factory is not None else Database().session_factory
    async with sf() as session:
        row = await session.get(TrendSearch, parsed_id)
        if row is not None:
            lock_identity.append((row.user_id, row.keyword_norm))

    async def _on_trend_progress(ev: ProgressEvent) -> None:
        await events.emit(
            job_id,
            stage=ev.stage,
            done=ev.done,
            total=ev.total,
            message=ev.message,
        )

    async def _close_row(status: str, error: str) -> None:
        # Keep the DB row in step with the job so the keyword is never left "running".
        try:
            async with sf() as session:
                row = await session.get(TrendSearch, parsed_id)
                if row is not None and row.status in ("queued", "running"):
                    row.status = status
                    row.error = error
                    row.finished_at = datetime.now(UTC)
                    await session.commit()
        except Exception:
            logger.exception("Failed closing trend search %s", search_id_str)

    async def _execute_trend(runtime: _JobRuntime) -> dict[str, Any]:
        try:
            result = await TrendService().run_search(
                parsed_id,
                on_progress=_on_trend_progress,
                session_factory=session_factory,
                sc_client_factory=sc_client_factory,
                llm_client=llm_client,
                secret_box=secret_box,
                settings=settings,
            )
        except (JobCancelledError, asyncio.CancelledError):
            await _close_row("cancelled", "cancelled")
            raise
        except Exception:
            await _close_row("failed", "internal_error")
            raise
        final_status = "done" if result.status == "completed" else result.status
        final_error = result.error or ""
        await runtime.stop_heartbeat()
        await _set_status_or_cancel(
            events,
            job_id,
            final_status,
            source_id=search_id_str,
            error=final_error,
        )
        await events.emit(
            job_id,
            stage=final_status,
            done=result.videos_scanned,
            total=result.videos_scanned,
            message=(
                f"Completed trend search with status={final_status} "
                f"(brands={result.brands_count}, videos={result.videos_scanned}, "
                f"comments={result.comments_scanned}, credits={result.credits_used})"
            ),
        )
        return {
            "job_id": job_id,
            "status": final_status,
            "source_id": search_id_str,
            "error": result.error,
        }

    async def _release_lock() -> None:
        if not lock_identity:
            return
        user_id, keyword_norm = lock_identity[0]
        release_fn = getattr(events, "release_trend_lock", None)
        if callable(release_fn):
            await release_fn(user_id, keyword_norm, job_id)

    return await _run_job_with_lifecycle(
        task_name="run_trend_search",
        job_id=job_id,
        events=events,
        settings=settings,
        started_message=f"Starting TikTok trend search {search_id_str}",
        initial_source_id=search_id_str,
        work_fn=_execute_trend,
        on_finally=_release_lock,
    )
