"""Redis-backed job status hash, event log (RPUSH), and live pub/sub stream for SSE."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as aioredis

from app.cache.redis import RedisClient

logger = logging.getLogger(__name__)

JOB_TTL_SECONDS = 86_400  # 24 hours
TERMINAL_JOB_STATUSES = frozenset({"done", "partial", "failed"})


def _decode(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _decode_mapping(raw: dict[Any, Any]) -> dict[str, str]:
    return {_decode(k): _decode(v) for k, v in raw.items()}


class JobEvents:
    """Publishes and replays job progress events using the ``RedisClient`` singleton.

    Redis key schema:
      - ``job:{id}`` (Hash, TTL 24h): ``status``, ``source_id``, ``error``, ``url``, ``updated_at``
      - ``job:{id}:events`` (List, TTL 24h): JSON-encoded event dicts with monotonic ``seq``
      - ``job:{id}:seq`` (String counter, TTL 24h): atomic sequence allocator
      - ``job:{id}`` (Pub/Sub channel): live event broadcasts
    """

    def __init__(
        self,
        redis_client: aioredis.Redis | None = None,
        *,
        ttl_seconds: int = JOB_TTL_SECONDS,
    ) -> None:
        self._redis = redis_client
        self._ttl_seconds = ttl_seconds

    @property
    def redis(self) -> aioredis.Redis:
        if self._redis is not None:
            return self._redis
        return RedisClient().client

    @staticmethod
    def status_key(job_id: str) -> str:
        return f"job:{job_id}"

    @staticmethod
    def events_key(job_id: str) -> str:
        return f"job:{job_id}:events"

    @staticmethod
    def seq_key(job_id: str) -> str:
        return f"job:{job_id}:seq"

    @staticmethod
    def channel_name(job_id: str) -> str:
        return f"job:{job_id}"

    async def create_job(
        self,
        job_id: str,
        *,
        url: str = "",
        source_id: str | None = None,
    ) -> dict[str, str]:
        """Initialize the job status hash in ``queued`` state."""
        mapping = {
            "status": "queued",
            "source_id": source_id or "",
            "error": "",
            "url": url,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        key = self.status_key(job_id)
        await self.redis.hset(key, mapping=mapping)
        await self.redis.expire(key, self._ttl_seconds)
        return mapping

    async def set_status(
        self,
        job_id: str,
        status: str,
        *,
        source_id: str | None = None,
        error: str | None = None,
    ) -> None:
        """Update job status hash fields and refresh TTL."""
        updates: dict[str, str] = {
            "status": status,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        if source_id is not None:
            updates["source_id"] = source_id
        if error is not None:
            updates["error"] = error
        key = self.status_key(job_id)
        await self.redis.hset(key, mapping=updates)
        await self.redis.expire(key, self._ttl_seconds)

    async def get_status(self, job_id: str) -> dict[str, Any] | None:
        """Return the job status dict, or ``None`` if the job does not exist."""
        raw = await self.redis.hgetall(self.status_key(job_id))
        if not raw:
            return None
        data = _decode_mapping(raw)
        return {
            "job_id": job_id,
            "status": data.get("status", "queued"),
            "source_id": data.get("source_id") or None,
            "error": data.get("error") or None,
            "url": data.get("url") or None,
            "updated_at": data.get("updated_at") or None,
        }

    async def emit(
        self,
        job_id: str,
        *,
        stage: str,
        done: int = 0,
        total: int | None = None,
        message: str = "",
    ) -> dict[str, Any]:
        """Append an event to ``job:{id}:events`` (RPUSH, TTL 24h) and PUBLISH on ``job:{id}``."""
        seq_key = self.seq_key(job_id)
        seq = int(await self.redis.incr(seq_key))
        await self.redis.expire(seq_key, self._ttl_seconds)

        event: dict[str, Any] = {
            "seq": seq,
            "stage": stage,
            "done": done,
            "total": total,
            "message": message,
            "ts": datetime.now(UTC).isoformat(),
        }
        payload = json.dumps(event, ensure_ascii=False)
        ev_key = self.events_key(job_id)
        await self.redis.rpush(ev_key, payload)
        await self.redis.expire(ev_key, self._ttl_seconds)
        await self.redis.publish(self.channel_name(job_id), payload)
        return event

    async def list_events(self, job_id: str, *, after_seq: int = 0) -> list[dict[str, Any]]:
        """Return persisted events with ``seq > after_seq`` in ascending order."""
        raw_items = await self.redis.lrange(self.events_key(job_id), 0, -1)
        events: list[dict[str, Any]] = []
        for raw in raw_items:
            try:
                parsed = json.loads(_decode(raw))
            except Exception:
                continue
            if isinstance(parsed, dict) and int(parsed.get("seq", 0)) > after_seq:
                events.append(parsed)
        return events

    async def last_event(self, job_id: str) -> dict[str, Any] | None:
        """Return the most recent event for ``job_id``, or ``None``."""
        raw = await self.redis.lindex(self.events_key(job_id), -1)
        if raw is None:
            return None
        try:
            parsed = json.loads(_decode(raw))
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None

    async def _terminal_tuple(
        self,
        job_id: str,
        status_info: dict[str, Any],
        last_seq: int,
    ) -> tuple[str, dict[str, Any]]:
        job_status = status_info.get("status", "done")
        event_name = "error" if job_status == "failed" else "done"
        payload = {
            "seq": last_seq,
            "job_id": job_id,
            "status": job_status,
            "source_id": status_info.get("source_id"),
            "error": status_info.get("error"),
        }
        return event_name, payload

    async def stream(
        self,
        job_id: str,
        *,
        last_event_id: int = 0,
        keepalive_seconds: float = 15.0,
    ) -> AsyncIterator[tuple[str, dict[str, Any] | None]]:
        """Replay missed events then follow pub/sub until job reaches a terminal state.

        Yields tuples of ``(sse_event_name, data_dict_or_none)`` where:
          - ``("progress", event_dict)`` is a normal progress event
          - ``("keepalive", None)`` signals an SSE keepalive comment
          - ``("done", summary_dict)`` or ``("error", summary_dict)`` ends the stream
        """
        pubsub = self.redis.pubsub()
        channel = self.channel_name(job_id)
        await pubsub.subscribe(channel)
        last_sent_seq = max(0, last_event_id)

        try:
            # 1. Replay persisted events after subscribing so no live messages are lost.
            history = await self.list_events(job_id, after_seq=last_sent_seq)
            for ev in history:
                last_sent_seq = max(last_sent_seq, int(ev.get("seq", 0)))
                yield ("progress", ev)

            status_info = await self.get_status(job_id)
            if status_info is None:
                yield (
                    "error",
                    {
                        "seq": last_sent_seq,
                        "job_id": job_id,
                        "status": "failed",
                        "source_id": None,
                        "error": "Job not found.",
                    },
                )
                return

            if status_info["status"] in TERMINAL_JOB_STATUSES:
                yield await self._terminal_tuple(job_id, status_info, last_sent_seq)
                return

            # 2. Follow live pub/sub events until terminal stage/status.
            while True:
                msg = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=keepalive_seconds,
                )
                if msg is None:
                    current = await self.get_status(job_id)
                    if current is not None and current["status"] in TERMINAL_JOB_STATUSES:
                        # Drain any final event written before status became terminal.
                        trailing = await self.list_events(job_id, after_seq=last_sent_seq)
                        for ev in trailing:
                            last_sent_seq = max(last_sent_seq, int(ev.get("seq", 0)))
                            yield ("progress", ev)
                        yield await self._terminal_tuple(job_id, current, last_sent_seq)
                        return
                    yield ("keepalive", None)
                    continue

                if msg.get("type") != "message":
                    continue

                try:
                    ev = json.loads(_decode(msg["data"]))
                except Exception:
                    continue
                if not isinstance(ev, dict):
                    continue

                ev_seq = int(ev.get("seq", 0))
                if ev_seq > last_sent_seq:
                    last_sent_seq = ev_seq
                    yield ("progress", ev)

                if ev.get("stage") in TERMINAL_JOB_STATUSES:
                    current = await self.get_status(job_id) or {
                        "status": ev.get("stage"),
                        "source_id": None,
                        "error": ev.get("message") if ev.get("stage") == "failed" else None,
                    }
                    yield await self._terminal_tuple(job_id, current, last_sent_seq)
                    return
        finally:
            try:
                await pubsub.unsubscribe(channel)
            except Exception:
                pass
            try:
                await pubsub.aclose()
            except Exception:
                pass
