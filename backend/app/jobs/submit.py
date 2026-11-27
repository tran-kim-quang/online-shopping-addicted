"""Shared ingest URL classification, TTL cache lookup, and Arq enqueue helper.

Used by both ``POST /api/ingest`` and the QA Agent's ``crawl_more`` tool so cache
semantics and URL validation stay identical across entry points.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel
from sqlalchemy import and_, false, or_, select
from sqlalchemy.dialects.postgresql.base import PGCompiler, PGDialect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.core.config import Settings
from app.crawl.classifier import classify_url, is_short_url, resolve_and_classify_url
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
    crawled_at: datetime | None = None
    refresh_after: datetime | None = None


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def compute_refresh_after(
    crawled_at: datetime | None,
    ttl_hours: int,
) -> datetime | None:
    """Compute the UTC timestamp after which a cached source expires."""
    if crawled_at is None:
        return None
    return _to_utc(crawled_at) + timedelta(hours=ttl_hours)


def fresh_cache_condition(
    *,
    ttl_hours: int,
    now: datetime | None = None,
    source_table: Any = Source,
) -> ColumnElement[bool]:
    """Shared SQLAlchemy predicate defining a source within the crawl cache TTL.

    Used by both ``submit_parsed_ingest`` (cache hit lookup) and
    ``GET /api/sources?fresh=true`` so the freshness contract stays identical.
    """
    if ttl_hours <= 0:
        return false()
    now_utc = _to_utc(now) if now is not None else datetime.now(UTC)
    cutoff = now_utc - timedelta(hours=ttl_hours)
    cols = source_table.c if hasattr(source_table, "c") else source_table
    return and_(
        cols.status == "done",
        cols.crawled_at.is_not(None),
        cols.crawled_at >= cutoff,
        # Crawl-only sources (e.g. Phase 2 CLI runs) have no chunks yet: not a cache hit.
        cols.stats.has_key("ingest"),
    )


class _NamedPGCompiler(PGCompiler):
    """Compile PostgreSQL predicates with plain ``:name`` placeholders for ``text()``."""

    def render_bind_cast(self, type_: Any, dbapi_type: Any, sqltext: str) -> str:
        return sqltext


class _NamedPGDialect(PGDialect):
    statement_compiler = _NamedPGCompiler


_NAMED_PG_DIALECT = _NamedPGDialect(paramstyle="named")


def compile_fresh_cache_sql(
    *,
    ttl_hours: int,
    now: datetime | None = None,
    alias: str = "s",
) -> tuple[str, dict[str, Any]]:
    """Compile :func:`fresh_cache_condition` into a named-parameter PostgreSQL clause."""
    aliased_table = Source.__table__.alias(alias)
    predicate = fresh_cache_condition(
        ttl_hours=ttl_hours,
        now=now,
        source_table=aliased_table,
    )
    compiled = predicate.compile(dialect=_NAMED_PG_DIALECT)
    return str(compiled), dict(compiled.params)


async def classify_ingest_url(
    url: str,
    *,
    settings: Settings,
    resolver: DNSResolver | None = None,
    resolve_short_url_fn: ResolveShortUrlFn | None = None,
) -> ParsedURL:
    """Classify ``url`` (resolving plugin short links through ``resolver`` if needed)."""
    if is_short_url(url):
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
    data_provider_profile_id: uuid.UUID | None = None,
    max_products: int | None = None,
    max_reviews: int | None = None,
) -> IngestSubmitResult:
    """Check TTL cache for ``parsed`` and enqueue an Arq ingest job on cache miss."""
    if settings.CRAWL_CACHE_TTL_HOURS > 0:
        cache_stmt = (
            select(Source)
            .where(
                fresh_cache_condition(ttl_hours=settings.CRAWL_CACHE_TTL_HOURS),
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
            crawled_utc = (
                _to_utc(cached_source.crawled_at) if cached_source.crawled_at is not None else None
            )
            refresh_after = compute_refresh_after(crawled_utc, settings.CRAWL_CACHE_TTL_HOURS)
            return IngestSubmitResult(
                cached=True,
                canonical_url=parsed.canonical_url,
                source_id=cached_source.id,
                crawled_at=crawled_utc,
                refresh_after=refresh_after,
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
    enqueue_kwargs: dict[str, Any] = {
        "job_id": job_id,
        "url": parsed.canonical_url,
        "llm_profile_id": str(llm_profile_id) if llm_profile_id is not None else None,
        "max_products": max_products,
        "max_reviews": max_reviews,
    }
    if data_provider_profile_id is not None:
        enqueue_kwargs["data_provider_profile_id"] = str(data_provider_profile_id)
    await job_queue.enqueue_ingest(**enqueue_kwargs)
    return IngestSubmitResult(
        cached=False,
        canonical_url=parsed.canonical_url,
        job_id=job_id,
    )
