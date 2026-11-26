"""Unit tests for the job status + SSE endpoints (``app.api.jobs``)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.api.jobs import _format_sse_frame, _parse_last_event_id
from app.core.config import Settings
from app.jobs.events import JobEvents
from tests.ingest_fakes import (
    FakeRedis,
    RecordingJobEvents,
    RecordingJobQueue,
    build_ingest_app,
)

SECRET = "vN5y6rM0J9h2xL8qK3pW1zB4cF7dG0tH2sV5mN8wX4Q="
JOB_ID = "job-sse-1"


def make_client(job_events: Any) -> httpx.AsyncClient:
    app = build_ingest_app(
        session=object(),
        settings=Settings(SECRET_KEY=SECRET),
        job_events=job_events,
        job_queue=RecordingJobQueue(),
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )


# ---------------------------------------------------------------------------
# Frame formatting helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 0),
        ("", 0),
        ("5", 5),
        ("  7  ", 7),
        ("0", 0),
        ("-3", 0),
        ("abc", 0),
        ("1.5", 0),
    ],
)
def test_parse_last_event_id(raw: str | None, expected: int) -> None:
    assert _parse_last_event_id(raw) == expected


def test_progress_frame_contains_id_event_and_json_data() -> None:
    frame = _format_sse_frame(
        "progress",
        {"seq": 3, "stage": "sentiment", "message": "đang phân tích", "done": 1, "total": 2},
    )

    assert frame.endswith("\n\n")
    assert "id: 3\n" in frame
    assert "event: progress\n" in frame
    data_line = frame.split("data: ", 1)[1].strip()
    payload = json.loads(data_line)
    assert payload["stage"] == "sentiment"
    assert "đang phân tích" in data_line, "Vietnamese text must not be ASCII-escaped"


def test_terminal_frames_use_done_and_error_event_names() -> None:
    assert "event: done" in _format_sse_frame("done", {"seq": 1, "status": "done"})
    assert "event: error" in _format_sse_frame("error", {"seq": 2, "status": "failed"})


def test_keepalive_frame_is_an_sse_comment() -> None:
    assert _format_sse_frame("keepalive", None) == ": keepalive\n\n"


# ---------------------------------------------------------------------------
# GET /api/jobs/{job_id}
# ---------------------------------------------------------------------------


async def test_job_status_returns_404_for_unknown_jobs() -> None:
    client = make_client(RecordingJobEvents())
    try:
        response = await client.get(f"/api/jobs/{JOB_ID}")
    finally:
        await client.aclose()

    assert response.status_code == 404


async def test_job_status_returns_state_and_last_event() -> None:
    events = RecordingJobEvents()
    await events.create_job(JOB_ID, url="https://tiki.vn/a-p1.html")
    await events.set_status(JOB_ID, "running", source_id="src-1")
    await events.emit(JOB_ID, stage="sentiment", done=1, total=3, message="ok")

    client = make_client(events)
    try:
        response = await client.get(f"/api/jobs/{JOB_ID}")
    finally:
        await client.aclose()

    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == JOB_ID
    assert body["status"] == "running"
    assert body["source_id"] == "src-1"
    assert body["last_event"]["stage"] == "sentiment"
    assert body["last_event"]["total"] == 3


# ---------------------------------------------------------------------------
# GET /api/jobs/{job_id}/events (SSE)
# ---------------------------------------------------------------------------


async def test_sse_stream_replays_events_then_finishes_with_done() -> None:
    redis = FakeRedis()
    events = JobEvents(redis)  # type: ignore[arg-type]
    await events.create_job(JOB_ID)
    await events.emit(JOB_ID, stage="started")
    await events.emit(JOB_ID, stage="sentiment", done=1, total=1)
    await events.set_status(JOB_ID, "done", source_id="src-7")
    await events.emit(JOB_ID, stage="done", done=1, total=1, message="xong")

    client = make_client(events)
    try:
        response = await client.get(f"/api/jobs/{JOB_ID}/events")
    finally:
        await client.aclose()

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    body = response.text
    assert body.count("event: progress") == 3
    assert "event: done" in body
    assert body.index("id: 1") < body.index("id: 2") < body.index("id: 3")


async def test_sse_stream_resumes_from_last_event_id() -> None:
    redis = FakeRedis()
    events = JobEvents(redis)  # type: ignore[arg-type]
    await events.create_job(JOB_ID)
    for idx in range(3):
        await events.emit(JOB_ID, stage=f"stage-{idx}")
    await events.set_status(JOB_ID, "done", source_id="src-7")

    client = make_client(events)
    try:
        response = await client.get(
            f"/api/jobs/{JOB_ID}/events",
            headers={"Last-Event-ID": "2"},
        )
    finally:
        await client.aclose()

    body = response.text
    assert body.count("event: progress") == 1
    assert "stage-2" in body
    assert "stage-0" not in body
    assert "event: done" in body


async def test_sse_stream_ends_with_error_for_failed_jobs() -> None:
    redis = FakeRedis()
    events = JobEvents(redis)  # type: ignore[arg-type]
    await events.create_job(JOB_ID)
    await events.emit(JOB_ID, stage="failed", message="crawl boom")
    await events.set_status(JOB_ID, "failed", error="crawl boom")

    client = make_client(events)
    try:
        response = await client.get(f"/api/jobs/{JOB_ID}/events")
    finally:
        await client.aclose()

    body = response.text
    assert "event: error" in body
    assert "crawl boom" in body
    assert "event: done" not in body


async def test_sse_stream_returns_404_for_unknown_jobs() -> None:
    client = make_client(JobEvents(FakeRedis()))  # type: ignore[arg-type]
    try:
        response = await client.get(f"/api/jobs/{JOB_ID}/events")
    finally:
        await client.aclose()

    assert response.status_code == 404
