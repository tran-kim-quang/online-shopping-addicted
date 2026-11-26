"""Unit tests for ``app.jobs.queue.JobQueue`` (Arq pool stubbed, no Redis)."""

from __future__ import annotations

from typing import Any

import pytest

from app.jobs.queue import JobQueue

JOB_ID = "job-queue-1"
SOURCE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PROFILE_ID = "33333333-3333-4333-8333-333333333333"


class RecordingPool:
    """``arq`` pool double capturing ``enqueue_job`` calls."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    async def enqueue_job(self, function: str, *args: Any, **kwargs: Any) -> Any:
        self.calls.append({"function": function, "args": args, "kwargs": kwargs})
        return {"job_id": kwargs.get("_job_id")}

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def queue(monkeypatch: pytest.MonkeyPatch) -> tuple[JobQueue, RecordingPool]:
    pool = RecordingPool()
    job_queue = JobQueue()

    async def _get_pool() -> RecordingPool:
        return pool

    monkeypatch.setattr(job_queue, "get_pool", _get_pool)
    return job_queue, pool


async def test_enqueue_analyze_sends_the_run_analyze_task(
    queue: tuple[JobQueue, RecordingPool],
) -> None:
    job_queue, pool = queue

    await job_queue.enqueue_analyze(
        job_id=JOB_ID,
        source_id=SOURCE_ID,
        llm_profile_id=PROFILE_ID,
    )

    assert pool.calls == [
        {
            "function": "run_analyze",
            "args": (JOB_ID, SOURCE_ID, PROFILE_ID),
            # Re-using the API job id keeps the SSE stream and the Arq job in sync.
            "kwargs": {"_job_id": JOB_ID},
        }
    ]


async def test_enqueue_ingest_still_sends_the_run_ingest_task(
    queue: tuple[JobQueue, RecordingPool],
) -> None:
    job_queue, pool = queue

    await job_queue.enqueue_ingest(
        job_id=JOB_ID,
        url="https://tiki.vn/a-p1.html",
        llm_profile_id=None,
        max_products=5,
        max_reviews=10,
    )

    call = pool.calls[0]
    assert call["function"] == "run_ingest"
    assert call["args"] == (JOB_ID, "https://tiki.vn/a-p1.html", None, 5, 10)
    assert call["kwargs"]["_job_id"] == JOB_ID
    assert "force" not in call["kwargs"]


async def test_close_releases_the_pool_once(
    queue: tuple[JobQueue, RecordingPool],
) -> None:
    job_queue, pool = queue
    job_queue._pool = pool  # type: ignore[assignment]

    await job_queue.close()
    await job_queue.close()

    assert pool.closed is True
