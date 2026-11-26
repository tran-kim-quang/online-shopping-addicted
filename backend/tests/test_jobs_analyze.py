"""Unit tests for the Arq task ``app.jobs.tasks.run_analyze`` (pipeline stubbed).

The DB side of the same code path (real reviews, real ``review_summaries``, partial batches)
lives in ``test_analyze_integration.py``. Here we lock the contract of the task itself:

* it never crawls and never embeds (an analyze job must not cost marketplace requests),
* it reuses the ingest job lifecycle (heartbeat, terminal CAS, cancellation),
* it always releases the per-source Redis analyze lock, including on failure.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

import app.jobs.tasks as tasks_module
from app.core.errors import JobCancelledError
from app.crawl.types import ProgressEvent
from app.ingest.pipeline import AnalyzeResult, AnalyzeStats
from app.jobs.tasks import run_analyze
from tests.ingest_fakes import RecordingJobEvents

JOB_ID = "job-analyze-1"
SOURCE_ID = uuid.uuid4()
PROFILE_ID = uuid.uuid4()


class BombEmbedder:
    """Embedder double that fails loudly if an analyze job tries to embed anything."""

    name = "bomb"
    dim = 1024

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed(self, texts: Any) -> list[list[float]]:
        self.calls.append(list(texts))
        raise AssertionError("run_analyze must not embed reviews again")


class HeartbeatEvents(RecordingJobEvents):
    """``RecordingJobEvents`` plus a counting/raising ``touch_heartbeat``."""

    def __init__(self, *, raise_after: int | None = None) -> None:
        super().__init__()
        self.heartbeats = 0
        self.raise_after = raise_after

    async def touch_heartbeat(self, job_id: str) -> bool:
        self.heartbeats += 1
        if self.raise_after is not None and self.heartbeats > self.raise_after:
            self._status.setdefault(job_id, {})["status"] = "failed"
            self._status[job_id]["error"] = "worker_timeout: expired"
            raise JobCancelledError(job_id, status="failed", error="worker_timeout: expired")
        return True


def analyze_result(
    status: str = "done",
    *,
    error: str | None = None,
    stats: AnalyzeStats | None = None,
) -> AnalyzeResult:
    return AnalyzeResult(
        source_id=SOURCE_ID,
        status=status,  # type: ignore[arg-type]
        stats=stats
        or AnalyzeStats(
            products_total=2,
            products_processed=2,
            reviews_to_analyze=5,
            reviews_analyzed=5,
            reviews_llm=4,
            reviews_rating_fallback=1,
            summaries_invalidated=2,
        ),
        error=error,
    )


def patch_pipeline(
    monkeypatch: Any,
    result: Any,
    *,
    delay: float = 0.0,
    stages: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Patch the pipeline entrypoint and make any crawl/ingest call explode."""
    calls: list[dict[str, Any]] = []

    async def _fake_analyze(source_id: Any, **kwargs: Any) -> Any:
        calls.append({"source_id": source_id, **kwargs})
        on_progress = kwargs.get("on_progress")
        for stage in stages or ["sentiment"]:
            if on_progress is not None:
                await on_progress(ProgressEvent(stage=stage, done=1, total=5, message=stage))
        if delay:
            await asyncio.sleep(delay)
        if isinstance(result, Exception):
            raise result
        return result

    async def _forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("run_analyze must not crawl or ingest")

    monkeypatch.setattr(tasks_module, "analyze_source", _fake_analyze)
    monkeypatch.setattr(tasks_module, "crawl_url", _forbidden)
    monkeypatch.setattr(tasks_module, "ingest_source", _forbidden)
    return calls


def make_ctx(events: Any, **overrides: Any) -> dict[str, Any]:
    ctx: dict[str, Any] = {"job_events": events}
    ctx.update(overrides)
    return ctx


async def test_run_analyze_emits_progress_and_finishes_done(
    monkeypatch: Any,
) -> None:
    events = RecordingJobEvents()
    calls = patch_pipeline(monkeypatch, analyze_result())

    result = await run_analyze(make_ctx(events), JOB_ID, str(SOURCE_ID), str(PROFILE_ID))

    assert result == {
        "job_id": JOB_ID,
        "status": "done",
        "source_id": str(SOURCE_ID),
        "error": None,
    }
    # Only the analyze step runs: started → sentiment → done (never classify/crawl/embed).
    assert events.stages == ["started", "sentiment", "done"]
    assert not {"classify", "crawl_done", "embed", "index"} & set(events.stages)
    assert [status["status"] for status in events.statuses] == ["running", "done"]
    assert events.statuses[0]["source_id"] == str(SOURCE_ID)
    # The final event reports reviews, not products/chunks.
    final = events.events[-1]
    assert final["done"] == 4 and final["total"] == 5
    assert "llm=4/5" in final["message"]
    assert "summaries_invalidated=2" in final["message"]

    assert len(calls) == 1
    assert calls[0]["source_id"] == SOURCE_ID
    assert calls[0]["llm_profile_id"] == PROFILE_ID
    assert "embedder" not in calls[0], "run_analyze must not pass an embedder"
    assert "registry" not in calls[0], "run_analyze must not pass the adapter registry"


async def test_run_analyze_forwards_injected_dependencies(
    monkeypatch: Any,
) -> None:
    events = RecordingJobEvents()
    calls = patch_pipeline(monkeypatch, analyze_result())
    session_factory = object()
    llm_client = object()
    secret_box = object()
    settings = object()

    await run_analyze(
        make_ctx(
            events,
            session_factory=session_factory,
            llm_client=llm_client,
            secret_box=secret_box,
            settings=settings,
        ),
        JOB_ID,
        SOURCE_ID,
        PROFILE_ID,
    )

    assert calls[0]["session_factory"] is session_factory
    assert calls[0]["llm_client"] is llm_client
    assert calls[0]["secret_box"] is secret_box
    assert calls[0]["settings"] is settings
    assert calls[0]["on_progress"] is not None


