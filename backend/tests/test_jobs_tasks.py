"""Unit tests for the Arq task ``app.jobs.tasks.run_ingest`` (crawl + ingest stubbed).

The real end-to-end run (fake adapter + FakeEmbedder + Postgres) lives in
``test_ingest_integration.py``.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

import app.jobs.tasks as tasks_module
from app.crawl.types import CrawlResult, CrawlStats, ProgressEvent
from app.ingest.pipeline import IngestResult, IngestStats
from app.jobs.tasks import _parse_profile_id, run_ingest
from tests.ingest_fakes import RecordingJobEvents

JOB_ID = "job-unit-1"
URL = "https://tiki.vn/san-pham-p1001.html"
SOURCE_ID = uuid.uuid4()


def crawl_result(status: str = "done", *, source_id: Any = SOURCE_ID, error: str | None = None):
    return CrawlResult(
        source_id=source_id,
        status=status,  # type: ignore[arg-type]
        stats=CrawlStats(products_total=1, products_succeeded=1, reviews_count=3),
        error=error,
    )


def ingest_result(status: str = "done", *, error: str | None = None, stats: Any = None):
    return IngestResult(
        source_id=SOURCE_ID,
        status=status,  # type: ignore[arg-type]
        product_ids=[uuid.uuid4()],
        stats=stats
        or IngestStats(products_total=1, products_ingested=1, chunks_total=4, chunks_embedded=4),
        error=error,
    )


@pytest.fixture
def events() -> RecordingJobEvents:
    return RecordingJobEvents()


def patch_crawl(
    monkeypatch: Any, result: Any, *, stages: list[str] | None = None
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def _fake_crawl(url: str, **kwargs: Any) -> Any:
        calls.append({"url": url, **kwargs})
        on_progress = kwargs.get("on_progress")
        for stage in stages or ["classify", "product", "done"]:
            if on_progress is not None:
                await on_progress(ProgressEvent(stage=stage, done=1, total=1, message=stage))
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(tasks_module, "crawl_url", _fake_crawl)
    return calls


def patch_ingest(
    monkeypatch: Any,
    result: Any,
    *,
    stages: list[str] | None = None,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def _fake_ingest(source_id: Any, **kwargs: Any) -> Any:
        calls.append({"source_id": source_id, **kwargs})
        on_progress = kwargs.get("on_progress")
        for stage in stages or ["ingest_start", "sentiment", "embed"]:
            if on_progress is not None:
                await on_progress(ProgressEvent(stage=stage, done=1, total=1, message=stage))
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(tasks_module, "ingest_source", _fake_ingest)
    return calls


async def test_run_ingest_happy_path_emits_progress_and_finishes_done(
    monkeypatch: Any,
    events: RecordingJobEvents,
) -> None:
    crawl_calls = patch_crawl(monkeypatch, crawl_result())
    ingest_calls = patch_ingest(monkeypatch, ingest_result())

    result = await run_ingest({"job_events": events}, JOB_ID, URL)

    assert result["status"] == "done"
    assert result["source_id"] == str(SOURCE_ID)
    assert result["error"] is None
    assert crawl_calls[0]["url"] == URL
    assert ingest_calls[0]["source_id"] == SOURCE_ID

    assert events.stages == [
        "started",
        "classify",
        "product",
        "crawl_done",  # crawl's terminal "done" must not close the SSE stream
        "ingest_start",
        "sentiment",
        "embed",
        "done",
    ]
    assert [status["status"] for status in events.statuses] == ["running", "running", "done"]
    assert events.statuses[1]["source_id"] == str(SOURCE_ID)


async def test_run_ingest_remaps_crawl_failed_stage(
    monkeypatch: Any,
    events: RecordingJobEvents,
) -> None:
    patch_crawl(monkeypatch, crawl_result(status="failed", error="blocked"), stages=["failed"])
    patch_ingest(monkeypatch, ingest_result())

    result = await run_ingest({"job_events": events}, JOB_ID, URL)

    assert result["status"] == "failed"
    assert "crawl_failed" in events.stages
    assert events.stages[-1] == "failed"
    assert events.statuses[-1]["status"] == "failed"
    assert events.statuses[-1]["error"] == "blocked"


async def test_run_ingest_does_not_ingest_when_crawl_fails(
    monkeypatch: Any,
    events: RecordingJobEvents,
) -> None:
    patch_crawl(monkeypatch, crawl_result(status="failed", error="boom"))
    ingest_calls = patch_ingest(monkeypatch, ingest_result())

    result = await run_ingest({"job_events": events}, JOB_ID, URL)

    assert result["status"] == "failed"
    assert ingest_calls == []


async def test_run_ingest_fails_when_crawl_returns_no_source(
    monkeypatch: Any,
    events: RecordingJobEvents,
) -> None:
    patch_crawl(monkeypatch, crawl_result(status="done", source_id=None))
    ingest_calls = patch_ingest(monkeypatch, ingest_result())

    result = await run_ingest({"job_events": events}, JOB_ID, URL)

    assert result["status"] == "failed"
    assert result["error"] == "Crawl failed."
    assert ingest_calls == []


async def test_run_ingest_propagates_partial_ingest_status(
    monkeypatch: Any,
    events: RecordingJobEvents,
) -> None:
    patch_crawl(monkeypatch, crawl_result())
    patch_ingest(
        monkeypatch,
        ingest_result(status="partial", error="1/2 product(s) failed ingestion."),
    )

    result = await run_ingest({"job_events": events}, JOB_ID, URL)

    assert result["status"] == "partial"
    assert result["error"] == "1/2 product(s) failed ingestion."
    assert events.statuses[-1]["status"] == "partial"
    assert events.stages[-1] == "partial"
    assert "chunks=4" in events.events[-1]["message"]


async def test_run_ingest_catches_unexpected_errors(
    monkeypatch: Any,
    events: RecordingJobEvents,
) -> None:
    patch_crawl(monkeypatch, RuntimeError("kaput"))

    result = await run_ingest({"job_events": events}, JOB_ID, URL)

    assert result["status"] == "failed"
    assert result["error"] == "kaput"
    assert events.statuses[-1]["status"] == "failed"
    assert events.stages[-1] == "failed"


async def test_run_ingest_isolates_event_publishing_failures(monkeypatch: Any) -> None:
    class BrokenEvents(RecordingJobEvents):
        async def set_status(self, *_args: Any, **_kwargs: Any) -> None:
            raise ConnectionError("redis down")

        async def emit(self, *_args: Any, **_kwargs: Any) -> Any:
            raise ConnectionError("redis down")

    patch_crawl(monkeypatch, crawl_result())
    patch_ingest(monkeypatch, ingest_result())

    result = await run_ingest({"job_events": BrokenEvents()}, JOB_ID, URL)

    assert result["status"] == "failed"
    assert "redis down" in (result["error"] or "")


async def test_run_ingest_forwards_limits_and_parses_profile_id(
    monkeypatch: Any,
    events: RecordingJobEvents,
) -> None:
    crawl_calls = patch_crawl(monkeypatch, crawl_result())
    ingest_calls = patch_ingest(monkeypatch, ingest_result())
    profile_id = uuid.uuid4()

    await run_ingest(
        {"job_events": events},
        JOB_ID,
        URL,
        llm_profile_id=str(profile_id),
        max_products=5,
        max_reviews=10,
    )

    assert crawl_calls[0]["max_products"] == 5
    assert crawl_calls[0]["max_reviews"] == 10
    assert ingest_calls[0]["llm_profile_id"] == profile_id


async def test_run_ingest_ignores_legacy_force_kwarg_from_a_queued_old_job(
    monkeypatch: Any,
    events: RecordingJobEvents,
    caplog: Any,
) -> None:
    """A job enqueued before the deploy still carries ``force=True`` in its Arq kwargs.

    Removing the parameter would make the new worker raise ``TypeError`` for that
    in-flight job, so unexpected kwargs must be tolerated (logged, then dropped).
    """
    crawl_calls = patch_crawl(monkeypatch, crawl_result())
    patch_ingest(monkeypatch, ingest_result())

    with caplog.at_level("WARNING", logger="app.jobs.tasks"):
        result = await run_ingest({"job_events": events}, JOB_ID, URL, force=True)

    assert result["status"] == "done"
    assert crawl_calls, "the job must still crawl despite the unknown kwarg"
    assert any("force" in record.getMessage() for record in caplog.records)


def test_cache_bypass_is_gone_from_the_public_job_api() -> None:
    """Neither the task nor the queue exposes ``force`` any more (only tolerate legacy runs)."""
    import inspect

    from app.jobs.queue import JobQueue

    assert "force" not in inspect.signature(run_ingest).parameters
    assert "force" not in inspect.signature(JobQueue.enqueue_ingest).parameters


async def test_run_ingest_rejects_malformed_profile_id(
    monkeypatch: Any,
    events: RecordingJobEvents,
) -> None:
    patch_crawl(monkeypatch, crawl_result())
    patch_ingest(monkeypatch, ingest_result())

    result = await run_ingest({"job_events": events}, JOB_ID, URL, llm_profile_id="not-a-uuid")

    assert result["status"] == "failed"
    assert events.statuses[-1]["status"] == "failed"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        (
            uuid.UUID("11111111-1111-1111-1111-111111111111"),
            uuid.UUID("11111111-1111-1111-1111-111111111111"),
        ),
        ("11111111-1111-1111-1111-111111111111", uuid.UUID("11111111-1111-1111-1111-111111111111")),
    ],
)
def test_parse_profile_id(raw: Any, expected: Any) -> None:
    assert _parse_profile_id(raw) == expected
