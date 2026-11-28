"""Redis-backed job status hash, heartbeat/stale detection, event log, and SSE stream."""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.redis import RedisClient
from app.core.config import get_settings
from app.core.errors import JobCancelledError

logger = logging.getLogger(__name__)

JOB_TTL_SECONDS = 86_400  # 24 hours
ANALYZE_LOCK_TTL_SECONDS = 1_800  # 30 minutes (matches worker job_timeout)
TERMINAL_JOB_STATUSES = frozenset({"done", "partial", "failed"})

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

_UNSET_SENTINEL = "__UNSET__"


# Atomic compare-and-set: never overwrite a terminal status (done/partial/failed).
_SET_STATUS_LUA = """
local st = redis.call('HGET', KEYS[1], 'status')
if st == 'done' or st == 'partial' or st == 'failed' then
    return 0
end
redis.call(
    'HSET', KEYS[1],
    'status', ARGV[1],
    'updated_at', ARGV[2],
    'heartbeat_at', ARGV[2]
)
if ARGV[3] ~= '__UNSET__' then
    redis.call('HSET', KEYS[1], 'source_id', ARGV[3])
end
if ARGV[4] ~= '__UNSET__' then
    redis.call('HSET', KEYS[1], 'error', ARGV[4])
end
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[5]))
return 1
"""

# Atomic compare-and-set: only mark a job failed if it exists and is NOT already terminal.
_MARK_STALE_LUA = """
local st = redis.call('HGET', KEYS[1], 'status')
if not st or st == 'done' or st == 'partial' or st == 'failed' then
    return 0
end
redis.call(
    'HSET', KEYS[1],
    'status', 'failed',
    'error', ARGV[1],
    'updated_at', ARGV[2],
    'heartbeat_at', ARGV[2]
)
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]))
return 1
"""


# Atomic compare-and-set: only delete a per-source lock if it still holds our job_id.
_RELEASE_LOCK_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


