"""Unit tests for ``POST /api/sources/{id}/analyze`` (in-process, no Postgres).

The SQL/lock semantics against a real database and a real Redis live in
``test_analyze_integration.py``; here the session and the LLM are never touched, so these
tests are fully offline (``_block_real_network`` still applies).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.api.sources import AnalyzeSourceRequest
from app.core.config import Settings
from app.db.models import LLMProfile, Product, Review, Shop, Source
from app.jobs.events import ANALYZE_LOCK_TTL_SECONDS, JobEvents
from app.security.secrets import SecretBox
from tests.ingest_fakes import (
    FakeRedis,
    RecordingJobQueue,
    Rows,
    build_ingest_app,
    make_product_row,
    make_review_row,
    make_source_row,
)

SECRET = "vN5y6rM0J9h2xL8qK3pW1zB4cF7dG0tH2sV5mN8wX4Q="
PROFILE_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
BATCH_SIZE = 10


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "SECRET_KEY": SECRET,
        "SENTIMENT_BATCH_SIZE": BATCH_SIZE,
        "JOB_STALE_TIMEOUT_SECONDS": 60,
        "JOB_QUEUED_TIMEOUT_SECONDS": 600,
    }
    values.update(overrides)
    return Settings(**values)


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class AnalyzeSession:
    """Session double answering ``get`` + the two ``select`` shapes the endpoint issues."""

    def __init__(
        self,
        *,
        source: Source | None,
        profile: LLMProfile | None = None,
        products: list[Product] | None = None,
        reviews: list[Review] | None = None,
        yield_points: bool = True,
    ) -> None:
        self.source = source
        self.profile = profile
        self.products = products if products is not None else []
        self.reviews = reviews if reviews is not None else []
        self.yield_points = yield_points
        self.gets: list[tuple[Any, Any]] = []
        self.executed = 0
        self.commits = 0

    async def get(self, model: Any, pk: Any) -> Any:
        self.gets.append((model, pk))
        if model is Source:
            return self.source if self.source is not None and self.source.id == pk else None
        if model is LLMProfile:
            return self.profile if self.profile is not None and self.profile.id == pk else None
        if model is Shop:  # pragma: no cover - defensive, scope uses product_ids only
            return None
        return None

    async def execute(self, statement: Any) -> Rows:
        # A real suspension point so two concurrent requests can genuinely interleave.
        if self.yield_points:
            await asyncio.sleep(0)
        self.executed += 1
        entity = statement.column_descriptions[0].get("entity")
        if entity is Product:
            return Rows(self.products)
        if entity is Review:
            return Rows(self.reviews)
        raise AssertionError(f"unexpected statement in analyze endpoint: {statement}")

    async def commit(self) -> None:
        self.commits += 1


def analyzed_by_rating(external_id: str, *, rating: int = 5) -> Review:
    """A review whose sentiment was inferred from the stars (the rows analyze must upgrade)."""
    from app.ingest.sentiment import rating_to_sentiment, review_content_hash

    review = make_review_row(external_id, rating=rating)
    review.sentiment = rating_to_sentiment(rating)
    review.sentiment_source = "rating"
    review.aspects = []
    review.analyzed_at = datetime.now(UTC)
    review.content_hash = review_content_hash(review)
    return review


def analyzed_by_llm(external_id: str) -> Review:
    review = analyzed_by_rating(external_id)
    review.sentiment_source = "llm"
    review.aspects = [{"aspect": "giao hàng", "sentiment": "positive"}]
    return review


def make_profile(**overrides: Any) -> LLMProfile:
    values: dict[str, Any] = {
        "id": PROFILE_ID,
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "chat_model": "nvidia/nemotron-3-70b",
        "capabilities": {"chat": True, "json_mode": True},
    }
    values.update(overrides)
    return LLMProfile(**values)


def make_source(**overrides: Any) -> Source:
    values: dict[str, Any] = {"status": "done"}
    values.update(overrides)
    return make_source_row(**values)


def make_product_for(source: Source, **overrides: Any) -> Product:
    product = make_product_row(**overrides)
    source.external_ref = {"product_ids": [str(product.id)]}
    return product


def build_client(
    *,
    session: AnalyzeSession,
    events: JobEvents,
    queue: RecordingJobQueue,
    settings: Settings | None = None,
    secret_box: Any = None,
) -> httpx.AsyncClient:
    app = build_ingest_app(
        session=session,
        settings=settings or make_settings(),
        job_events=events,
        job_queue=queue,
        secret_box=secret_box,
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )


async def post_analyze(
    client: httpx.AsyncClient,
    source_id: uuid.UUID | str,
    *,
    profile_id: str = str(PROFILE_ID),
    **extra: Any,
) -> httpx.Response:
    body: dict[str, Any] = {"llm_profile_id": profile_id}
    body.update(extra)
    return await client.post(f"/api/sources/{source_id}/analyze", json=body)


def events_for(redis: FakeRedis | None = None) -> JobEvents:
    return JobEvents(redis if redis is not None else FakeRedis())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 404 — unknown source / profile
# ---------------------------------------------------------------------------


async def test_unknown_source_returns_404_without_touching_redis() -> None:
    redis = FakeRedis()
    session = AnalyzeSession(source=None)
    queue = RecordingJobQueue()
    async with build_client(session=session, events=events_for(redis), queue=queue) as client:
        response = await post_analyze(client, uuid.uuid4())

    assert response.status_code == 404
    assert response.json()["detail"] == "Source not found."
    assert session.executed == 0
    assert queue.analyze_calls == []
    assert redis.hashes == {} and redis.strings == {}


async def test_unknown_profile_returns_404_and_creates_no_job() -> None:
    source = make_source()
    session = AnalyzeSession(source=source, profile=None)
    redis = FakeRedis()
    queue = RecordingJobQueue()
    async with build_client(session=session, events=events_for(redis), queue=queue) as client:
        response = await post_analyze(client, source.id)

    assert response.status_code == 404
    assert response.json()["detail"] == "LLM profile not found."
    assert queue.analyze_calls == []
    assert redis.hashes == {} and redis.strings == {}
    assert session.commits == 0
    # The source was loaded, then the profile lookup failed: no scope/review queries at all.
    assert session.executed == 0


# ---------------------------------------------------------------------------
# 409 — source not ready / secret mismatch / analysis running
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status_value", ["pending", "queued", "running", "failed"])
async def test_source_not_ready_returns_409_for_every_non_ready_status(
    status_value: str,
) -> None:
    source = make_source(status=status_value)
    session = AnalyzeSession(source=source, profile=make_profile())
    redis = FakeRedis()
    queue = RecordingJobQueue()
    async with build_client(session=session, events=events_for(redis), queue=queue) as client:
        response = await post_analyze(client, source.id)

    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "source_not_ready"
    assert f"status='{status_value}'" in body["detail"]
    assert queue.analyze_calls == []
    assert redis.hashes == {} and redis.strings == {}
    # No profile lookup either: readiness is checked before anything else.
    assert [model for model, _ in session.gets] == [Source]


@pytest.mark.parametrize("status_value", ["done", "partial"])
async def test_ready_source_statuses_are_accepted(status_value: str) -> None:
    source = make_source(status=status_value)
    product = make_product_for(source)
    session = AnalyzeSession(
        source=source,
        profile=make_profile(),
        products=[product],
        reviews=[analyzed_by_rating("r1")],
    )
    queue = RecordingJobQueue()
    async with build_client(session=session, events=events_for(), queue=queue) as client:
        response = await post_analyze(client, source.id)

    assert response.status_code == 202
    assert len(queue.analyze_calls) == 1


async def test_undecryptable_api_key_returns_409_secret_key_mismatch() -> None:
    source = make_source()
    profile = make_profile(api_key_enc="not-a-fernet-token")
    box = SecretBox()
    box.init(SECRET)

    session = AnalyzeSession(source=source, profile=profile)
    redis = FakeRedis()
    queue = RecordingJobQueue()
    async with build_client(
        session=session,
        events=events_for(redis),
        queue=queue,
        secret_box=box,
    ) as client:
        response = await post_analyze(client, source.id)

    assert response.status_code == 409
    assert response.json()["error"] == "secret_key_mismatch"
    assert queue.analyze_calls == []
    assert redis.strings == {}, "a mismatched key must not take the analyze lock"
    assert session.executed == 0


async def test_live_lock_returns_409_analysis_running_with_the_job_id() -> None:
    source = make_source()
    redis = FakeRedis()
    events = events_for(redis)
    holder = "job-already-running"
    await events.create_job(holder, source_id=str(source.id), job_type="analyze")
    await events.set_status(holder, "running", source_id=str(source.id))
    await redis.set(JobEvents.analyze_lock_key(str(source.id)), holder)

    queue = RecordingJobQueue()
    session = AnalyzeSession(source=source, profile=make_profile())
    async with build_client(session=session, events=events, queue=queue) as client:
        response = await post_analyze(client, source.id)

    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "analysis_running"
    assert body["job_id"] == holder
    assert queue.analyze_calls == []
    assert await redis.get(JobEvents.analyze_lock_key(str(source.id))) == holder


# ---------------------------------------------------------------------------
# 200 — up to date
# ---------------------------------------------------------------------------


async def test_up_to_date_returns_200_without_creating_a_job() -> None:
    source = make_source()
    product = make_product_for(source)
    session = AnalyzeSession(
        source=source,
        profile=make_profile(),
        products=[product],
        reviews=[analyzed_by_llm("r1"), analyzed_by_llm("r2")],
    )
    redis = FakeRedis()
    queue = RecordingJobQueue()
    async with build_client(session=session, events=events_for(redis), queue=queue) as client:
        response = await post_analyze(client, source.id)

    assert response.status_code == 200
    assert response.json() == {"status": "up_to_date"}
    assert queue.analyze_calls == []
    assert redis.hashes == {} and redis.strings == {}
    assert session.commits == 0
    assert "last_analyze_job_id" not in (source.stats or {})


async def test_source_without_products_is_up_to_date() -> None:
    source = make_source()
    source.external_ref = {}
    session = AnalyzeSession(source=source, profile=make_profile(), products=[], reviews=[])
    queue = RecordingJobQueue()
    async with build_client(session=session, events=events_for(), queue=queue) as client:
        response = await post_analyze(client, source.id)

    assert response.status_code == 200
    assert response.json() == {"status": "up_to_date"}
    assert queue.analyze_calls == []


# ---------------------------------------------------------------------------
# 202 — enqueue + estimate + stats
# ---------------------------------------------------------------------------


async def test_enqueues_one_job_with_the_llm_request_estimate() -> None:
    source = make_source()
    product = make_product_for(source)
    reviews = [analyzed_by_rating(f"r{idx}") for idx in range(25)]
    reviews.append(analyzed_by_llm("llm-1"))  # already analyzed → not counted
    session = AnalyzeSession(
        source=source,
        profile=make_profile(),
        products=[product],
        reviews=reviews,
    )
    redis = FakeRedis()
    events = events_for(redis)
    queue = RecordingJobQueue()

    async with build_client(
        session=session,
        events=events,
        queue=queue,
        settings=make_settings(SENTIMENT_BATCH_SIZE=10),
    ) as client:
        response = await post_analyze(client, source.id)

    assert response.status_code == 202
    body = response.json()
    assert uuid.UUID(body["job_id"])
    assert body["reviews_to_analyze"] == 25
    assert body["estimated_llm_requests"] == 3  # ceil(25 / 10)
    assert set(body) == {"job_id", "reviews_to_analyze", "estimated_llm_requests"}

    assert queue.analyze_calls == [
        {
            "job_id": body["job_id"],
            "source_id": str(source.id),
            "llm_profile_id": str(PROFILE_ID),
        }
    ]
    job_hash = await redis.hgetall(JobEvents.status_key(body["job_id"]))
    assert job_hash["status"] == "queued"
    assert job_hash["job_type"] == "analyze"
    assert job_hash["source_id"] == str(source.id)

    status = await events.get_status(body["job_id"])
    assert status is not None and status["status"] == "queued"
    queued_events = await events.list_events(body["job_id"])
    assert [event["stage"] for event in queued_events] == ["queued"]
    assert queued_events[0]["total"] == 25

    # The report/banner and the UI's "attach to running job" both read this from stats.
    assert source.stats["last_analyze_job_id"] == body["job_id"]
    assert session.commits == 1

    lock_key = JobEvents.analyze_lock_key(str(source.id))
    assert await redis.get(lock_key) == body["job_id"]
    assert redis.expiry[lock_key] == ANALYZE_LOCK_TTL_SECONDS


async def test_estimate_never_reports_zero_requests_for_positive_reviews() -> None:
    source = make_source()
    product = make_product_for(source)
    session = AnalyzeSession(
        source=source,
        profile=make_profile(),
        products=[product],
        reviews=[analyzed_by_rating("r1")],
    )
    queue = RecordingJobQueue()
    async with build_client(
        session=session,
        events=events_for(),
        queue=queue,
        settings=make_settings(SENTIMENT_BATCH_SIZE=0),
    ) as client:
        response = await post_analyze(client, source.id)

    assert response.status_code == 202
    assert response.json()["estimated_llm_requests"] == 1


async def test_lock_stale_holder_is_reclaimed_after_a_worker_timeout() -> None:
    """A worker that died leaves a lock behind: the next request must not be blocked."""
    source = make_source()
    redis = FakeRedis()
    events = events_for(redis)
    dead_job = "job-dead-worker"
    await events.create_job(dead_job, source_id=str(source.id), job_type="analyze")
    await events.set_status(dead_job, "running", source_id=str(source.id))
    dead_heartbeat = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    await redis.hset(
        JobEvents.status_key(dead_job),
        mapping={"heartbeat_at": dead_heartbeat, "updated_at": dead_heartbeat},
    )
    await redis.set(JobEvents.analyze_lock_key(str(source.id)), dead_job)

    product = make_product_for(source)
    session = AnalyzeSession(
        source=source,
        profile=make_profile(),
        products=[product],
        reviews=[analyzed_by_rating("r1")],
    )
    queue = RecordingJobQueue()
    async with build_client(session=session, events=events, queue=queue) as client:
        response = await post_analyze(client, source.id)

    assert response.status_code == 202
    new_job_id = response.json()["job_id"]
    assert new_job_id != dead_job
    assert await redis.get(JobEvents.analyze_lock_key(str(source.id))) == new_job_id
    dead_status = await events.get_status(dead_job)
    assert dead_status is not None and dead_status["status"] == "failed"
    assert "worker_timeout" in (dead_status["error"] or "")


async def test_lock_of_a_finished_job_is_released_and_reused() -> None:
    source = make_source()
    redis = FakeRedis()
    events = events_for(redis)
    finished_job = "job-finished"
    await events.create_job(finished_job, source_id=str(source.id), job_type="analyze")
    await events.set_status(finished_job, "done", source_id=str(source.id))
    await redis.set(JobEvents.analyze_lock_key(str(source.id)), finished_job)

    product = make_product_for(source)
    session = AnalyzeSession(
        source=source,
        profile=make_profile(),
        products=[product],
        reviews=[analyzed_by_rating("r1")],
    )
    queue = RecordingJobQueue()
    async with build_client(session=session, events=events, queue=queue) as client:
        response = await post_analyze(client, source.id)

    assert response.status_code == 202
    assert await redis.get(JobEvents.analyze_lock_key(str(source.id))) == response.json()["job_id"]


async def test_two_concurrent_requests_create_exactly_one_job() -> None:
    """Double-clicking must cost one job, not two LLM bills."""
    source = make_source()
    product = make_product_for(source)
    redis = FakeRedis()
    events = events_for(redis)
    queue = RecordingJobQueue()

    async with build_client(
        session=AnalyzeSession(
            source=source,
            profile=make_profile(),
            products=[product],
            reviews=[analyzed_by_rating("r1")],
        ),
        events=events,
        queue=queue,
    ) as client:
        responses = await asyncio.gather(
            post_analyze(client, source.id),
            post_analyze(client, source.id),
        )

    codes = sorted(response.status_code for response in responses)
    assert codes == [202, 409]
    accepted = next(r for r in responses if r.status_code == 202)
    rejected = next(r for r in responses if r.status_code == 409)
    assert rejected.json()["error"] == "analysis_running"
    assert rejected.json()["job_id"] == accepted.json()["job_id"]
    assert len(queue.analyze_calls) == 1
    assert len([key for key in redis.hashes if key.startswith("job:")]) == 1


async def test_enqueue_failure_releases_the_lock_and_keeps_the_job_out_of_the_queue() -> None:
    source = make_source()
    product = make_product_for(source)
    redis = FakeRedis()
    events = events_for(redis)
    queue = RecordingJobQueue(fail_analyze=True)
    session = AnalyzeSession(
        source=source,
        profile=make_profile(),
        products=[product],
        reviews=[analyzed_by_rating("r1")],
    )

    async with build_client(session=session, events=events, queue=queue) as client:
        with pytest.raises(ConnectionError):
            await post_analyze(client, source.id)

    assert await redis.get(JobEvents.analyze_lock_key(str(source.id))) is None
    assert queue.analyze_calls == []


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------


async def test_extra_body_fields_are_rejected_with_422() -> None:
    source = make_source()
    session = AnalyzeSession(source=source, profile=make_profile())
    queue = RecordingJobQueue()
    async with build_client(session=session, events=events_for(), queue=queue) as client:
        response = await post_analyze(client, source.id, force=True)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any(item["type"] == "extra_forbidden" for item in detail)
    assert [model for model, _ in session.gets] == [], "validation runs before any lookup"
    assert queue.analyze_calls == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"llm_profile_id": "not-a-uuid"},
        {"llm_profile_id": None},
    ],
)
async def test_missing_or_malformed_profile_id_is_rejected_with_422(body: dict[str, Any]) -> None:
    source = make_source()
    session = AnalyzeSession(source=source, profile=make_profile())
    queue = RecordingJobQueue()
    async with build_client(session=session, events=events_for(), queue=queue) as client:
        response = await client.post(f"/api/sources/{source.id}/analyze", json=body)

    assert response.status_code == 422
    assert queue.analyze_calls == []
    assert session.gets == []


def test_request_model_forbids_extra_fields() -> None:
    assert AnalyzeSourceRequest.model_config.get("extra") == "forbid"
    assert set(AnalyzeSourceRequest.model_fields) == {"llm_profile_id"}


# ---------------------------------------------------------------------------
# Lock primitives on real JobEvents over FakeRedis
# ---------------------------------------------------------------------------


async def test_analyze_lock_is_exclusive_and_only_its_owner_can_release_it() -> None:
    events = events_for()

    assert await events.acquire_source_lock("src-1", "job-a", ttl_seconds=30) == (True, "job-a")
    assert await events.acquire_source_lock("src-1", "job-b", ttl_seconds=30) == (False, "job-a")
    assert await events.get_source_lock("src-1") == "job-a"

    # A different job must not be able to steal/release the lock.
    assert await events.release_source_lock("src-1", "job-b") is False
    assert await events.get_source_lock("src-1") == "job-a"

    assert await events.release_source_lock("src-1", "job-a") is True
    assert await events.get_source_lock("src-1") is None


async def test_mark_stale_does_not_fail_the_source_of_an_analyze_job() -> None:
    """A timed-out analyze job must not mark an already-ingested source as failed."""
    redis = FakeRedis()
    events = events_for(redis)
    source_id = uuid.uuid4()
    await events.create_job("job-analyze", source_id=str(source_id), job_type="analyze")
    await events.set_status("job-analyze", "running", source_id=str(source_id))
    old = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    await redis.hset(
        JobEvents.status_key("job-analyze"),
        mapping={"heartbeat_at": old, "updated_at": old},
    )

    transitioned, latest = await events.mark_stale("job-analyze")

    assert transitioned is True
    assert latest is not None and latest["status"] == "failed"
    # No DB write was attempted (the double has no session_factory and no rows).
    assert redis.strings == {}


class RacyLockEvents(JobEvents):
    """Lock looks free at the pre-check, then a rival job grabs it before ``SET NX``.

    This is the interleaving the second terminal-holder check exists for: between
    ``get_source_lock`` (no holder) and ``acquire_source_lock`` another request can take
    the lock. A rival that already finished must not block the source; a rival that is
    still running must produce 409 ``analysis_running`` (and never a second job).
    """

    def __init__(self, redis: FakeRedis, *, rival_job: str) -> None:
        super().__init__(redis)  # type: ignore[arg-type]
        self.rival_job = rival_job
        self.prechecks = 0
        self.steals = 0

    async def get_source_lock(self, source_id: str) -> str | None:
        self.prechecks += 1
        if self.prechecks == 1:
            return None
        return await super().get_source_lock(source_id)

    async def acquire_source_lock(
        self,
        source_id: str,
        job_id: str,
        *,
        ttl_seconds: int = ANALYZE_LOCK_TTL_SECONDS,
    ) -> tuple[bool, str | None]:
        if self.steals == 0:
            self.steals += 1
            await self.redis.set(self.analyze_lock_key(source_id), self.rival_job)
            return False, self.rival_job
        return await super().acquire_source_lock(source_id, job_id, ttl_seconds=ttl_seconds)


class UntimedEvents(JobEvents):
    """``JobEvents`` without ``check_stale`` (older double): the API must fall back."""

    def __init__(self, redis: FakeRedis) -> None:
        super().__init__(redis)  # type: ignore[arg-type]
        self.check_stale = None  # type: ignore[assignment]


@pytest.mark.parametrize(
    ("rival_status", "expected_status"),
    [("done", 202), ("failed", 202), ("running", 409)],
)
async def test_a_rival_job_that_steals_the_lock_is_handled_by_its_status(
    rival_status: str,
    expected_status: int,
) -> None:
    source = make_source()
    product = make_product_for(source)
    redis = FakeRedis()
    events = RacyLockEvents(redis, rival_job="job-rival")
    await events.create_job("job-rival", source_id=str(source.id), job_type="analyze")
    await events.set_status("job-rival", rival_status, source_id=str(source.id))
    queue = RecordingJobQueue()

    async with build_client(
        session=AnalyzeSession(
            source=source,
            profile=make_profile(),
            products=[product],
            reviews=[analyzed_by_rating("r1")],
        ),
        events=events,
        queue=queue,
    ) as client:
        response = await post_analyze(client, source.id)

    assert events.steals == 1
    assert response.status_code == expected_status
    if expected_status == 202:
        new_job_id = response.json()["job_id"]
        assert new_job_id != "job-rival"
        assert await redis.get(JobEvents.analyze_lock_key(str(source.id))) == new_job_id
        assert len(queue.analyze_calls) == 1
    else:
        # A live rival is never displaced: one job, one LLM bill.
        assert response.json()["error"] == "analysis_running"
        assert response.json()["job_id"] == "job-rival"
        assert queue.analyze_calls == []
        assert await redis.get(JobEvents.analyze_lock_key(str(source.id))) == "job-rival"


async def test_terminal_holder_is_detected_when_the_events_double_has_no_check_stale() -> None:
    """``_is_lock_holder_terminal`` falls back to ``get_status`` for simpler doubles."""
    source = make_source()
    product = make_product_for(source)
    redis = FakeRedis()
    events = UntimedEvents(redis)  # type: ignore[arg-type]
    await events.create_job("job-finished", source_id=str(source.id), job_type="analyze")
    await events.set_status("job-finished", "failed", source_id=str(source.id))
    await redis.set(JobEvents.analyze_lock_key(str(source.id)), "job-finished")
    queue = RecordingJobQueue()

    async with build_client(
        session=AnalyzeSession(
            source=source,
            profile=make_profile(),
            products=[product],
            reviews=[analyzed_by_rating("r1")],
        ),
        events=events,
        queue=queue,
    ) as client:
        response = await post_analyze(client, source.id)

    assert response.status_code == 202
    assert len(queue.analyze_calls) == 1
