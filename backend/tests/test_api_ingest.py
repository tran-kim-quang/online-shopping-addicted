"""Unit tests for ``POST /api/ingest`` (classification, TTL cache, enqueue wiring).

The SQL semantics of the cache predicate (a crawl-only source without
``stats['ingest']`` must NOT count as a cache hit) are verified against a real
database in ``test_ingest_integration.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy.dialects import postgresql

from app.core.config import Settings
from app.db.models import LLMProfile
from tests.ingest_fakes import (
    RecordingJobEvents,
    RecordingJobQueue,
    Rows,
    ScriptedSession,
    build_ingest_app,
    make_source_row,
)

SECRET = "vN5y6rM0J9h2xL8qK3pW1zB4cF7dG0tH2sV5mN8wX4Q="
PRODUCT_URL = "https://tiki.vn/san-pham-p1001.html"
CANONICAL_URL = "https://tiki.vn/san-pham-p1001.html"


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"SECRET_KEY": SECRET, "CRAWL_CACHE_TTL_HOURS": 48}
    values.update(overrides)
    return Settings(**values)


class CapturingSession(ScriptedSession):
    """Session double that also records the statements it received."""

    def __init__(self, results: list[Any]) -> None:
        super().__init__(results)
        self.statements: list[Any] = []

    async def execute(self, stmt: Any) -> Any:
        self.statements.append(stmt)
        return await super().execute(stmt)

    async def get(self, model: Any, pk: Any) -> Any:
        self.executed += 1
        if model is LLMProfile:
            return self._profiles.get(pk)
        return None

    _profiles: dict[Any, Any] = {}


@pytest.fixture
def job_events() -> RecordingJobEvents:
    return RecordingJobEvents()


@pytest.fixture
def job_queue() -> RecordingJobQueue:
    return RecordingJobQueue()


def make_client(
    *,
    session: Any,
    job_events: RecordingJobEvents,
    job_queue: RecordingJobQueue,
    settings: Settings | None = None,
    resolver: Any = None,
) -> httpx.AsyncClient:
    app = build_ingest_app(
        session=session,
        settings=settings or make_settings(),
        job_events=job_events,
        job_queue=job_queue,
        resolver=resolver,
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )


async def post_ingest(client: httpx.AsyncClient, **body: Any) -> httpx.Response:
    payload = {"url": PRODUCT_URL}
    payload.update(body)
    return await client.post("/api/ingest", json=payload)


# ---------------------------------------------------------------------------
# Classification errors → 400
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "error_code"),
    [
        ("https://www.lazada.vn/products/x", "unsupported_platform"),
        ("https://example.com/san-pham", "unsupported_url"),
        ("ftp://tiki.vn/san-pham-p1.html", "unsupported_url"),
        ("https://tiki.vn/tai-khoan/dang-nhap", "unsupported_url"),
    ],
)
async def test_unsupported_urls_are_rejected_with_reasons(
    url: str,
    error_code: str,
    job_events: RecordingJobEvents,
    job_queue: RecordingJobQueue,
) -> None:
    session = ScriptedSession([Rows([])])
    client = make_client(session=session, job_events=job_events, job_queue=job_queue)
    try:
        response = await client.post("/api/ingest", json={"url": url})
    finally:
        await client.aclose()

    assert response.status_code == 400
    body = response.json()
    assert body["error"] == error_code
    assert body["detail"]
    assert session.executed == 0, "invalid URLs must not touch the database"
    assert job_queue.calls == []


async def test_short_tiki_link_is_resolved_before_enqueueing(
    monkeypatch: Any,
    job_events: RecordingJobEvents,
    job_queue: RecordingJobQueue,
) -> None:
    import app.api.ingest as ingest_module
    from app.crawl.types import ParsedURL

    calls: list[dict[str, Any]] = []

    async def resolver(host: str) -> list[str]:
        return ["93.184.216.34"]

    async def _fake_resolve(url: str, *, allow_private: bool, resolver: Any) -> ParsedURL:
        calls.append({"url": url, "allow_private": allow_private, "resolver": resolver})
        return ParsedURL(
            platform="tiki",
            kind="product",
            product_id="1001",
            canonical_url=CANONICAL_URL,
        )

    monkeypatch.setattr(ingest_module, "resolve_and_classify_url", _fake_resolve)
    session = ScriptedSession([Rows([])])
    client = make_client(
        session=session,
        job_events=job_events,
        job_queue=job_queue,
        resolver=resolver,
    )
    try:
        response = await client.post("/api/ingest", json={"url": "https://ti.ki/abc123"})
    finally:
        await client.aclose()

    assert response.status_code == 202
    assert len(calls) == 1
    assert calls[0]["resolver"] is resolver, (
        "short links must resolve through the injected resolver"
    )
    assert job_queue.calls[0]["url"] == CANONICAL_URL


# ---------------------------------------------------------------------------
# Cache hit → 200
# ---------------------------------------------------------------------------


async def test_ingested_source_within_ttl_is_returned_as_cache_hit(
    job_events: RecordingJobEvents,
    job_queue: RecordingJobQueue,
) -> None:
    crawled_at = datetime.now(UTC) - timedelta(hours=2)
    cached = make_source_row(
        url=CANONICAL_URL,
        status="done",
        stats={"ingest": {"chunks_total": 12}},
        crawled_at=crawled_at,
        external_ref={"canonical_url": CANONICAL_URL, "product_id": "1001"},
    )
    session = CapturingSession([Rows([cached])])
    client = make_client(session=session, job_events=job_events, job_queue=job_queue)
    try:
        response = await post_ingest(client)
    finally:
        await client.aclose()

    assert response.status_code == 200
    body = response.json()
    assert body["source_id"] == str(cached.id)
    assert body["cached"] is True
    # Cache hits now tell the client how old the data is and when it auto-refreshes.
    assert datetime.fromisoformat(body["crawled_at"]) == crawled_at
    assert datetime.fromisoformat(body["refresh_after"]) == crawled_at + timedelta(hours=48)
    assert job_queue.calls == []
    assert job_events.created == []


async def test_cache_query_requires_ingest_stats_and_a_recent_done_crawl(
    job_events: RecordingJobEvents,
    job_queue: RecordingJobQueue,
) -> None:
    """Guard the crawl-only exclusion: the SQL must filter on ``stats ? 'ingest'``."""
    session = CapturingSession([Rows([])])
    client = make_client(session=session, job_events=job_events, job_queue=job_queue)
    try:
        await post_ingest(client)
    finally:
        await client.aclose()

    assert len(session.statements) == 1
    compiled = str(
        session.statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "stats ? 'ingest'" in compiled
    assert "sources.status = 'done'" in compiled
    assert "sources.crawled_at >=" in compiled


async def test_cache_hit_refresh_after_follows_the_configured_ttl(
    job_events: RecordingJobEvents,
    job_queue: RecordingJobQueue,
) -> None:
    crawled_at = datetime.now(UTC) - timedelta(hours=1)
    cached = make_source_row(
        url=CANONICAL_URL,
        status="done",
        stats={"ingest": {"chunks_total": 3}},
        crawled_at=crawled_at,
        external_ref={"canonical_url": CANONICAL_URL, "product_id": "1001"},
    )
    session = CapturingSession([Rows([cached])])
    client = make_client(
        session=session,
        job_events=job_events,
        job_queue=job_queue,
        settings=make_settings(CRAWL_CACHE_TTL_HOURS=6),
    )
    try:
        response = await post_ingest(client)
    finally:
        await client.aclose()

    assert response.status_code == 200
    assert datetime.fromisoformat(response.json()["refresh_after"]) == crawled_at + timedelta(
        hours=6
    )


@pytest.mark.parametrize("field", ["force", "bypass_cache"])
async def test_extra_request_fields_are_rejected_with_422(
    field: str,
    job_events: RecordingJobEvents,
    job_queue: RecordingJobQueue,
) -> None:
    """``IngestRequest`` forbids extras, so legacy ``force`` clients fail loudly."""
    session = CapturingSession([Rows([])])
    client = make_client(session=session, job_events=job_events, job_queue=job_queue)
    try:
        response = await post_ingest(client, **{field: True})
    finally:
        await client.aclose()

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any(item["loc"] == ["body", field] for item in detail)
    assert any(item["type"] == "extra_forbidden" for item in detail)
    assert session.executed == 0, "a rejected payload must not reach the database"
    assert job_queue.calls == []
    assert job_events.created == []


async def test_zero_ttl_disables_the_cache(
    job_events: RecordingJobEvents,
    job_queue: RecordingJobQueue,
) -> None:
    session = CapturingSession([Rows([])])
    client = make_client(
        session=session,
        job_events=job_events,
        job_queue=job_queue,
        settings=make_settings(CRAWL_CACHE_TTL_HOURS=0),
    )
    try:
        response = await post_ingest(client)
    finally:
        await client.aclose()

    assert response.status_code == 202
    assert session.executed == 0


# ---------------------------------------------------------------------------
# Cache miss → 202 + enqueue
# ---------------------------------------------------------------------------


async def test_cache_miss_enqueues_a_job(
    job_events: RecordingJobEvents,
    job_queue: RecordingJobQueue,
) -> None:
    session = ScriptedSession([Rows([])])
    client = make_client(session=session, job_events=job_events, job_queue=job_queue)
    try:
        response = await post_ingest(client, max_products=5, max_reviews=10)
    finally:
        await client.aclose()

    assert response.status_code == 202
    body = response.json()
    assert body["cached"] is False
    assert body["source_id"] is None
    job_id = body["job_id"]
    assert uuid.UUID(job_id)

    assert job_events.created == [{"job_id": job_id, "url": CANONICAL_URL, "source_id": None}]
    assert [event["stage"] for event in job_events.events] == ["queued"]
    assert job_queue.calls == [
        {
            "job_id": job_id,
            "url": CANONICAL_URL,
            "llm_profile_id": None,
            "max_products": 5,
            "max_reviews": 10,
        }
    ]


async def test_unknown_llm_profile_returns_404(
    job_events: RecordingJobEvents,
    job_queue: RecordingJobQueue,
) -> None:
    session = CapturingSession([Rows([])])
    client = make_client(session=session, job_events=job_events, job_queue=job_queue)
    try:
        response = await post_ingest(client, llm_profile_id=str(uuid.uuid4()))
    finally:
        await client.aclose()

    assert response.status_code == 404
    assert job_queue.calls == []


async def test_known_llm_profile_is_forwarded_to_the_job(
    job_events: RecordingJobEvents,
    job_queue: RecordingJobQueue,
) -> None:
    profile_id = uuid.uuid4()
    session = CapturingSession([Rows([])])
    session._profiles = {
        profile_id: LLMProfile(id=profile_id, name="p", base_url="http://x", chat_model="m")
    }
    client = make_client(session=session, job_events=job_events, job_queue=job_queue)
    try:
        response = await post_ingest(client, llm_profile_id=str(profile_id))
    finally:
        await client.aclose()

    assert response.status_code == 202
    assert job_queue.calls[0]["llm_profile_id"] == str(profile_id)


@pytest.mark.parametrize(
    "body",
    [
        {"url": ""},
        {"url": PRODUCT_URL, "max_products": 0},
        {"url": PRODUCT_URL, "max_products": 501},
        {"url": PRODUCT_URL, "max_reviews": -1},
        {"url": PRODUCT_URL, "max_reviews": 1001},
        {"url": PRODUCT_URL, "llm_profile_id": "not-a-uuid"},
    ],
)
async def test_invalid_payloads_are_rejected_with_422(
    body: dict[str, Any],
    job_events: RecordingJobEvents,
    job_queue: RecordingJobQueue,
) -> None:
    session = ScriptedSession([Rows([])])
    client = make_client(session=session, job_events=job_events, job_queue=job_queue)
    try:
        response = await client.post("/api/ingest", json=body)
    finally:
        await client.aclose()

    assert response.status_code == 422
    assert job_queue.calls == []