async def test_run_analyze_partial_status_is_kept(monkeypatch: Any) -> None:
    events = RecordingJobEvents()
    patch_pipeline(
        monkeypatch,
        analyze_result(
            status="partial",
            error="1/2 LLM batch(es) failed; kept star-rating fallback.",
        ),
    )

    result = await run_analyze(make_ctx(events), JOB_ID, SOURCE_ID, PROFILE_ID)

    assert result["status"] == "partial"
    assert "star-rating fallback" in (result["error"] or "")
    assert events.statuses[-1]["status"] == "partial"
    assert events.stages[-1] == "partial"


async def test_run_analyze_releases_the_source_lock_on_success(monkeypatch: Any) -> None:
    events = RecordingJobEvents()
    patch_pipeline(monkeypatch, analyze_result())

    await run_analyze(make_ctx(events), JOB_ID, SOURCE_ID, PROFILE_ID)

    assert events.released_locks == [(str(SOURCE_ID), JOB_ID)]


async def test_run_analyze_releases_the_source_lock_when_the_pipeline_raises(
    monkeypatch: Any,
) -> None:
    events = RecordingJobEvents()
    patch_pipeline(monkeypatch, RuntimeError("llm profile vanished"))

    result = await run_analyze(make_ctx(events), JOB_ID, SOURCE_ID, PROFILE_ID)

    assert result["status"] == "failed"
    assert "llm profile vanished" in (result["error"] or "")
    assert events.statuses[-1]["status"] == "failed"
    # Without this release the source stays locked for the whole TTL (30 min).
    assert events.released_locks == [(str(SOURCE_ID), JOB_ID)]


async def test_run_analyze_releases_the_lock_when_the_job_was_already_terminal(
    monkeypatch: Any,
) -> None:
    """A retried job that already finished must return early and free the lock."""
    events = RecordingJobEvents()
    await events.set_status(JOB_ID, "done", source_id=str(SOURCE_ID))
    calls = patch_pipeline(monkeypatch, analyze_result())

    result = await run_analyze(make_ctx(events), JOB_ID, SOURCE_ID, PROFILE_ID)

    assert result["status"] == "done"
    assert calls == [], "a terminal job must not re-run the analysis"
    assert events.released_locks == [(str(SOURCE_ID), JOB_ID)]


async def test_run_analyze_propagates_cancellation_without_writing_terminal_status(
    monkeypatch: Any,
) -> None:
    from tests.agent_fakes import make_settings

    events = HeartbeatEvents(raise_after=1)
    patch_pipeline(monkeypatch, analyze_result(), delay=5.0)

    loop = asyncio.get_running_loop()
    started = loop.time()
    result = await run_analyze(
        make_ctx(events, settings=make_settings(JOB_HEARTBEAT_INTERVAL_SECONDS=0.05)),
        JOB_ID,
        SOURCE_ID,
        PROFILE_ID,
    )
    elapsed = loop.time() - started

    assert elapsed < 2.0, "a cancelled analyze job must stop promptly"
    assert result["status"] == "failed"
    assert result["error"] == "worker_timeout: expired"
    # mark_stale already wrote the terminal status: the worker must not write another one.
    assert {status["status"] for status in events.statuses} == {"running"}
    assert events.released_locks == [(str(SOURCE_ID), JOB_ID)]


async def test_run_analyze_heartbeat_ticks_and_stops_after_completion(
    monkeypatch: Any,
) -> None:
    from tests.agent_fakes import make_settings

    events = HeartbeatEvents()
    patch_pipeline(monkeypatch, analyze_result(), delay=0.18)

    await run_analyze(
        make_ctx(events, settings=make_settings(JOB_HEARTBEAT_INTERVAL_SECONDS=0.05)),
        JOB_ID,
        SOURCE_ID,
        PROFILE_ID,
    )

    assert events.heartbeats >= 2
    fired = events.heartbeats
    await asyncio.sleep(0.15)
    assert events.heartbeats == fired, "heartbeat task must be cancelled in finally"


async def test_run_analyze_requires_a_profile_id(monkeypatch: Any) -> None:
    """A job serialised without a profile id fails before touching the pipeline.

    The API never enqueues such a job (``AnalyzeSourceRequest.llm_profile_id`` is a required
    UUID and the profile row is checked first), so this is a defensive guard: the task raises
    instead of running an analysis with the rating fallback. The per-source lock is therefore
    not released here — it expires with its TTL (see report §6.2).
    """
    events = RecordingJobEvents()
    calls = patch_pipeline(monkeypatch, analyze_result())

    with pytest.raises(ValueError, match="llm_profile_id is required"):
        await run_analyze(make_ctx(events), JOB_ID, SOURCE_ID, "")

    assert calls == []
    assert events.statuses == [], "the lifecycle must not start without a profile"


@pytest.mark.parametrize("as_uuid", [True, False])
async def test_run_analyze_accepts_uuid_or_string_ids(monkeypatch: Any, as_uuid: bool) -> None:
    events = RecordingJobEvents()
    calls = patch_pipeline(monkeypatch, analyze_result())
    source_arg: Any = SOURCE_ID if as_uuid else str(SOURCE_ID)

    await run_analyze(make_ctx(events), JOB_ID, source_arg, str(PROFILE_ID))

    assert calls[0]["source_id"] == SOURCE_ID
    assert isinstance(calls[0]["source_id"], uuid.UUID)
