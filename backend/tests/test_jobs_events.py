"""Unit tests for ``app.jobs.events`` using an in-memory Redis double."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.jobs.events import JOB_TTL_SECONDS, JobEvents
from tests.ingest_fakes import FakeRedis

JOB_ID = "job-123"


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def events(redis: FakeRedis) -> JobEvents:
    return JobEvents(redis, ttl_seconds=JOB_TTL_SECONDS)  # type: ignore[arg-type]


async def collect(agen: Any, count: int) -> list[tuple[str, Any]]:
    """Pull ``count`` items out of an async generator, then close it."""
    items: list[tuple[str, Any]] = []
    try:
        for _ in range(count):
            items.append(await asyncio.wait_for(agen.__anext__(), timeout=2))
    finally:
        await agen.aclose()
    return items


# ---------------------------------------------------------------------------
# Key schema + status hash
# ---------------------------------------------------------------------------


def test_redis_keys_follow_the_documented_schema() -> None:
    assert JobEvents.status_key("abc") == "job:abc"
    assert JobEvents.events_key("abc") == "job:abc:events"
    assert JobEvents.seq_key("abc") == "job:abc:seq"
    assert JobEvents.channel_name("abc") == "job:abc"


async def test_create_job_initialises_a_queued_status_hash(
    events: JobEvents,
    redis: FakeRedis,
) -> None:
    await events.create_job(JOB_ID, url="https://tiki.vn/a-p1.html")

    status = await events.get_status(JOB_ID)
    assert status is not None
    assert status["status"] == "queued"
    assert status["url"] == "https://tiki.vn/a-p1.html"
    assert status["source_id"] is None
    assert status["error"] is None
    assert redis.expiry["job:job-123"] == JOB_TTL_SECONDS


async def test_get_status_returns_none_for_unknown_jobs(events: JobEvents) -> None:
    assert await events.get_status("missing") is None


async def test_set_status_updates_fields_and_refreshes_ttl(
    events: JobEvents,
    redis: FakeRedis,
) -> None:
    await events.create_job(JOB_ID)
    await events.set_status(JOB_ID, "running", source_id="src-1")
    await events.set_status(JOB_ID, "failed", error="boom")

    status = await events.get_status(JOB_ID)
    assert status is not None
    assert status["status"] == "failed"
    assert status["source_id"] == "src-1"
    assert status["error"] == "boom"
    assert redis.expiry["job:job-123"] == JOB_TTL_SECONDS


# ---------------------------------------------------------------------------
# Event log
# ---------------------------------------------------------------------------


async def test_emit_rpushes_and_publishes_the_same_payload(
    events: JobEvents,
    redis: FakeRedis,
) -> None:
    await events.create_job(JOB_ID)

    event = await events.emit(
        JOB_ID,
        stage="sentiment",
        done=2,
        total=5,
        message="đang phân tích",
    )

    stored = redis.lists["job:job-123:events"]
    assert len(stored) == 1
    assert redis.published == [("job:job-123", stored[0])]
    assert event["seq"] == 1
    assert event["stage"] == "sentiment"
    assert event["done"] == 2 and event["total"] == 5
    assert event["message"] == "đang phân tích"
    assert "ts" in event
    assert redis.expiry["job:job-123:events"] == JOB_TTL_SECONDS
    assert redis.expiry["job:job-123:seq"] == JOB_TTL_SECONDS


async def test_emit_allocates_monotonic_sequence_numbers(events: JobEvents) -> None:
    seqs = []
    for idx in range(4):
        event = await events.emit(JOB_ID, stage=f"stage-{idx}")
        seqs.append(event["seq"])

    assert seqs == [1, 2, 3, 4]


async def test_list_events_filters_by_after_seq(events: JobEvents) -> None:
    for idx in range(3):
        await events.emit(JOB_ID, stage=f"stage-{idx}")

    all_events = await events.list_events(JOB_ID)
    resumed = await events.list_events(JOB_ID, after_seq=2)

    assert [event["stage"] for event in all_events] == ["stage-0", "stage-1", "stage-2"]
    assert [event["stage"] for event in resumed] == ["stage-2"]


async def test_list_events_skips_corrupt_entries(
    events: JobEvents,
    redis: FakeRedis,
) -> None:
    await events.emit(JOB_ID, stage="ok")
    redis.lists["job:job-123:events"].append("{not json")

    events_read = await events.list_events(JOB_ID)

    assert [event["stage"] for event in events_read] == ["ok"]


async def test_last_event_returns_the_newest_event(events: JobEvents) -> None:
    assert await events.last_event(JOB_ID) is None
    await events.emit(JOB_ID, stage="first")
    await events.emit(JOB_ID, stage="second")

    last = await events.last_event(JOB_ID)

    assert last is not None and last["stage"] == "second"


# ---------------------------------------------------------------------------
# SSE stream
# ---------------------------------------------------------------------------


async def test_stream_replays_history_then_emits_done(events: JobEvents) -> None:
    await events.create_job(JOB_ID)
    await events.emit(JOB_ID, stage="started")
    await events.emit(JOB_ID, stage="sentiment", done=1, total=1)
    await events.set_status(JOB_ID, "done", source_id="src-1")

    frames = await collect(events.stream(JOB_ID, keepalive_seconds=0.05), count=3)

    assert [name for name, _ in frames] == ["progress", "progress", "done"]
    assert frames[0][1]["stage"] == "started"
    assert frames[2][1]["status"] == "done"
    assert frames[2][1]["source_id"] == "src-1"


async def test_stream_resumes_after_last_event_id(events: JobEvents) -> None:
    await events.create_job(JOB_ID)
    for idx in range(3):
        await events.emit(JOB_ID, stage=f"stage-{idx}")
    await events.set_status(JOB_ID, "done")

    frames = await collect(
        events.stream(JOB_ID, last_event_id=2, keepalive_seconds=0.05),
        count=2,
    )

    assert [name for name, _ in frames] == ["progress", "done"]
    assert frames[0][1]["seq"] == 3


async def test_stream_ends_with_error_event_for_failed_jobs(events: JobEvents) -> None:
    await events.create_job(JOB_ID)
    await events.emit(JOB_ID, stage="failed", message="crawl boom")
    await events.set_status(JOB_ID, "failed", error="crawl boom")

    frames = await collect(events.stream(JOB_ID), count=2)

    assert [name for name, _ in frames] == ["progress", "error"]
    assert frames[1][1]["status"] == "failed"
    assert frames[1][1]["error"] == "crawl boom"


async def test_stream_reports_missing_jobs_as_error(events: JobEvents) -> None:
    frames = await collect(events.stream("missing-job"), count=1)

    assert frames[0][0] == "error"
    assert frames[0][1]["error"] == "Job not found."


async def test_stream_emits_keepalive_while_waiting(events: JobEvents) -> None:
    await events.create_job(JOB_ID)
    await events.set_status(JOB_ID, "running")
    agen = events.stream(JOB_ID, keepalive_seconds=0.01)

    frame = await asyncio.wait_for(agen.__anext__(), timeout=2)
    await agen.aclose()

    assert frame == ("keepalive", None)


async def test_stream_follows_live_pubsub_until_terminal_stage(events: JobEvents) -> None:
    await events.create_job(JOB_ID)
    await events.set_status(JOB_ID, "running")
    agen = events.stream(JOB_ID, keepalive_seconds=0.01)

    async def _publish_soon() -> None:
        await asyncio.sleep(0.02)
        await events.emit(JOB_ID, stage="embed", done=1, total=1)
        await events.set_status(JOB_ID, "done", source_id="src-9")
        await events.emit(JOB_ID, stage="done", done=1, total=1, message="xong")

    publisher = asyncio.create_task(_publish_soon())
    try:
        frames: list[tuple[str, Any]] = []
        while True:
            item = await asyncio.wait_for(agen.__anext__(), timeout=2)
            if item[0] == "keepalive":
                continue
            frames.append(item)
            if item[0] in ("done", "error"):
                break
    finally:
        await agen.aclose()
        await publisher

    assert [name for name, _ in frames] == ["progress", "progress", "done"]
    assert frames[0][1]["stage"] == "embed"
    assert frames[2][1]["status"] == "done"
    assert frames[2][1]["source_id"] == "src-9"


async def test_stream_uses_default_singleton_redis_when_not_injected() -> None:
    # Constructor without a client must not touch Redis until a method is called.
    assert JobEvents()._redis is None
