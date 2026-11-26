"""``sources.stats['last_job_id']`` — how a running source keeps a live SSE handle.

The source page of the Phase 5 web app opens ``GET /api/jobs/{id}/events`` for a source
whose status is ``pending/queued/running`` using ``last_job_id`` from the source row. That
id is written once, as soon as the crawl creates the ``Source`` (``on_source_created``),
and must survive later crawl updates that rewrite ``stats``.

Unit scope only: the DB-backed preservation SQL is asserted in
``test_api_sources_list_integration.py``.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

import app.jobs.tasks as tasks_module
from app.crawl.types import CrawlResult, CrawlStats, ProgressEvent
from app.db.models import Source
from app.ingest.pipeline import IngestResult, IngestStats
from app.jobs.tasks import _record_source_last_job_id, run_ingest
from tests.ingest_fakes import RecordingJobEvents

JOB_ID = "job-last-id-1"
SOURCE_ID = uuid.uuid4()
URL = "https://tiki.vn/cua-hang/fahasa"


class _FakeSession:
    def __init__(self, source: Source | None, *, fail_commit: bool = False) -> None:
        self.source = source
        self.fail_commit = fail_commit
        self.commits = 0

    async def get(self, _model: type, pk: uuid.UUID) -> Source | None:
        if self.source is not None and self.source.id == pk:
            return self.source
        return None

    async def commit(self) -> None:
        if self.fail_commit:
            raise RuntimeError("connection lost")
        self.commits += 1


class _FakeSessionFactory:
    """Minimal async-context-manager factory matching ``AsyncSessionMaker``."""

    def __init__(self, session: _FakeSession) -> None:
        self.session = session
        self.opened = 0

    def __call__(self) -> _FakeSessionFactory:
        self.opened += 1
        return self

    async def __aenter__(self) -> _FakeSession:
        return self.session

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


def _source(stats: dict[str, Any] | None = None) -> Source:
    return Source(id=SOURCE_ID, url=URL, stats={} if stats is None else stats)


async def test_last_job_id_is_merged_into_existing_stats() -> None:
    source = _source({"ingest": {"chunks": 4}})
    factory = _FakeSessionFactory(_FakeSession(source))

    await _record_source_last_job_id(SOURCE_ID, JOB_ID, session_factory=factory)

    assert source.stats == {"ingest": {"chunks": 4}, "last_job_id": JOB_ID}
    assert factory.session.commits == 1


async def test_last_job_id_overwrites_previous_job_id() -> None:
    source = _source({"last_job_id": "job-old"})
    factory = _FakeSessionFactory(_FakeSession(source))

    await _record_source_last_job_id(SOURCE_ID, JOB_ID, session_factory=factory)

    assert source.stats["last_job_id"] == JOB_ID


async def test_missing_source_is_ignored_without_commit() -> None:
    factory = _FakeSessionFactory(_FakeSession(None))

    await _record_source_last_job_id(SOURCE_ID, JOB_ID, session_factory=factory)

    assert factory.session.commits == 0


async def test_commit_failure_is_logged_and_swallowed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    source = _source()
    factory = _FakeSessionFactory(_FakeSession(source, fail_commit=True))

    await _record_source_last_job_id(SOURCE_ID, JOB_ID, session_factory=factory)

    assert any("Failed recording" in record.message for record in caplog.records)


async def test_falls_back_to_database_singleton_when_factory_not_injected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession(_source())
    factory = _FakeSessionFactory(session)

    class _FakeDatabase:
        session_factory = factory

    monkeypatch.setattr("app.db.database.Database", lambda: _FakeDatabase())

    await _record_source_last_job_id(SOURCE_ID, JOB_ID)

    assert factory.opened == 1
    assert session.commits == 1


async def test_uninitialised_database_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Exploding:
        def __init__(self) -> None:
            raise RuntimeError("Database has not been initialised")

    monkeypatch.setattr("app.db.database.Database", _Exploding)

    # Must not raise: a job without a DB handle should still be able to run.
    await _record_source_last_job_id(SOURCE_ID, JOB_ID)


async def test_run_ingest_records_last_job_id_when_crawl_creates_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[tuple[uuid.UUID, str]] = []
    factory = _FakeSessionFactory(_FakeSession(_source()))

    async def _fake_record(source_id: uuid.UUID, job_id: str, **_kwargs: Any) -> None:
        recorded.append((source_id, job_id))

    async def _fake_crawl(_url: str, **kwargs: Any) -> CrawlResult:
        on_progress = kwargs.get("on_progress")
        if on_progress is not None:
            await on_progress(ProgressEvent(stage="classify", done=0, total=None, message="x"))
        on_source_created = kwargs.get("on_source_created")
        if on_source_created is not None:
            await on_source_created(SOURCE_ID)
        return CrawlResult(
            source_id=SOURCE_ID,
            status="done",
            stats=CrawlStats(products_total=1, products_succeeded=1, reviews_count=1),
            error=None,
        )

    async def _fake_ingest(_source_id: uuid.UUID, **_kwargs: Any) -> IngestResult:
        return IngestResult(
            source_id=SOURCE_ID,
            status="done",
            product_ids=[uuid.uuid4()],
            stats=IngestStats(
                products_total=1, products_ingested=1, chunks_total=1, chunks_embedded=1
            ),
            error=None,
        )

    monkeypatch.setattr(tasks_module, "crawl_url", _fake_crawl)
    monkeypatch.setattr(tasks_module, "ingest_source", _fake_ingest)
    monkeypatch.setattr(tasks_module, "_record_source_last_job_id", _fake_record)

    result = await run_ingest(
        {"job_events": RecordingJobEvents(), "session_factory": factory},
        JOB_ID,
        URL,
    )

    assert result["status"] == "done"
    assert recorded == [(SOURCE_ID, JOB_ID)]


async def test_run_ingest_survives_last_job_id_write_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_crawl(_url: str, **kwargs: Any) -> CrawlResult:
        on_source_created = kwargs.get("on_source_created")
        if on_source_created is not None:
            await on_source_created(SOURCE_ID)
        return CrawlResult(
            source_id=SOURCE_ID,
            status="done",
            stats=CrawlStats(products_total=0, products_succeeded=0, reviews_count=0),
            error=None,
        )

    async def _fake_ingest(_source_id: uuid.UUID, **_kwargs: Any) -> IngestResult:
        return IngestResult(
            source_id=SOURCE_ID,
            status="partial",
            product_ids=[],
            stats=IngestStats(
                products_total=0, products_ingested=0, chunks_total=0, chunks_embedded=0
            ),
            error="no_products",
        )

    monkeypatch.setattr(tasks_module, "crawl_url", _fake_crawl)
    monkeypatch.setattr(tasks_module, "ingest_source", _fake_ingest)

    factory = _FakeSessionFactory(_FakeSession(_source(), fail_commit=True))

    result = await run_ingest(
        {"job_events": RecordingJobEvents(), "session_factory": factory},
        JOB_ID,
        URL,
    )

    # A failed bookkeeping write must not fail the ingest job itself.
    assert result["status"] == "partial"
