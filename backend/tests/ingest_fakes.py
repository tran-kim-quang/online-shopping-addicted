"""Shared test doubles for the Phase 3 ingestion/jobs layer.

Everything here is offline and deterministic: no Postgres, no Redis, no
``sentence-transformers`` model download. Real-SQL behaviour of the same code
paths is covered by ``test_ingest_integration.py`` against a dedicated database.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from app.db.models import Chunk, Product, Review, Shop, Source

# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------


class FakeEmbedder:
    """Deterministic, in-memory :class:`app.ingest.embedding.Embedder` double.

    Vectors are derived from SHA-256 of the text, so identical text always maps
    to an identical vector (needed for RRF / cosine assertions) while staying
    orthogonal enough between different texts.
    """

    def __init__(self, *, dim: int = 8, name: str = "fake-embedder") -> None:
        self._dim = dim
        self._name = name
        self.calls: list[list[str]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def dim(self) -> int | None:
        return self._dim

    @property
    def embedded_texts(self) -> list[str]:
        return [text for batch in self.calls for text in batch]

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        batch = [str(text) for text in texts]
        if not batch:
            return []
        self.calls.append(batch)
        return [self.vector(text) for text in batch]

    def vector(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        raw = [digest[idx % len(digest)] / 255.0 + 0.001 * (idx + 1) for idx in range(self._dim)]
        norm = sum(value * value for value in raw) ** 0.5 or 1.0
        return [round(value / norm, 8) for value in raw]


class WrongLengthEmbedder(FakeEmbedder):
    """Embedder that returns fewer vectors than requested (error-path double)."""

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = await super().embed(texts)
        return vectors[:-1]


# ---------------------------------------------------------------------------
# ORM rows built in memory (no session/flush required)
# ---------------------------------------------------------------------------


def make_review_row(
    external_id: str = "r1",
    *,
    product_id: uuid.UUID | None = None,
    rating: int = 5,
    title: str | None = "Hài lòng",
    content: str | None = "Sản phẩm tốt, giao hàng nhanh",
    **overrides: Any,
) -> Review:
    """Build an in-memory ``Review`` row (never attached to a session)."""
    data: dict[str, Any] = {
        "id": uuid.uuid4(),
        "product_id": product_id or uuid.uuid4(),
        "external_id": external_id,
        "rating": rating,
        "title": title,
        "content": content,
        "author": "Người dùng A.",
    }
    data.update(overrides)
    return Review(**data)


def make_product_row(
    external_id: str = "1001",
    *,
    title: str = "Gạo ST25 túi 5kg",
    shop_id: uuid.UUID | None = None,
    **overrides: Any,
) -> Product:
    """Build an in-memory ``Product`` row with all Phase 3 counters defaulted."""
    data: dict[str, Any] = {
        "id": uuid.uuid4(),
        "platform": "tiki",
        "external_id": external_id,
        "title": title,
        "url": f"https://tiki.vn/san-pham-{external_id}.html",
        "price": Decimal("125000"),
        "currency": "VND",
        "sold_count": 50,
        "rating_avg": 4.6,
        "rating_count": 12,
        "description_text": None,
        "specs": {},
        "images": [],
        "raw": {},
        "shop_id": shop_id,
    }
    data.update(overrides)
    return Product(**data)


def make_shop_row(
    external_id: str = "seller-1",
    *,
    name: str = "Shop Test",
) -> Shop:
    """Build an in-memory ``Shop`` row."""
    return Shop(
        id=uuid.uuid4(),
        platform="tiki",
        external_id=external_id,
        name=name,
        url=f"https://tiki.vn/cua-hang/{external_id}",
        raw={},
    )


def make_source_row(
    *,
    url: str = "https://tiki.vn/san-pham-p1001.html",
    url_type: str = "product",
    status: str = "done",
    stats: dict[str, Any] | None = None,
    external_ref: dict[str, Any] | None = None,
    crawled_at: datetime | None = None,
) -> Source:
    """Build an in-memory ``Source`` row."""
    return Source(
        id=uuid.uuid4(),
        url=url,
        url_type=url_type,
        platform="tiki",
        status=status,
        stats=stats if stats is not None else {},
        external_ref=external_ref or {"product_id": "1001"},
        crawled_at=crawled_at or datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Minimal async-session doubles
# ---------------------------------------------------------------------------


class Rows:
    """Result double supporting ``.all()``, ``.scalars().all()`` and ``.one()``."""

    def __init__(self, rows: Sequence[Any] = (), one: Any = None) -> None:
        self._rows = list(rows)
        self._one = one

    def all(self) -> list[Any]:
        return list(self._rows)

    def scalars(self) -> Rows:
        return self

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def one(self) -> Any:
        return self._one


class ScriptedSession:
    """Async session double that returns pre-canned results in call order.

    Only suitable for functions with a *fixed* number of ``session.execute``
    calls (e.g. :func:`app.ingest.sentiment.analyze_product_reviews`). The SQL
    itself is exercised by the integration suite.
    """

    def __init__(self, results: Sequence[Any]) -> None:
        self._results = list(results)
        self.executed = 0
        self.flushes = 0
        self.added: list[Any] = []
        self.deleted: list[Any] = []

    async def execute(self, _stmt: Any) -> Any:
        if not self._results:
            raise AssertionError("ScriptedSession ran out of scripted results")
        self.executed += 1
        return self._results.pop(0)

    async def flush(self) -> None:
        self.flushes += 1

    async def commit(self) -> None:
        return None

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def delete(self, obj: Any) -> None:
        self.deleted.append(obj)

    async def get(self, _model: Any, _pk: Any) -> Any:
        return None


class ChunkSession:
    """Session double for :func:`app.ingest.chunking.sync_product_chunks`.

    The sync routine issues exactly one ``select(Chunk) WHERE product_id = ...``
    per product, so returning every stored chunk is equivalent for single-product
    tests.
    """

    def __init__(self, chunks: Sequence[Chunk] = ()) -> None:
        self.chunks: list[Chunk] = list(chunks)
        self.added: list[Chunk] = []
        self.deleted: list[Chunk] = []
        self.flushes = 0

    async def execute(self, _stmt: Any) -> Rows:
        return Rows(self.chunks)

    def add(self, obj: Chunk) -> None:
        self.added.append(obj)

    async def delete(self, obj: Chunk) -> None:
        self.deleted.append(obj)

    async def flush(self) -> None:
        self.flushes += 1


class SessionFactoryDouble:
    """``session_factory`` double yielding a pre-built session each call."""

    def __init__(self, session: Any) -> None:
        self.session = session
        self.opens = 0

    def __call__(self) -> SessionFactoryDouble:
        return self

    async def __aenter__(self) -> Any:
        self.opens += 1
        return self.session

    async def __aexit__(self, *_exc: Any) -> None:
        return None


# ---------------------------------------------------------------------------
# Redis doubles
# ---------------------------------------------------------------------------


class FakePubSub:
    """Minimal ``redis.asyncio`` pub/sub double fed by :class:`FakeRedis`."""

    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis
        self._channels: list[str] = []
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.closed = False
        self.unsubscribed: list[str] = []

    async def subscribe(self, *channels: str) -> None:
        for channel in channels:
            self._channels.append(channel)
            self._redis._subscribers[channel].append(self)

    async def get_message(
        self,
        *,
        ignore_subscribe_messages: bool = True,
        timeout: float | None = None,
    ) -> dict[str, Any] | None:
        _ = ignore_subscribe_messages
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except TimeoutError:
            return None

    async def unsubscribe(self, *channels: str) -> None:
        for channel in channels or tuple(self._channels):
            self.unsubscribed.append(channel)
            if self in self._redis._subscribers.get(channel, []):
                self._redis._subscribers[channel].remove(self)

    async def aclose(self) -> None:
        self.closed = True

    def deliver(self, channel: str, payload: str) -> None:
        self._queue.put_nowait({"type": "message", "channel": channel, "data": payload})


class FakeRedis:
    """In-memory subset of ``redis.asyncio.Redis`` used by ``JobEvents``."""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.lists: dict[str, list[str]] = defaultdict(list)
        self.counters: dict[str, int] = defaultdict(int)
        self.expiry: dict[str, int] = {}
        self.published: list[tuple[str, str]] = []
        self._subscribers: dict[str, list[FakePubSub]] = defaultdict(list)

    async def hset(self, key: str, *, mapping: dict[str, str] | None = None, **kwargs: Any) -> int:
        bucket = self.hashes.setdefault(key, {})
        payload = dict(mapping or {})
        payload.update({str(k): str(v) for k, v in kwargs.items()})
        new_fields = sum(1 for k in payload if k not in bucket)
        bucket.update(payload)
        return new_fields

    async def expire(self, key: str, ttl: int) -> bool:
        self.expiry[key] = ttl
        return True

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    async def incr(self, key: str) -> int:
        self.counters[key] += 1
        return self.counters[key]

    async def rpush(self, key: str, *values: str) -> int:
        self.lists[key].extend(values)
        return len(self.lists[key])

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        items = self.lists.get(key, [])
        if end == -1:
            return items[start:]
        return items[start : end + 1]

    async def lindex(self, key: str, index: int) -> str | None:
        items = self.lists.get(key, [])
        try:
            return items[index]
        except IndexError:
            return None

    async def publish(self, channel: str, message: str) -> int:
        self.published.append((channel, message))
        subscribers = list(self._subscribers.get(channel, []))
        for subscriber in subscribers:
            subscriber.deliver(channel, message)
        return len(subscribers)

    def deliver_raw(self, channel: str, message: dict[str, Any]) -> None:
        """Inject an arbitrary pub/sub frame (for malformed-message tests)."""
        for subscriber in list(self._subscribers.get(channel, [])):
            subscriber._queue.put_nowait(message)

    def pubsub(self) -> FakePubSub:
        return FakePubSub(self)

    async def aclose(self) -> None:
        return None


class RecordingJobEvents:
    """``JobEvents`` double that records everything without touching Redis."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.statuses: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self._status: dict[str, dict[str, Any]] = {}

    async def create_job(self, job_id: str, *, url: str = "", source_id: str | None = None) -> None:
        self.created.append({"job_id": job_id, "url": url, "source_id": source_id})
        self._status[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "source_id": source_id,
            "error": None,
            "url": url,
            "updated_at": None,
        }

    async def set_status(
        self,
        job_id: str,
        status: str,
        *,
        source_id: str | None = None,
        error: str | None = None,
    ) -> None:
        self.statuses.append(
            {"job_id": job_id, "status": status, "source_id": source_id, "error": error}
        )
        current = self._status.setdefault(
            job_id,
            {
                "job_id": job_id,
                "status": status,
                "source_id": source_id,
                "error": None,
                "url": None,
            },
        )
        current["status"] = status
        if source_id is not None:
            current["source_id"] = source_id
        if error is not None:
            current["error"] = error

    async def emit(
        self,
        job_id: str,
        *,
        stage: str,
        done: int = 0,
        total: int | None = None,
        message: str = "",
    ) -> dict[str, Any]:
        event = {
            "job_id": job_id,
            "seq": len(self.events) + 1,
            "stage": stage,
            "done": done,
            "total": total,
            "message": message,
        }
        self.events.append(event)
        return event

    async def get_status(self, job_id: str) -> dict[str, Any] | None:
        return self._status.get(job_id)

    async def last_event(self, job_id: str) -> dict[str, Any] | None:
        matches = [event for event in self.events if event["job_id"] == job_id]
        return matches[-1] if matches else None

    @property
    def stages(self) -> list[str]:
        return [event["stage"] for event in self.events]


