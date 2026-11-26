"""Shared ingest URL classification, TTL cache lookup, and Arq enqueue helper.

Used by both ``POST /api/ingest`` and the QA Agent's ``crawl_more`` tool so cache
semantics and URL validation stay identical across entry points.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.crawl.classifier import classify_url, is_tiki_short_url, resolve_and_classify_url
from app.crawl.types import ParsedURL
from app.db.models import Source
from app.jobs.events import JobEvents
from app.jobs.queue import JobQueue
from app.security.url_validator import DNSResolver

ResolveShortUrlFn = Callable[..., Awaitable[ParsedURL]]


class IngestSubmitResult(BaseModel):
    """Outcome of submitting a URL for crawl + ingestion."""

    cached: bool
    canonical_url: str
    source_id: uuid.UUID | None = None
    job_id: str | None = None


async def classify_ingest_url(
    url: str,
    *,
    settings: Settings,
    resolver: DNSResolver | None = None,
    resolve_short_url_fn: ResolveShortUrlFn | None = None,
) -> ParsedURL:
    """Classify ``url`` (resolving ``ti.ki`` short links through ``resolver`` if needed)."""
    if is_tiki_short_url(url):
        fn = resolve_short_url_fn if resolve_short_url_fn is not None else resolve_and_classify_url
        return await fn(
            url,
            allow_private=settings.ALLOW_PRIVATE_CRAWL_HOSTS,
            resolver=resolver,
        )
    return classify_url(url)


async def submit_parsed_ingest(
    parsed: ParsedURL,
    *,
    session: AsyncSession,
    settings: Settings,
    job_events: JobEvents,
    job_queue: JobQueue,
    llm_profile_id: uuid.UUID | None = None,
    max_products: int | None = None,
    max_reviews: int | None = None,
    force: bool = False,
) -> IngestSubmitResult:
    """Check TTL cache for ``parsed`` and enqueue an Arq ingest job on cache miss."""
    if not force and settings.CRAWL_CACHE_TTL_HOURS > 0:
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
            return IngestSubmitResult(
                cached=True,
                canonical_url=parsed.canonical_url,
                source_id=cached_source.id,
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
        llm_profile_id=str(llm_profile_id) if llm_profile_id is not None else None,
        max_products=max_products,
        max_reviews=max_reviews,
        force=force,
    )
    return IngestSubmitResult(
        cached=False,
        canonical_url=parsed.canonical_url,
        job_id=job_id,
    )


async def submit_ingest_url(
    url: str,
    *,
    session: AsyncSession,
    settings: Settings,
    job_events: JobEvents,
    job_queue: JobQueue,
    resolver: DNSResolver | None = None,
    llm_profile_id: uuid.UUID | None = None,
    max_products: int | None = None,
    max_reviews: int | None = None,
    force: bool = False,
    resolve_short_url_fn: ResolveShortUrlFn | None = None,
) -> IngestSubmitResult:
    """Classify ``url``, respect TTL cache, and enqueue an Arq ingest job when needed."""
    parsed = await classify_ingest_url(
        url,
        settings=settings,
        resolver=resolver,
        resolve_short_url_fn=resolve_short_url_fn,
    )
    return await submit_parsed_ingest(
        parsed,
        session=session,
        settings=settings,
        job_events=job_events,
        job_queue=job_queue,
        llm_profile_id=llm_profile_id,
        max_products=max_products,
        max_reviews=max_reviews,
        force=force,
    )
