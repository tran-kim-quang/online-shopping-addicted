"""Unit tests for the Phase 4 A0 fixes and the phase-4 follow-ups (B1).

Covers F1 (heartbeat / stale detection / worker_timeout / stream cap / source failure),
F3 (extra captcha keys), G1 (terminal-job guards: skip, CAS, cancel on heartbeat) and
G2 (``on_source_created`` wiring in ``crawl_url``). F4/F5 and the F2 cosine threshold need
Postgres and live in ``test_agent_tools_integration.py`` / ``test_jobs_phase4_integration.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.core.errors import JobCancelledError
from app.crawl.service import crawl_url
from app.crawl.types import CrawlResult, CrawlStats, ProgressEvent
from app.db.models import Source
from app.ingest.pipeline import IngestResult, IngestStats
from app.jobs.events import TERMINAL_JOB_STATUSES, JobEvents
from app.jobs.tasks import run_ingest
from tests.crawl_fakes import (
    DummySessionFactory,
    FakeAdapter,
    RepositorySpy,
    make_product,
    make_shop,
)
from tests.ingest_fakes import FakeRedis, RecordingJobEvents

JOB_ID = "job-phase4-1"
URL = "https://tiki.vn/san-pham-p1001.html"
SOURCE_ID = uuid.uuid4()
BASE_TIME = datetime(2026, 11, 20, 8, 0, tzinfo=UTC)


class HeartbeatEvents(RecordingJobEvents):
    """``RecordingJobEvents`` plus a counting/raising ``touch_heartbeat``."""

    def __init__(self, *, raise_after: int | None = None) -> None:
        super().__init__()
        self.heartbeats = 0
        self.raise_after = raise_after
        self.stopped_at: int | None = None

    async def touch_heartbeat(self, job_id: str) -> bool:
        self.heartbeats += 1
        if self.raise_after is not None and self.heartbeats > self.raise_after:
            self.stopped_at = self.heartbeats
            # Simulate ``mark_stale`` having already flipped the Redis hash to failed.
            self._status.setdefault(job_id, {})["status"] = "failed"
            self._status[job_id]["error"] = "worker_timeout: expired"
            raise JobCancelledError(job_id, status="failed", error="worker_timeout: expired")
        return True


def crawl_result(**kwargs: Any) -> CrawlResult:
    data: dict[str, Any] = {
        "source_id": SOURCE_ID,
        "status": "done",
        "stats": CrawlStats(products_total=1, products_succeeded=1, reviews_count=0),
    }
    data.update(kwargs)
    return CrawlResult(**data)


def ingest_result(**kwargs: Any) -> IngestResult:
    data: dict[str, Any] = {
        "source_id": SOURCE_ID,
        "status": "done",
        "product_ids": [uuid.uuid4()],
        "stats": IngestStats(products_total=1, products_ingested=1, chunks_total=2),
    }
    data.update(kwargs)
    return IngestResult(**data)


def patch_pipeline(
    monkeypatch: Any,
    *,
    crawl_delay: float = 0.0,
    ingest_delay: float = 0.0,
) -> dict[str, Any]:
    calls: dict[str, Any] = {"crawl": [], "ingest": []}

    async def _crawl(url: str, **kwargs: Any) -> CrawlResult:
        calls["crawl"].append({"url": url, **kwargs})
        on_source_created = kwargs.get("on_source_created")
        if on_source_created is not None:
            await on_source_created(SOURCE_ID)
        if crawl_delay:
            await asyncio.sleep(crawl_delay)
        return crawl_result()

    async def _ingest(source_id: Any, **kwargs: Any) -> IngestResult:
        calls["ingest"].append({"source_id": source_id, **kwargs})
        if ingest_delay:
            await asyncio.sleep(ingest_delay)
        return ingest_result()

    import app.jobs.tasks as tasks_module

    monkeypatch.setattr(tasks_module, "crawl_url", _crawl)
    monkeypatch.setattr(tasks_module, "ingest_source", _ingest)
    return calls


# ---------------------------------------------------------------------------
# F1 — heartbeat loop
# ---------------------------------------------------------------------------


async def test_heartbeat_runs_periodically_and_stops_after_completion(
    monkeypatch: Any,
) -> None:
    from tests.agent_fakes import make_settings

    patch_pipeline(monkeypatch, crawl_delay=0.18)
    events = HeartbeatEvents()

    result = await run_ingest(
        {
            "job_events": events,
            "settings": make_settings(JOB_HEARTBEAT_INTERVAL_SECONDS=0.05),
        },
        JOB_ID,
        URL,
    )

    assert result["status"] == "done"
    assert events.heartbeats >= 2, "heartbeat must tick while the job runs"
    fired = events.heartbeats
    await asyncio.sleep(0.15)
    assert events.heartbeats == fired, "heartbeat task must be cancelled in finally"


async def test_heartbeat_cancels_work_when_the_job_turns_terminal(monkeypatch: Any) -> None:
    from tests.agent_fakes import make_settings

    patch_pipeline(monkeypatch, crawl_delay=5.0)
    events = HeartbeatEvents(raise_after=1)

    started = asyncio.get_running_loop().time()
    result = await run_ingest(
        {
            "job_events": events,
            "settings": make_settings(JOB_HEARTBEAT_INTERVAL_SECONDS=0.05),
        },
        JOB_ID,
        URL,
    )
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 2.0, "a cancelled job must not wait for the crawl to finish"
    assert result["status"] == "failed"
    assert result["error"] == "worker_timeout: expired"
    assert events.stopped_at == 2
    # The worker must not write a terminal status of its own after the heartbeat detected one.
    assert {status["status"] for status in events.statuses} == {"running"}


async def test_run_ingest_reports_cancellation_without_touching_terminal_status(
    monkeypatch: Any,
) -> None:
    from tests.agent_fakes import make_settings

    class RejectingEvents(HeartbeatEvents):
        async def set_status(self, job_id: str, status: str, **kwargs: Any) -> bool:  # type: ignore[override]
            if status in TERMINAL_JOB_STATUSES and self.statuses:
                # Simulate mark_stale winning the race just before the worker finishes.
                raise JobCancelledError(job_id, status="failed", error="worker_timeout: expired")
            return await super().set_status(job_id, status, **kwargs)

    patch_pipeline(monkeypatch)
    events = RejectingEvents()

    result = await run_ingest(
        {
            "job_events": events,
            "settings": make_settings(JOB_HEARTBEAT_INTERVAL_SECONDS=5),
        },
        JOB_ID,
        URL,
    )

    assert result["error"] == "worker_timeout: expired"
    assert "done" not in [status["status"] for status in events.statuses]
    # The worker keeps whatever status Redis holds; it never re-writes the terminal one.
    assert {status["status"] for status in events.statuses} == {"running"}


# ---------------------------------------------------------------------------
# F1/G1 — JobEvents CAS, terminal guards, stale detection
# ---------------------------------------------------------------------------


async def test_set_status_never_moves_a_terminal_job_back_to_non_terminal() -> None:
    events = JobEvents(FakeRedis())  # type: ignore[arg-type]
    await events.create_job(JOB_ID, source_id=str(SOURCE_ID))
    assert await events.set_status(JOB_ID, "done") is True

    assert await events.set_status(JOB_ID, "running") is False

    status = await events.get_status(JOB_ID)
    assert status is not None
    assert status["status"] == "done"


async def test_set_status_records_source_id_and_error() -> None:
    events = JobEvents(FakeRedis())  # type: ignore[arg-type]
    await events.create_job(JOB_ID)

    assert await events.set_status(JOB_ID, "running", source_id=str(SOURCE_ID)) is True
    status = await events.get_status(JOB_ID)
    assert status is not None
    assert status["source_id"] == str(SOURCE_ID)
    assert status["heartbeat_at"] == status["updated_at"]


async def test_emit_on_terminal_job_raises_cancellation() -> None:
    events = JobEvents(FakeRedis())  # type: ignore[arg-type]
    await events.create_job(JOB_ID)
    await events.set_status(JOB_ID, "failed", error="worker_timeout: gone")

    with pytest.raises(JobCancelledError) as excinfo:
        await events.emit(JOB_ID, stage="crawl_done")

    assert excinfo.value.status == "failed"
    assert excinfo.value.error == "worker_timeout: gone"
    # No event leaked into the log.
    assert await events.list_events(JOB_ID) == []


async def test_emit_with_matching_terminal_stage_is_allowed() -> None:
    events = JobEvents(FakeRedis())  # type: ignore[arg-type]
    await events.create_job(JOB_ID)
    await events.set_status(JOB_ID, "done")

    event = await events.emit(JOB_ID, stage="done", message="xong")

    assert event["stage"] == "done"
    assert len(await events.list_events(JOB_ID)) == 1


async def test_touch_heartbeat_updates_timestamp_and_rejects_terminal_jobs() -> None:
    now = BASE_TIME
    events = JobEvents(FakeRedis(), now_fn=lambda: now)  # type: ignore[arg-type]
    await events.create_job(JOB_ID)
    await events.set_status(JOB_ID, "running")

    now = BASE_TIME + timedelta(seconds=30)
    assert await events.touch_heartbeat(JOB_ID) is True
    status = await events.get_status(JOB_ID)
    assert status is not None
    assert status["heartbeat_at"] == now.isoformat()

    await events.set_status(JOB_ID, "done")
    with pytest.raises(JobCancelledError) as excinfo:
        await events.touch_heartbeat(JOB_ID)
    assert excinfo.value.status == "done"


async def test_touch_heartbeat_returns_false_for_a_missing_job() -> None:
    events = JobEvents(FakeRedis())  # type: ignore[arg-type]

    assert await events.touch_heartbeat("nope") is False


async def _make_running_job(redis: FakeRedis, *, heartbeat_at: datetime) -> JobEvents:
    writer = JobEvents(redis, now_fn=lambda: heartbeat_at)  # type: ignore[arg-type]
    await writer.create_job(JOB_ID, source_id=str(SOURCE_ID))
    await writer.set_status(JOB_ID, "running")
    # Force the heartbeat into the past regardless of clock tricks.
    await redis.hset(
        JobEvents.status_key(JOB_ID), mapping={"heartbeat_at": heartbeat_at.isoformat()}
    )
    return writer


async def test_stream_stale_running_job_ends_with_worker_timeout() -> None:
    redis = FakeRedis()
    await _make_running_job(redis, heartbeat_at=BASE_TIME)
    reader = JobEvents(redis, now_fn=lambda: BASE_TIME + timedelta(seconds=120))  # type: ignore[arg-type]

    events = [
        item
        async for item in reader.stream(
            JOB_ID,
            keepalive_seconds=0.01,
            stale_timeout_seconds=60,
            queued_timeout_seconds=0,
            max_stream_seconds=5,
        )
    ]

    assert events[-1][0] == "error"
    payload = events[-1][1]
    assert payload is not None
    assert payload["status"] == "failed"
    assert "worker_timeout" in (payload["error"] or "")
    status = await reader.get_status(JOB_ID)
    assert status is not None
    assert status["status"] == "failed"


async def test_stream_keeps_a_running_job_with_a_fresh_heartbeat_alive() -> None:
    redis = FakeRedis()
    await _make_running_job(redis, heartbeat_at=BASE_TIME)
    reader = JobEvents(redis, now_fn=lambda: BASE_TIME + timedelta(seconds=5))  # type: ignore[arg-type]

    events = [
        item
        async for item in reader.stream(
            JOB_ID,
            keepalive_seconds=0.01,
            stale_timeout_seconds=60,
            queued_timeout_seconds=600,
            max_stream_seconds=0.05,
        )
    ]

    assert [name for name, _ in events][-1] == "timeout"
    status = await reader.get_status(JOB_ID)
    assert status is not None
    assert status["status"] == "running", "a stream cap must not fail the job"


async def test_stream_marks_a_long_queued_job_stale() -> None:
    redis = FakeRedis()
    writer = JobEvents(redis, now_fn=lambda: BASE_TIME)  # type: ignore[arg-type]
    await writer.create_job(JOB_ID)
    reader = JobEvents(redis, now_fn=lambda: BASE_TIME + timedelta(seconds=700))  # type: ignore[arg-type]

    events = [
        item
        async for item in reader.stream(
            JOB_ID,
            keepalive_seconds=0.01,
            stale_timeout_seconds=0,
            queued_timeout_seconds=600,
            max_stream_seconds=5,
        )
    ]

    assert events[-1][0] == "error"
    payload = events[-1][1]
    assert payload is not None
    assert "stayed queued" in (payload["error"] or "")


async def test_mark_stale_is_idempotent_and_never_overwrites_a_done_job() -> None:
    events = JobEvents(FakeRedis())  # type: ignore[arg-type]
    await events.create_job(JOB_ID)
    await events.set_status(JOB_ID, "done")

    transitioned, latest = await events.mark_stale(JOB_ID)

    assert transitioned is False
    assert latest is not None
    assert latest["status"] == "done"


async def test_mark_stale_marks_the_source_failed_when_the_job_has_one() -> None:
    redis = FakeRedis()
    events = JobEvents(redis)  # type: ignore[arg-type]
    await events.create_job(JOB_ID, source_id="not-a-uuid")

    # Invalid source ids are tolerated (logged, no DB access attempted).
    transitioned, _ = await events.mark_stale(JOB_ID)
    assert transitioned is True
    assert (await events.get_status(JOB_ID))["status"] == "failed"  # type: ignore[index]

    source = Source(id=uuid.uuid4(), url=URL, status="running", stats={}, external_ref={})
    events_with_db = JobEvents(redis, session_factory=_SourceSessionFactory(source))  # type: ignore[arg-type]
    await events_with_db.create_job("job-2", source_id=str(source.id))

    transitioned, latest = await events_with_db.mark_stale("job-2")

    assert transitioned is True
    assert latest is not None and latest["source_id"] == str(source.id)
    assert source.status == "failed"
    assert "worker_timeout" in (source.error or "")


class _AdapterRegistry:
    """Minimal ``AdapterRegistry`` stand-in returning one scripted adapter."""

    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter

    def get(self, _platform: str) -> Any:
        return self.adapter


class _FakeSession:
    def __init__(self, source: Source) -> None:
        self.source = source

    async def get(self, _model: Any, pk: uuid.UUID) -> Any:
        return self.source if pk == self.source.id else None

    async def commit(self) -> None:
        return None


class _SourceSessionFactory:
    def __init__(self, source: Source) -> None:
        self.source = source

    def __call__(self) -> Any:
        factory = self

        class _Ctx:
            async def __aenter__(self) -> _FakeSession:
                return _FakeSession(factory.source)

            async def __aexit__(self, *_exc: Any) -> None:
                return None

        return _Ctx()


# ---------------------------------------------------------------------------
# G1 — run_ingest skips jobs that are already terminal
# ---------------------------------------------------------------------------


async def test_run_ingest_skips_jobs_already_terminal(monkeypatch: Any) -> None:
    calls = patch_pipeline(monkeypatch)
    events = RecordingJobEvents()
    await events.create_job(JOB_ID, url=URL, source_id=str(SOURCE_ID))
    await events.set_status(JOB_ID, "failed", error="worker_timeout: expired")

    result = await run_ingest({"job_events": events}, JOB_ID, URL)

    assert result["status"] == "failed"
    assert result["error"] == "worker_timeout: expired"
    assert calls["crawl"] == [] and calls["ingest"] == []
    assert [status["status"] for status in events.statuses] == ["failed"]
    assert events.events == [], "a skipped retry must not emit progress events"


@pytest.mark.parametrize("status", ["done", "partial", "failed"])
async def test_run_ingest_skips_every_terminal_status(monkeypatch: Any, status: str) -> None:
    calls = patch_pipeline(monkeypatch)
    events = RecordingJobEvents()
    await events.create_job(JOB_ID)
    await events.set_status(JOB_ID, status)

    result = await run_ingest({"job_events": events}, JOB_ID, URL)

    assert result["status"] == status
    assert calls["crawl"] == []


async def test_run_ingest_records_source_id_as_soon_as_crawl_creates_it(
    monkeypatch: Any,
) -> None:
    calls = patch_pipeline(monkeypatch)
    events = HeartbeatEvents()

    await run_ingest({"job_events": events}, JOB_ID, URL)

    with_source = [s for s in events.statuses if s["source_id"] == str(SOURCE_ID)]
    assert with_source, (
        "G2: source_id must reach the job hash during the crawl, not only at the end"
    )
    assert with_source[0]["status"] == "running"
    assert events.statuses.index(with_source[0]) < len(events.statuses) - 1
    assert calls["crawl"][0]["on_source_created"] is not None


# ---------------------------------------------------------------------------
# G2 — crawl_url notifies right after creating the source row
# ---------------------------------------------------------------------------


async def test_crawl_url_calls_on_source_created_immediately(monkeypatch: Any) -> None:
    import app.crawl.service as service_module

    spy = RepositorySpy()
    spy.install(monkeypatch, service_module)
    adapter = FakeAdapter(shop=make_shop(), default_product=make_product())
    order: list[str] = []

    async def _create_source(_session: Any, **kwargs: Any) -> Any:
        order.append("create_source")
        return await RepositorySpy.create_source(spy, _session, **kwargs)

    monkeypatch.setattr(service_module, "create_source", _create_source)

    async def _on_created(source_id: uuid.UUID) -> None:
        order.append(f"on_source_created:{source_id}")

    result = await crawl_url(
        "https://tiki.vn/cua-hang/shop-1",
        session_factory=DummySessionFactory(),
        registry=_AdapterRegistry(adapter),
        on_source_created=_on_created,
    )

    assert result.status == "done"
    assert order == ["create_source", f"on_source_created:{spy.source_id}"]


async def test_crawl_url_without_callback_still_works(monkeypatch: Any) -> None:
    import app.crawl.service as service_module

    spy = RepositorySpy()
    spy.install(monkeypatch, service_module)
    adapter = FakeAdapter(shop=make_shop(), default_product=make_product())

    result = await crawl_url(
        "https://tiki.vn/cua-hang/shop-1",
        session_factory=DummySessionFactory(),
        registry=_AdapterRegistry(adapter),
    )

    assert result.status == "done"
    assert [call["status"] for call in spy.sources] == ["running"]


async def test_crawl_url_progress_callback_receives_events(monkeypatch: Any) -> None:
    import app.crawl.service as service_module

    spy = RepositorySpy()
    spy.install(monkeypatch, service_module)
    adapter = FakeAdapter(shop=make_shop(), default_product=make_product())
    seen: list[ProgressEvent] = []

    async def _on_progress(event: ProgressEvent) -> None:
        seen.append(event)

    await crawl_url(
        "https://tiki.vn/cua-hang/shop-1",
        session_factory=DummySessionFactory(),
        registry=_AdapterRegistry(adapter),
        on_progress=_on_progress,
    )

    assert seen and seen[-1].stage == "done"


# ---------------------------------------------------------------------------
# F3 — extra captcha keys in ``detect_block``
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _json_response(payload: dict[str, Any]) -> Any:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200, json=payload, headers={"content-type": "application/json"}
        )
    )
    with httpx.Client(transport=transport) as client:
        yield client.get("https://tiki.vn/api/v2/products/1")


@pytest.mark.parametrize(
    "payload",
    [
        {"need_captcha": True},
        {"captcha_url": "https://tiki.vn/captcha"},
        {"verify_url": "/verify"},
        {"is_captcha": True},
        {"challenge": "turnstile"},
        {"NEED_CAPTCHA": True},
        {"error": {"Need_Captcha": True}},
        {"data": {"captcha_url": "https://tiki.vn/c"}},
    ],
)
def test_detect_block_recognizes_new_captcha_keys(payload: dict[str, Any]) -> None:
    from app.crawl.http import detect_block

    with _json_response(payload) as response:
        assert detect_block(response) is not None


def test_detect_block_ignores_regular_payloads() -> None:
    from app.crawl.http import detect_block

    with _json_response({"data": [{"id": 1, "name": "Gạo"}]}) as response:
        assert detect_block(response) is None