class RecordingJobQueue:
    """``JobQueue`` double capturing ``enqueue_ingest`` payloads."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def enqueue_ingest(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(job_id=kwargs.get("job_id"))


class ErrorRedis:
    """Redis double whose writes fail (used for error-path assertions)."""

    def __init__(self, message: str = "redis down") -> None:
        self.message = message

    async def hset(self, *_args: Any, **_kwargs: Any) -> None:
        raise ConnectionError(self.message)

    async def expire(self, *_args: Any, **_kwargs: Any) -> None:
        raise ConnectionError(self.message)

    async def incr(self, *_args: Any, **_kwargs: Any) -> None:
        raise ConnectionError(self.message)

    async def rpush(self, *_args: Any, **_kwargs: Any) -> None:
        raise ConnectionError(self.message)

    async def publish(self, *_args: Any, **_kwargs: Any) -> None:
        raise ConnectionError(self.message)


# ---------------------------------------------------------------------------
# FastAPI app wiring for API tests
# ---------------------------------------------------------------------------


def build_ingest_app(
    *,
    session: Any,
    settings: Any,
    job_events: Any,
    job_queue: Any,
    resolver: Any = None,
    embedder: Any = None,
) -> Any:
    """Create the real app with every ingestion dependency replaced by a double."""
    from app.api.deps import (
        get_embedder_override,
        get_job_events,
        get_job_queue,
        get_session,
        get_settings,
        get_url_resolver,
    )
    from app.main import create_app

    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_job_events] = lambda: job_events
    app.dependency_overrides[get_job_queue] = lambda: job_queue
    app.dependency_overrides[get_url_resolver] = lambda: resolver
    app.dependency_overrides[get_embedder_override] = lambda: embedder
    return app
