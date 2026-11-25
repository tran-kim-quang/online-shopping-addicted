"""Arq background tasks orchestrating crawl + post-crawl ingestion."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from app.crawl.service import crawl_url
from app.crawl.types import ProgressEvent
from app.ingest.pipeline import ingest_source
from app.jobs.events import JobEvents

logger = logging.getLogger(__name__)


def _parse_profile_id(raw: str | uuid.UUID | None) -> uuid.UUID | None:
    if raw is None:
        return None
    if isinstance(raw, uuid.UUID):
        return raw
    stripped = str(raw).strip()
    return uuid.UUID(stripped) if stripped else None


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

    try:
        await events.set_status(job_id, "running")
        await events.emit(
            job_id,
            stage="started",
            done=0,
            total=None,
            message=f"Starting crawl and ingestion for {url}",
        )

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

        crawl_result = await crawl_url(
            url,
            session_factory=session_factory,
            registry=registry,
            max_products=max_products,
            max_reviews=max_reviews,
            on_progress=_on_crawl_progress,
        )

        if crawl_result.source_id is not None:
            source_id_str = str(crawl_result.source_id)
            await events.set_status(job_id, "running", source_id=source_id_str)

        if crawl_result.status == "failed" or crawl_result.source_id is None:
            error_msg = crawl_result.error or "Crawl failed."
            await events.set_status(
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

        async def _on_ingest_progress(ev: ProgressEvent) -> None:
            await events.emit(
                job_id,
                stage=ev.stage,
                done=ev.done,
                total=ev.total,
                message=ev.message,
            )

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
        await events.set_status(
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
    except Exception as exc:
        logger.exception("Unhandled error in run_ingest job %s: %s", job_id, exc)
        err = str(exc)
        try:
            await events.set_status(job_id, "failed", source_id=source_id_str, error=err)
            await events.emit(job_id, stage="failed", done=0, total=None, message=err)
        except Exception:
            logger.exception("Failed publishing failure event for job %s", job_id)
        return {
            "job_id": job_id,
            "status": "failed",
            "source_id": source_id_str,
            "error": err,
        }