def _decode(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _decode_mapping(raw: dict[Any, Any]) -> dict[str, str]:
    return {_decode(k): _decode(v) for k, v in raw.items()}


def _parse_iso_datetime(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        return None


def _default_timeouts() -> tuple[float, float, float]:
    try:
        settings = get_settings()
        return (
            float(settings.JOB_STALE_TIMEOUT_SECONDS),
            float(settings.JOB_QUEUED_TIMEOUT_SECONDS),
            float(settings.JOB_STREAM_MAX_SECONDS),
        )
    except Exception:
        return 120.0, 600.0, 3600.0


def extract_error_code(error: str | None) -> str | None:
    """Extract stable error code prefix from ``'<code>: <message>'`` when present."""
    if not error:
        return None
    trimmed = error.strip()
    if ":" not in trimmed:
        return None
    prefix = trimmed.split(":", 1)[0].strip().lower()
    if prefix and prefix.replace("_", "").isalnum():
        return prefix
    return None


class JobEvents:
    """Publishes and replays job progress events using the ``RedisClient`` singleton.

    Redis key schema:
      - ``job:{id}`` (Hash, TTL 24h): ``status``, ``source_id``, ``error``, ``url``,
        ``created_at``, ``updated_at``, ``heartbeat_at``
      - ``job:{id}:events`` (List, TTL 24h): JSON-encoded event dicts with monotonic ``seq``
      - ``job:{id}:seq`` (String counter, TTL 24h): atomic sequence allocator
      - ``job:{id}`` (Pub/Sub channel): live event broadcasts
    """

    def __init__(
        self,
        redis_client: aioredis.Redis | None = None,
        *,
        ttl_seconds: int = JOB_TTL_SECONDS,
        session_factory: SessionFactory | None = None,
        now_fn: Callable[[], datetime] | None = None,
        monotonic_fn: Callable[[], float] | None = None,
    ) -> None:
        self._redis = redis_client
        self._ttl_seconds = ttl_seconds
        self._session_factory = session_factory
        self._now: Callable[[], datetime] = (
            now_fn if now_fn is not None else lambda: datetime.now(UTC)
        )
        self._monotonic: Callable[[], float] = (
            monotonic_fn if monotonic_fn is not None else time.monotonic
        )

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

    @staticmethod
    def analyze_lock_key(source_id: str) -> str:
        return f"analyze:lock:{source_id}"

    @staticmethod
    def trend_lock_key(user_id: str, keyword_norm: str) -> str:
        return f"trend:lock:{user_id}:{keyword_norm}"

    async def get_trend_lock(self, user_id: str, keyword_norm: str) -> str | None:
        """Return the ``job_id`` currently holding the trend search lock, or None."""
        raw = await self.redis.get(self.trend_lock_key(user_id, keyword_norm))
        if raw is None:
            return None
        decoded = _decode(raw).strip()
        return decoded or None

    async def acquire_trend_lock(
        self,
        user_id: str,
        keyword_norm: str,
        job_id: str,
        *,
        ttl_seconds: int = ANALYZE_LOCK_TTL_SECONDS,
    ) -> tuple[bool, str | None]:
        """Atomically acquire the per-(user, keyword_norm) trend search lock."""
        key = self.trend_lock_key(user_id, keyword_norm)
        acquired = await self.redis.set(key, job_id, nx=True, ex=ttl_seconds)
        if acquired:
            return True, job_id
        holder = await self.get_trend_lock(user_id, keyword_norm)
        return False, holder

    async def release_trend_lock(self, user_id: str, keyword_norm: str, job_id: str) -> bool:
        """Atomically release the trend search lock only if held by ``job_id``."""
        key = self.trend_lock_key(user_id, keyword_norm)
        eval_fn = getattr(self.redis, "eval", None)
        if callable(eval_fn):
            deleted_raw = await eval_fn(_RELEASE_LOCK_LUA, 1, key, job_id)
            return bool(int(deleted_raw or 0))
        holder = await self.get_trend_lock(user_id, keyword_norm)
        if holder != job_id:
            return False
        delete_fn = getattr(self.redis, "delete", None)
        if callable(delete_fn):
            return bool(await delete_fn(key))
        return False

    async def get_source_lock(self, source_id: str) -> str | None:
        """Return the ``job_id`` currently holding the analyze lock for ``source_id``, or None."""
        raw = await self.redis.get(self.analyze_lock_key(source_id))
        if raw is None:
            return None
        decoded = _decode(raw).strip()
        return decoded or None

    async def acquire_source_lock(
        self,
        source_id: str,
        job_id: str,
        *,
        ttl_seconds: int = ANALYZE_LOCK_TTL_SECONDS,
    ) -> tuple[bool, str | None]:
        """Atomically acquire the per-source analyze lock (``SET NX EX``).

        Returns ``(True, job_id)`` when acquired, or ``(False, holder_job_id)`` when
        another job already holds the lock.
        """
        key = self.analyze_lock_key(source_id)
        acquired = await self.redis.set(key, job_id, nx=True, ex=ttl_seconds)
        if acquired:
            return True, job_id
        holder = await self.get_source_lock(source_id)
        return False, holder

    async def release_source_lock(self, source_id: str, job_id: str) -> bool:
        """Atomically release the per-source analyze lock only if held by ``job_id``."""
        key = self.analyze_lock_key(source_id)
        eval_fn = getattr(self.redis, "eval", None)
        if callable(eval_fn):
            deleted_raw = await eval_fn(_RELEASE_LOCK_LUA, 1, key, job_id)
            return bool(int(deleted_raw or 0))

        # Fallback for in-memory test doubles without Lua eval.
        holder = await self.get_source_lock(source_id)
        if holder != job_id:
            return False
        delete_fn = getattr(self.redis, "delete", None)
        if callable(delete_fn):
            return bool(await delete_fn(key))
        return False

    async def create_job(
        self,
        job_id: str,
        *,
        url: str = "",
        source_id: str | None = None,
        job_type: str | None = None,
    ) -> dict[str, str]:
        """Initialize the job status hash in ``queued`` state."""
        now_iso = self._now().isoformat()
        mapping = {
            "status": "queued",
            "source_id": source_id or "",
            "error": "",
            "url": url,
            "created_at": now_iso,
            "updated_at": now_iso,
            "heartbeat_at": now_iso,
        }
        if job_type is not None:
            mapping["job_type"] = job_type
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
    ) -> bool:
        """Atomically update job status hash fields if the job is not already terminal.

        Returns ``True`` if the status was updated, or ``False`` if the job was already
        in a terminal state (``done``/``partial``/``failed``) and therefore left untouched.
        """
        now_iso = self._now().isoformat()
        key = self.status_key(job_id)

        eval_fn = getattr(self.redis, "eval", None)
        if callable(eval_fn):
            updated_raw = await eval_fn(
                _SET_STATUS_LUA,
                1,
                key,
                status,
                now_iso,
                source_id if source_id is not None else _UNSET_SENTINEL,
                error if error is not None else _UNSET_SENTINEL,
                str(self._ttl_seconds),
            )
            return bool(int(updated_raw or 0))

        # Fallback for in-memory test doubles without Lua eval (e.g. FakeRedis).
        raw = await self.redis.hgetall(key)
        if raw:
            data = _decode_mapping(raw)
            if data.get("status") in TERMINAL_JOB_STATUSES:
                return False

        updates: dict[str, str] = {
            "status": status,
            "updated_at": now_iso,
            "heartbeat_at": now_iso,
        }
        if source_id is not None:
            updates["source_id"] = source_id
        if error is not None:
            updates["error"] = error
        await self.redis.hset(key, mapping=updates)
        await self.redis.expire(key, self._ttl_seconds)
        return True

    async def touch_heartbeat(self, job_id: str) -> bool:
        """Refresh ``heartbeat_at`` for a non-terminal job.

        Raises :class:`JobCancelledError` if the job has already reached a terminal state
        so a slow worker detects external timeout/cancellation on the next heartbeat tick.
        """
        key = self.status_key(job_id)
        raw = await self.redis.hgetall(key)
        if not raw:
            return False
        data = _decode_mapping(raw)
        current_status = data.get("status", "")
        if current_status in TERMINAL_JOB_STATUSES:
            raise JobCancelledError(
                job_id,
                status=current_status,
                error=data.get("error") or None,
            )
        now_iso = self._now().isoformat()
        await self.redis.hset(key, mapping={"heartbeat_at": now_iso})
        await self.redis.expire(key, self._ttl_seconds)
        return True

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
            "created_at": data.get("created_at") or None,
            "updated_at": data.get("updated_at") or None,
            "heartbeat_at": data.get("heartbeat_at") or None,
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
        """Append an event to ``job:{id}:events`` (RPUSH, TTL 24h) and PUBLISH on ``job:{id}``.

        Raises :class:`JobCancelledError` without publishing if the job is already in a
        terminal state that does not match ``stage`` (preventing a slow worker from writing
        events after ``mark_stale`` has already marked the job ``failed``).
        """
        st_key = self.status_key(job_id)
        raw_status = await self.redis.hgetall(st_key)
        current_status: str | None = None
        if raw_status:
            status_data = _decode_mapping(raw_status)
            current_status = status_data.get("status")
            if current_status in TERMINAL_JOB_STATUSES and stage != current_status:
                raise JobCancelledError(
                    job_id,
                    status=current_status,
                    error=status_data.get("error") or None,
                )

        seq_key = self.seq_key(job_id)
        seq = int(await self.redis.incr(seq_key))
        await self.redis.expire(seq_key, self._ttl_seconds)

        now_iso = self._now().isoformat()
        event: dict[str, Any] = {
            "seq": seq,
            "stage": stage,
            "done": done,
            "total": total,
            "message": message,
            "ts": now_iso,
        }
        payload = json.dumps(event, ensure_ascii=False)
        ev_key = self.events_key(job_id)
        await self.redis.rpush(ev_key, payload)
        await self.redis.expire(ev_key, self._ttl_seconds)

        # Keep heartbeat fresh whenever progress is emitted for an active (non-terminal) job.
        if raw_status and current_status not in TERMINAL_JOB_STATUSES:
            await self.redis.hset(st_key, mapping={"heartbeat_at": now_iso})
            await self.redis.expire(st_key, self._ttl_seconds)

        await self.redis.publish(self.channel_name(job_id), payload)
        return event

    async def _mark_source_failed_on_timeout(self, source_id_str: str, error: str) -> None:
        """Best-effort update of ``sources.status='failed'`` when a job times out."""
        try:
            source_uuid = uuid.UUID(source_id_str)
        except ValueError:
            logger.warning(
                "Skipping source timeout update for invalid source_id '%s'.", source_id_str
            )
            return

        try:
            if self._session_factory is not None:
                factory = self._session_factory
            else:
                from app.db.database import Database

                factory = Database().session_factory

            from app.db.models import Source

            async with factory() as session:
                source = await session.get(Source, source_uuid)
                if source is not None:
                    source.status = "failed"
                    source.error = error
                    source.updated_at = self._now()
                    await session.commit()
        except Exception as exc:
            logger.warning(
                "Failed updating source %s to failed after worker_timeout: %s",
                source_id_str,
                exc,
            )

    async def mark_stale(
        self,
        job_id: str,
        *,
        error: str = "worker_timeout: worker heartbeat expired",
    ) -> tuple[bool, dict[str, Any] | None]:
        """Idempotently transition a non-terminal job to ``failed`` with ``worker_timeout``.

        Uses an atomic Redis Lua compare-and-set when available so a job that just
        finished concurrently (``done``/``partial``/``failed``) is never overwritten.
        Returns ``(transitioned, latest_status_info)``.
        """
        key = self.status_key(job_id)
        now_iso = self._now().isoformat()

        eval_fn = getattr(self.redis, "eval", None)
        if callable(eval_fn):
            changed_raw = await eval_fn(
                _MARK_STALE_LUA,
                1,
                key,
                error,
                now_iso,
                str(self._ttl_seconds),
            )
            transitioned = bool(int(changed_raw or 0))
        else:
            raw = await self.redis.hgetall(key)
            if not raw:
                return False, None
            data = _decode_mapping(raw)
            if data.get("status") in TERMINAL_JOB_STATUSES:
                return False, await self.get_status(job_id)
            await self.redis.hset(
                key,
                mapping={
                    "status": "failed",
                    "error": error,
                    "updated_at": now_iso,
                    "heartbeat_at": now_iso,
                },
            )
            await self.redis.expire(key, self._ttl_seconds)
            transitioned = True

        latest = await self.get_status(job_id)
        if transitioned:
            await self.emit(job_id, stage="failed", done=0, total=None, message=error)
            raw_hash = await self.redis.hgetall(key)
            job_type = _decode_mapping(raw_hash).get("job_type") if raw_hash else None
            if latest and latest.get("source_id") and job_type != "analyze":
                await self._mark_source_failed_on_timeout(str(latest["source_id"]), error)
        return transitioned, latest

    async def check_stale(
        self,
        job_id: str,
        *,
        stale_timeout_seconds: float | None = None,
        queued_timeout_seconds: float | None = None,
    ) -> dict[str, Any] | None:
        """Inspect job timestamps and call :meth:`mark_stale` if the job is stale."""
        info = await self.get_status(job_id)
        if info is None or info["status"] in TERMINAL_JOB_STATUSES:
            return info

        default_stale, default_queued, _ = _default_timeouts()
        eff_stale = (
            float(stale_timeout_seconds) if stale_timeout_seconds is not None else default_stale
        )
        eff_queued = (
            float(queued_timeout_seconds) if queued_timeout_seconds is not None else default_queued
        )

        now = self._now()
        status_val = info["status"]

        if status_val == "running" and eff_stale > 0:
            ref_dt = (
                _parse_iso_datetime(info.get("heartbeat_at"))
                or _parse_iso_datetime(info.get("updated_at"))
                or _parse_iso_datetime(info.get("created_at"))
            )
            if ref_dt is not None and (now - ref_dt).total_seconds() > eff_stale:
                reason = f"worker_timeout: no heartbeat for >{eff_stale:g}s"
                _, latest = await self.mark_stale(job_id, error=reason)
                return latest
        elif status_val == "queued" and eff_queued > 0:
            ref_dt = (
                _parse_iso_datetime(info.get("updated_at"))
                or _parse_iso_datetime(info.get("created_at"))
                or _parse_iso_datetime(info.get("heartbeat_at"))
            )
            if ref_dt is not None and (now - ref_dt).total_seconds() > eff_queued:
                reason = f"worker_timeout: job stayed queued for >{eff_queued:g}s"
                _, latest = await self.mark_stale(job_id, error=reason)
                return latest

        return info

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
        err = status_info.get("error")
        payload: dict[str, Any] = {
            "seq": last_seq,
            "job_id": job_id,
            "status": job_status,
            "source_id": status_info.get("source_id"),
            "error": err,
        }
        err_code = extract_error_code(err) if job_status == "failed" else None
        if err_code is not None:
            payload["error_code"] = err_code
        return event_name, payload

    async def stream(
        self,
        job_id: str,
        *,
        last_event_id: int = 0,
        keepalive_seconds: float = 15.0,
        stale_timeout_seconds: float | None = None,
        queued_timeout_seconds: float | None = None,
        max_stream_seconds: float | None = None,
    ) -> AsyncIterator[tuple[str, dict[str, Any] | None]]:
        """Replay missed events then follow pub/sub until job reaches a terminal state.

        Yields tuples of ``(sse_event_name, data_dict_or_none)`` where:
          - ``("progress", event_dict)`` is a normal progress event
          - ``("keepalive", None)`` signals an SSE keepalive comment
          - ``("timeout", info_dict)`` closes the connection at ``JOB_STREAM_MAX_SECONDS``
            without marking the job failed
          - ``("done", summary_dict)`` or ``("error", summary_dict)`` ends the stream
        """
        default_stale, default_queued, default_max_stream = _default_timeouts()
        eff_stale = (
            float(stale_timeout_seconds) if stale_timeout_seconds is not None else default_stale
        )
        eff_queued = (
            float(queued_timeout_seconds) if queued_timeout_seconds is not None else default_queued
        )
        eff_max_stream = (
            float(max_stream_seconds) if max_stream_seconds is not None else default_max_stream
        )

        stream_started = self._monotonic()
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

            status_info = await self.check_stale(
                job_id,
                stale_timeout_seconds=eff_stale,
                queued_timeout_seconds=eff_queued,
            )
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
                trailing = await self.list_events(job_id, after_seq=last_sent_seq)
                for ev in trailing:
                    last_sent_seq = max(last_sent_seq, int(ev.get("seq", 0)))
                    yield ("progress", ev)
                yield await self._terminal_tuple(job_id, status_info, last_sent_seq)
                return

            last_known_status = status_info["status"]

            # 2. Follow live pub/sub events until terminal stage/status or stream cap.
            while True:
                elapsed = self._monotonic() - stream_started
                if eff_max_stream > 0 and elapsed >= eff_max_stream:
                    yield (
                        "timeout",
                        {
                            "seq": last_sent_seq,
                            "job_id": job_id,
                            "status": last_known_status,
                            "message": (
                                "Stream max duration reached; reconnect with Last-Event-ID."
                            ),
                        },
                    )
                    return

                wait_timeout = keepalive_seconds
                if eff_max_stream > 0:
                    remaining = max(0.001, eff_max_stream - elapsed)
                    wait_timeout = min(keepalive_seconds, remaining)

                msg = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=wait_timeout,
                )
                if msg is None:
                    if (
                        eff_max_stream > 0
                        and (self._monotonic() - stream_started) >= eff_max_stream
                    ):
                        yield (
                            "timeout",
                            {
                                "seq": last_sent_seq,
                                "job_id": job_id,
                                "status": last_known_status,
                                "message": (
                                    "Stream max duration reached; reconnect with Last-Event-ID."
                                ),
                            },
                        )
                        return

                    current = await self.check_stale(
                        job_id,
                        stale_timeout_seconds=eff_stale,
                        queued_timeout_seconds=eff_queued,
                    )
                    if current is not None:
                        last_known_status = current["status"]
                        if current["status"] in TERMINAL_JOB_STATUSES:
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
