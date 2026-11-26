"""Integration tests for the standalone LLM analysis path (Phần B of ``llm-analyze.md``).

Isolation contract (see skill ``shop-qa-testing``):

* Postgres: dedicated database ``shopqa_phase5_analyze_test``; ``shopqa`` is only used as the
  maintenance connection and is never migrated, truncated or written.
* Redis: database 15 only; database 0 belongs to the running api/worker.
* No network and no real LLM: every completion comes from :class:`ScriptedLLM`.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import asyncpg
import httpx
import pytest
import redis.asyncio as aioredis
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.jobs.tasks as tasks_module
from app.core.config import Settings
from app.core.errors import LLMUnavailableError
from app.db.models import Chunk, LLMProfile, Product, Review, ReviewSummary, Shop, Source
from app.ingest.pipeline import analyze_source, ingest_source
from app.ingest.sentiment import rating_to_sentiment, review_content_hash
from app.jobs.events import ANALYZE_LOCK_TTL_SECONDS, JobEvents
from app.jobs.tasks import run_analyze
from tests.ingest_fakes import (
    FakeEmbedder,
    RecordingJobQueue,
    build_ingest_app,
)

pytestmark = pytest.mark.integration

BACKEND_DIR = Path(__file__).resolve().parents[1]
SECRET = "vN5y6rM0J9h2xL8qK3pW1zB4cF7dG0tH2sV5mN8wX4Q="

PG_HOST = os.environ.get("TEST_PHASE5_ANALYZE_PG_HOST", "localhost")
PG_PORT = int(os.environ.get("TEST_PHASE5_ANALYZE_PG_PORT", "5433"))
PG_USER = os.environ.get("TEST_PHASE5_ANALYZE_PG_USER", "shopqa")
PG_PASSWORD = os.environ.get("TEST_PHASE5_ANALYZE_PG_PASSWORD", "shopqa")
PG_MAINTENANCE_DB = os.environ.get("TEST_PHASE5_ANALYZE_PG_MAINTENANCE_DB", "shopqa")
TEST_DB_NAME = os.environ.get("TEST_PHASE5_ANALYZE_DB_NAME", "shopqa_phase5_analyze_test")
TEST_REDIS_DB = int(os.environ.get("TEST_PHASE5_ANALYZE_REDIS_DB", "15"))

PROJECT_DB_NAME = "shopqa"  # never touched: holds real crawl data
DIM = 1024

MAINTENANCE_DSN = f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{PG_MAINTENANCE_DB}"
TEST_DATABASE_URL = (
    f"postgresql+asyncpg://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{TEST_DB_NAME}"
)
TEST_REDIS_URL = f"redis://{PG_HOST}:6380/{TEST_REDIS_DB}"


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "SECRET_KEY": SECRET,
        "DATABASE_URL": TEST_DATABASE_URL,
        "REDIS_URL": TEST_REDIS_URL,
        "EMBEDDING_BACKEND": "none",
        "SENTIMENT_BATCH_SIZE": 20,
        "JOB_STALE_TIMEOUT_SECONDS": 60,
        "JOB_QUEUED_TIMEOUT_SECONDS": 600,
    }
    values.update(overrides)
    return Settings(**values)


# ---------------------------------------------------------------------------
# Fake LLM provider (in-process, no HTTP, no socket)
# ---------------------------------------------------------------------------

_REVIEW_ID_RE = re.compile(r"id: ([0-9a-fA-F-]{36})")


class ScriptedLLM:
    """Minimal ``LLMClient`` double: parses review ids out of the prompt and answers them."""

    def __init__(
        self,
        *,
        fail_ids: set[str] | None = None,
        aspects: list[dict[str, str]] | None = None,
        sentiment: str = "positive",
        negative_ids: set[str] | None = None,
    ) -> None:
        self.config = SimpleNamespace(small_model="scripted-small", chat_model="scripted-chat")
        self.fail_ids = set(fail_ids or set())
        self.negative_ids = set(negative_ids or set())
        self.aspects = (
            aspects if aspects is not None else [{"aspect": "chất lượng", "sentiment": "positive"}]
        )
        self.sentiment = sentiment
        self.calls: list[list[dict[str, Any]]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> Any:
        self.calls.append(messages)
        prompt = messages[-1]["content"]
        ids = _REVIEW_ID_RE.findall(prompt)
        if not ids:  # pragma: no cover - guards against a silently empty prompt
            raise AssertionError("no review ids found in the sentiment prompt")
        if self.fail_ids & set(ids):
            raise LLMUnavailableError("scripted provider outage", status_code=503)
        items = [
            {
                "id": review_id,
                "sentiment": ("negative" if review_id in self.negative_ids else self.sentiment),
                "aspects": self.aspects,
            }
            for review_id in ids
        ]
        return SimpleNamespace(content=json.dumps({"items": items}, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Database / Redis fixtures
# ---------------------------------------------------------------------------


def _skip_if_postgres_unreachable() -> None:
    async def _ping() -> None:
        conn = await asyncio.wait_for(asyncpg.connect(MAINTENANCE_DSN), timeout=5)
        await conn.close()

    try:
        asyncio.run(_ping())
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable at {MAINTENANCE_DSN}: {exc}")


async def _ensure_test_database() -> None:
    conn = await asyncpg.connect(MAINTENANCE_DSN)
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", TEST_DB_NAME)
        if not exists:
            await conn.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def analyze_database_url() -> str:
    _skip_if_postgres_unreachable()
    assert TEST_DB_NAME != PROJECT_DB_NAME, "integration tests must not run against shopqa"

    asyncio.run(_ensure_test_database())
    env = {**os.environ, "DATABASE_URL": TEST_DATABASE_URL}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"alembic failed:\n{result.stdout}\n{result.stderr}"
    return TEST_DATABASE_URL


@pytest.fixture
async def session_factory(analyze_database_url: str) -> AsyncIterator[Any]:
    engine = create_async_engine(analyze_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            current = (await conn.execute(text("SELECT current_database()"))).scalar_one()
            assert current == TEST_DB_NAME
            await conn.execute(
                text("TRUNCATE review_summaries, chunks, reviews, products, shops, sources CASCADE")
            )
            await conn.execute(text("DELETE FROM llm_profiles"))
        yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
async def session(session_factory: Any) -> AsyncIterator[AsyncSession]:
    async with session_factory() as active_session:
        yield active_session


@pytest.fixture
async def redis_client() -> AsyncIterator[Any]:
    client = aioredis.from_url(TEST_REDIS_URL, decode_responses=True)
    try:
        await client.ping()
    except Exception as exc:  # pragma: no cover - environment dependent
        await client.aclose()
        pytest.skip(f"Redis not reachable at {TEST_REDIS_URL}: {exc}")

    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def star_only_review(
    product_id: uuid.UUID,
    external_id: str,
    *,
    rating: int = 5,
    content: str | None = None,
    age_hours: int = 0,
) -> Review:
    """A review whose sentiment came from the star fallback (no aspects, hash recorded)."""
    review = Review(
        id=uuid.uuid4(),
        product_id=product_id,
        external_id=external_id,
        rating=rating,
        title="Đánh giá",
        content=content if content is not None else f"Nội dung {external_id} rất ổn",
        author="Người dùng",
        created_at_source=datetime.now(UTC) - timedelta(hours=age_hours),
        images=[],
        raw={},
        sentiment=rating_to_sentiment(rating),
        sentiment_source="rating",
        aspects=[],
        analyzed_at=datetime.now(UTC) - timedelta(days=1),
    )
    review.content_hash = review_content_hash(review)
    return review


def llm_analyzed_review(product_id: uuid.UUID, external_id: str) -> Review:
    review = star_only_review(product_id, external_id)
    review.sentiment_source = "llm"
    review.aspects = [{"aspect": "giao hàng", "sentiment": "positive"}]
    return review


async def seed_source_with_products(
    session: AsyncSession,
    *,
    status: str = "done",
    with_shop: bool = True,
) -> dict[str, Any]:
    """Source with two products (A: rating-only reviews, B: already LLM-analyzed)."""
    shop = (
        Shop(
            id=uuid.uuid4(),
            platform="tiki",
            external_id="seller-analyze",
            name="Shop Analyze",
            url="https://tiki.vn/cua-hang/shop-analyze",
            raw={},
        )
        if with_shop
        else None
    )
    if shop is not None:
        session.add(shop)

    product_a = Product(
        id=uuid.uuid4(),
        shop_id=shop.id if shop is not None else None,
        platform="tiki",
        external_id="5001",
        title="Sản phẩm A",
        url="https://tiki.vn/san-pham-a-p5001.html",
        images=[],
        specs={},
        raw={},
    )
    product_b = Product(
        id=uuid.uuid4(),
        shop_id=shop.id if shop is not None else None,
        platform="tiki",
        external_id="5002",
        title="Sản phẩm B",
        url="https://tiki.vn/san-pham-b-p5002.html",
        images=[],
        specs={},
        raw={},
    )
    session.add_all([product_a, product_b])

    reviews_a = [
        star_only_review(product_a.id, "a-1", rating=5, age_hours=1),
        star_only_review(product_a.id, "a-2", rating=2, age_hours=2),
    ]
    review_b = llm_analyzed_review(product_b.id, "b-1")
    session.add_all([*reviews_a, review_b])

    source = Source(
        id=uuid.uuid4(),
        url="https://tiki.vn/cua-hang/shop-analyze",
        url_type="shop",
        platform="tiki",
        status=status,
        stats={},
        external_ref={
            "product_ids": [str(product_a.id), str(product_b.id)],
            "canonical_url": "https://tiki.vn/cua-hang/shop-analyze",
        },
        crawled_at=datetime.now(UTC),
    )
    session.add(source)

    profile = LLMProfile(
        id=uuid.uuid4(),
        name="Scripted",
        base_url="https://llm.test/v1",
        chat_model="scripted-chat",
        small_model=None,
        capabilities={"chat": True, "json_mode": True},
    )
    session.add(profile)
    await session.commit()

    return {
        "shop": shop,
        "product_a": product_a,
        "product_b": product_b,
        "reviews_a": reviews_a,
        "review_b": review_b,
        "source": source,
        "profile": profile,
    }


async def _reviews_of(session_factory: Any, product_id: uuid.UUID) -> list[Review]:
    """Read reviews through a *fresh* session.

    The pipeline commits from its own sessions; re-using the seeding session would return
    stale identity-map instances instead of what the worker actually persisted.
    """
    async with session_factory() as fresh:
        result = await fresh.execute(
            select(Review).where(Review.product_id == product_id).order_by(Review.external_id.asc())
        )
        return list(result.scalars().all())


async def _product_counts(session_factory: Any, product_id: uuid.UUID) -> tuple[int, int, int]:
    async with session_factory() as fresh:
        product = await fresh.get(Product, product_id)
        assert product is not None
        return (
            product.positive_review_count,
            product.negative_review_count,
            product.neutral_review_count,
        )


# ---------------------------------------------------------------------------
# analyze_source — the happy path
# ---------------------------------------------------------------------------


async def test_analyze_upgrades_only_the_rating_only_reviews(
    session_factory: Any,
    session: AsyncSession,
) -> None:
    seed = await seed_source_with_products(session)
    llm = ScriptedLLM(negative_ids={str(seed["reviews_a"][1].id)})

    result = await analyze_source(
        seed["source"].id,
        llm_profile_id=seed["profile"].id,
        session_factory=session_factory,
        llm_client=llm,
        settings=make_settings(),
    )

    assert result.status == "done"
    assert result.error is None
    assert result.stats.reviews_to_analyze == 2
    assert result.stats.reviews_llm == 2
    assert result.stats.reviews_rating_fallback == 0
    assert result.stats.reviews_skipped == 1  # product B's review was already LLM-analyzed
    assert result.stats.summaries_invalidated == 1
    assert len(llm.calls) == 1

    a_reviews = await _reviews_of(session_factory, seed["product_a"].id)
    assert [review.sentiment_source for review in a_reviews] == ["llm", "llm"]
    assert all(review.aspects for review in a_reviews)
    assert {review.sentiment for review in a_reviews} == {"positive", "negative"}

    # Product B is already analyzed by an LLM: its rows must be byte-for-byte untouched.
    b_review = (await _reviews_of(session_factory, seed["product_b"].id))[0]
    assert b_review.sentiment_source == "llm"
    assert b_review.aspects == [{"aspect": "giao hàng", "sentiment": "positive"}]

    assert await _product_counts(session_factory, seed["product_a"].id) == (1, 1, 0)
    assert await _product_counts(session_factory, seed["product_b"].id) == (1, 0, 0)


async def test_analyze_does_not_re_chunk_or_re_embed(
    session_factory: Any,
    session: AsyncSession,
) -> None:
    seed = await seed_source_with_products(session)
    chunk = Chunk(
        id=uuid.uuid4(),
        product_id=seed["product_a"].id,
        shop_id=seed["shop"].id,
        source_type="review",
        source_ref="a-1",
        text="Sản phẩm rất tốt",
        content_hash="0" * 64,
        embedding=None,
        embedding_model=None,
        dim=None,
    )
    session.add(chunk)
    await session.commit()

    async with session_factory() as fresh:
        before = (await fresh.execute(select(func.count()).select_from(Chunk))).scalar_one()

    result = await analyze_source(
        seed["source"].id,
        llm_profile_id=seed["profile"].id,
        session_factory=session_factory,
        llm_client=ScriptedLLM(),
        settings=make_settings(),
    )

    async with session_factory() as fresh:
        after = (await fresh.execute(select(func.count()).select_from(Chunk))).scalar_one()
        refreshed = await fresh.get(Chunk, chunk.id)

    assert result.status == "done"
    assert before == after == 1
    assert refreshed is not None
    assert refreshed.content_hash == "0" * 64
    assert refreshed.embedding is None, "analyze must not write embeddings"
    assert refreshed.text == "Sản phẩm rất tốt"


async def test_analyze_invalidates_review_summaries_only_for_changed_products(
    session_factory: Any,
    session: AsyncSession,
) -> None:
    seed = await seed_source_with_products(session)
    summary_a = ReviewSummary(
        product_id=seed["product_a"].id,
        reviews_fingerprint="a" * 64,
        summary={"summary": "cũ"},
        model="scripted-chat",
    )
    summary_b = ReviewSummary(
        product_id=seed["product_b"].id,
        reviews_fingerprint="b" * 64,
        summary={"summary": "giữ nguyên"},
        model="scripted-chat",
    )
    session.add_all([summary_a, summary_b])
    await session.commit()

    await analyze_source(
        seed["source"].id,
        llm_profile_id=seed["profile"].id,
        session_factory=session_factory,
        llm_client=ScriptedLLM(),
        settings=make_settings(),
    )

    async with session_factory() as fresh:
        remaining = (await fresh.execute(select(ReviewSummary.product_id))).scalars().all()

    assert list(remaining) == [seed["product_b"].id], (
        "only products whose reviews changed may lose their cached summary"
    )


async def test_analyze_is_a_noop_when_everything_is_already_llm_analyzed(
    session_factory: Any,
    session: AsyncSession,
) -> None:
    seed = await seed_source_with_products(session)
    llm = ScriptedLLM()

    first = await analyze_source(
        seed["source"].id,
        llm_profile_id=seed["profile"].id,
        session_factory=session_factory,
        llm_client=llm,
        settings=make_settings(),
    )
    assert first.stats.reviews_to_analyze == 2

    calls_after_first = len(llm.calls)
    second = await analyze_source(
        seed["source"].id,
        llm_profile_id=seed["profile"].id,
        session_factory=session_factory,
        llm_client=llm,
        settings=make_settings(),
    )

    assert second.status == "done"
    assert second.stats.reviews_to_analyze == 0
    assert second.stats.reviews_analyzed == 0
    assert second.stats.summaries_invalidated == 0
    assert len(llm.calls) == calls_after_first, "a finished analysis must not call the LLM again"


# ---------------------------------------------------------------------------
# analyze_source — partial failures
# ---------------------------------------------------------------------------


async def test_one_failed_llm_batch_falls_back_to_stars_and_keeps_the_rest(
    session_factory: Any,
    session: AsyncSession,
) -> None:
    seed = await seed_source_with_products(session)
    good_review, failing_review = seed["reviews_a"]
    llm = ScriptedLLM(fail_ids={str(failing_review.id)})

    result = await analyze_source(
        seed["source"].id,
        llm_profile_id=seed["profile"].id,
        session_factory=session_factory,
        llm_client=llm,
        settings=make_settings(SENTIMENT_BATCH_SIZE=1),
    )

    assert result.status == "partial"
    assert result.error is not None
    assert "1/2 LLM batch(es) failed" in result.error
    assert "star-rating fallback" in result.error
    assert result.stats.batches_total == 2
    assert result.stats.batches_failed == 1
    assert result.stats.reviews_llm == 1
    assert result.stats.reviews_rating_fallback == 1

    by_external_id = {
        review.external_id: review
        for review in await _reviews_of(session_factory, seed["product_a"].id)
    }
    upgraded = by_external_id[good_review.external_id]
    fallback = by_external_id[failing_review.external_id]
    assert upgraded.sentiment_source == "llm"
    assert upgraded.aspects
    # The failed batch is persisted with the star fallback, so the product counts stay right.
    assert fallback.sentiment_source == "rating"
    assert fallback.sentiment == rating_to_sentiment(fallback.rating)
    assert fallback.aspects == []

    positive, negative, neutral = await _product_counts(session_factory, seed["product_a"].id)
    assert positive + negative + neutral == 2


async def test_analyze_reports_failure_for_an_unknown_source(session_factory: Any) -> None:
    with pytest.raises(ValueError, match="not found"):
        await analyze_source(
            uuid.uuid4(),
            llm_profile_id=uuid.uuid4(),
            session_factory=session_factory,
            llm_client=ScriptedLLM(),
            settings=make_settings(),
        )


async def test_analyze_reports_failure_for_an_unknown_profile(
    session_factory: Any,
    session: AsyncSession,
) -> None:
    seed = await seed_source_with_products(session)

    with pytest.raises(ValueError, match="profile"):
        await analyze_source(
            seed["source"].id,
            llm_profile_id=uuid.uuid4(),
            session_factory=session_factory,
            settings=make_settings(),
        )


# ---------------------------------------------------------------------------
# run_analyze — Arq task over the real stack
# ---------------------------------------------------------------------------


def forbid_crawl_and_ingest(monkeypatch: Any) -> None:
    async def _forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("an analyze job must never crawl or ingest")

    monkeypatch.setattr(tasks_module, "crawl_url", _forbidden)
    monkeypatch.setattr(tasks_module, "ingest_source", _forbidden)


async def test_run_analyze_end_to_end_releases_the_lock_and_never_crawls(
    session_factory: Any,
    session: AsyncSession,
    redis_client: Any,
    monkeypatch: Any,
) -> None:
    forbid_crawl_and_ingest(monkeypatch)
    seed = await seed_source_with_products(session)
    events = JobEvents(redis_client)
    job_id = f"job-{uuid.uuid4()}"
    await events.create_job(job_id, source_id=str(seed["source"].id), job_type="analyze")
    await events.acquire_source_lock(str(seed["source"].id), job_id, ttl_seconds=60)

    class BombEmbedder(FakeEmbedder):
        async def embed(self, texts: Any) -> list[list[float]]:
            raise AssertionError("run_analyze must not embed")

    result = await run_analyze(
        {
            "session_factory": session_factory,
            "job_events": events,
            "llm_client": ScriptedLLM(),
            "settings": make_settings(),
            "embedder": BombEmbedder(dim=DIM),
        },
        job_id,
        str(seed["source"].id),
        str(seed["profile"].id),
    )

    assert result["status"] == "done"
    assert result["source_id"] == str(seed["source"].id)

    status = await events.get_status(job_id)
    assert status is not None and status["status"] == "done"
    stages = [event["stage"] for event in await events.list_events(job_id)]
    assert stages[0] == "started"
    assert "sentiment" in stages
    assert stages[-1] == "done"
    assert not {"classify", "crawl_done", "embed"} & set(stages)

    # The task released its own lock, so the source can be analyzed again immediately.
    assert await redis_client.get(JobEvents.analyze_lock_key(str(seed["source"].id))) is None

    # No chunk was created: analysis only touches reviews/sentiments/counters.
    async with session_factory() as fresh:
        chunk_count = (await fresh.execute(select(func.count()).select_from(Chunk))).scalar_one()
    assert chunk_count == 0


async def test_run_analyze_marks_the_job_partial_when_a_batch_fails(
    session_factory: Any,
    session: AsyncSession,
    redis_client: Any,
    monkeypatch: Any,
) -> None:
    forbid_crawl_and_ingest(monkeypatch)
    seed = await seed_source_with_products(session)
    failing_review = seed["reviews_a"][0]
    events = JobEvents(redis_client)
    job_id = f"job-{uuid.uuid4()}"
    await events.create_job(job_id, source_id=str(seed["source"].id), job_type="analyze")

    result = await run_analyze(
        {
            "session_factory": session_factory,
            "job_events": events,
            "llm_client": ScriptedLLM(fail_ids={str(failing_review.id)}),
            "settings": make_settings(SENTIMENT_BATCH_SIZE=1),
        },
        job_id,
        str(seed["source"].id),
        str(seed["profile"].id),
    )

    assert result["status"] == "partial"
    status = await events.get_status(job_id)
    assert status is not None and status["status"] == "partial"
    assert "star-rating fallback" in (status["error"] or "")
    final_event = (await events.list_events(job_id))[-1]
    assert final_event["stage"] == "partial"
    assert "fallback=1" in final_event["message"]


# ---------------------------------------------------------------------------
# ingest_source must upgrade rating-only reviews when an LLM is available
# ---------------------------------------------------------------------------


async def test_ingest_upgrades_rating_only_reviews_when_a_profile_is_used(
    session_factory: Any,
    session: AsyncSession,
) -> None:
    seed = await seed_source_with_products(session)
    llm = ScriptedLLM()

    result = await ingest_source(
        seed["source"].id,
        llm_profile_id=seed["profile"].id,
        session_factory=session_factory,
        embedder=FakeEmbedder(dim=DIM),
        llm_client=llm,
        settings=make_settings(),
    )

    assert result.status == "done"
    assert result.stats.reviews_llm == 2
    a_reviews = await _reviews_of(session_factory, seed["product_a"].id)
    assert all(review.sentiment_source == "llm" for review in a_reviews)
    assert all(review.aspects for review in a_reviews)
    # Product B's already-analyzed review is not re-sent to the LLM.
    assert result.stats.reviews_skipped >= 1


async def test_ingest_without_a_profile_does_not_reopen_rating_only_reviews(
    session_factory: Any,
    session: AsyncSession,
) -> None:
    seed = await seed_source_with_products(session)
    before = {
        review.external_id: review.sentiment_source
        for review in await _reviews_of(session_factory, seed["product_a"].id)
    }

    result = await ingest_source(
        seed["source"].id,
        session_factory=session_factory,
        embedder=FakeEmbedder(dim=DIM),
        settings=make_settings(),
    )

    assert result.stats.reviews_llm == 0
    assert result.stats.reviews_skipped >= 2
    after = {
        review.external_id: review.sentiment_source
        for review in await _reviews_of(session_factory, seed["product_a"].id)
    }
    assert after == before, "without an LLM the star fallback must not loop"


# ---------------------------------------------------------------------------
# Lock over real Redis (Lua release path) + endpoint over real DB/Redis
# ---------------------------------------------------------------------------


async def test_analyze_lock_lives_in_redis_with_a_ttl_and_only_its_owner_releases_it(
    redis_client: Any,
) -> None:
    events = JobEvents(redis_client)
    key = JobEvents.analyze_lock_key("src-real")

    assert await events.acquire_source_lock("src-real", "job-1", ttl_seconds=90) == (True, "job-1")
    ttl = await redis_client.ttl(key)
    assert 0 < ttl <= 90

    assert await events.acquire_source_lock("src-real", "job-2", ttl_seconds=90) == (False, "job-1")
    assert await events.release_source_lock("src-real", "job-2") is False
    assert await events.get_source_lock("src-real") == "job-1"

    assert await events.release_source_lock("src-real", "job-1") is True
    assert await events.get_source_lock("src-real") is None


async def test_analyze_ttl_covers_the_whole_job_lifecycle() -> None:
    """A queued job can wait ``JOB_QUEUED_TIMEOUT_SECONDS``; the lock must not expire first."""
    settings = make_settings(JOB_STALE_TIMEOUT_SECONDS=120, JOB_QUEUED_TIMEOUT_SECONDS=600)

    lock_ttl = max(
        ANALYZE_LOCK_TTL_SECONDS,
        int(settings.JOB_STALE_TIMEOUT_SECONDS + settings.JOB_QUEUED_TIMEOUT_SECONDS),
    )

    assert lock_ttl == ANALYZE_LOCK_TTL_SECONDS == 1800
    assert lock_ttl > settings.JOB_QUEUED_TIMEOUT_SECONDS


async def _app_with_db(session_factory: Any, *, events: Any, queue: Any) -> Any:
    async def _session_dependency() -> AsyncIterator[AsyncSession]:
        async with session_factory() as active_session:
            yield active_session

    app = build_ingest_app(
        session=object(),
        settings=make_settings(),
        job_events=events,
        job_queue=queue,
    )
    from app.api.deps import get_session

    app.dependency_overrides[get_session] = _session_dependency
    return app


async def test_analyze_endpoint_over_real_db_and_redis_is_idempotent_per_source(
    session_factory: Any,
    session: AsyncSession,
    redis_client: Any,
) -> None:
    seed = await seed_source_with_products(session)
    events = JobEvents(redis_client)
    queue = RecordingJobQueue()
    app = await _app_with_db(session_factory, events=events, queue=queue)
    body = {"llm_profile_id": str(seed["profile"].id)}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        first = await client.post(f"/api/sources/{seed['source'].id}/analyze", json=body)
        second = await client.post(f"/api/sources/{seed['source'].id}/analyze", json=body)

    assert first.status_code == 202
    payload = first.json()
    assert payload["reviews_to_analyze"] == 2
    assert payload["estimated_llm_requests"] == 1

    assert second.status_code == 409
    assert second.json()["error"] == "analysis_running"
    assert second.json()["job_id"] == payload["job_id"]
    assert len(queue.analyze_calls) == 1

    # ``last_analyze_job_id`` is persisted so the UI can resume SSE after a reload.
    async with session_factory() as fresh_session:
        source = await fresh_session.get(Source, seed["source"].id)
    assert source is not None
    assert source.stats["last_analyze_job_id"] == payload["job_id"]
    assert (
        await redis_client.get(JobEvents.analyze_lock_key(str(seed["source"].id)))
        == payload["job_id"]
    )


async def test_analyze_endpoint_returns_up_to_date_against_real_rows(
    session_factory: Any,
    session: AsyncSession,
    redis_client: Any,
) -> None:
    seed = await seed_source_with_products(session)
    # Mark every review in scope as LLM-analyzed.
    for review in [*seed["reviews_a"], seed["review_b"]]:
        review.sentiment_source = "llm"
        review.aspects = [{"aspect": "giá", "sentiment": "neutral"}]
    await session.commit()

    events = JobEvents(redis_client)
    queue = RecordingJobQueue()
    app = await _app_with_db(session_factory, events=events, queue=queue)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            f"/api/sources/{seed['source'].id}/analyze",
            json={"llm_profile_id": str(seed["profile"].id)},
        )

    assert response.status_code == 200
    assert response.json() == {"status": "up_to_date"}
    assert queue.analyze_calls == []
    assert await redis_client.get(JobEvents.analyze_lock_key(str(seed["source"].id))) is None
