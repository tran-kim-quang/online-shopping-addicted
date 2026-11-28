"""Redis-backed daily quota tracking for system (admin-shared) keys."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.cache.redis import RedisClient
from app.core.singleton import SingletonMeta

_USAGE_TTL_SECONDS = 3 * 24 * 60 * 60  # 3 days

_TRY_CONSUME_LUA = """
local key = KEYS[1]
local amount = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
local cur = tonumber(redis.call('GET', key) or '0')
if cur + amount > limit + 1e-9 then
    return 0
end
redis.call('INCRBYFLOAT', key, amount)
redis.call('EXPIRE', key, ttl)
return 1
"""


class SystemUsage(metaclass=SingletonMeta):
    """Singleton tracking daily UTC usage of admin-shared resources in Redis."""

    def __init__(self) -> None:
        self._redis_override: Any = None

    def set_redis(self, redis_client: Any) -> None:
        """Override the Redis client (primarily for unit tests)."""
        self._redis_override = redis_client

    @property
    def redis(self) -> Any:
        if self._redis_override is not None:
            return self._redis_override
        return RedisClient().client

    @staticmethod
    def usage_key(kind: str, *, now: datetime | None = None) -> str:
        """Return the Redis key ``usage:{kind}:{YYYY-MM-DD}`` in UTC."""
        dt = now.astimezone(UTC) if now is not None else datetime.now(UTC)
        return f"usage:{kind}:{dt.strftime('%Y-%m-%d')}"

    async def used(self, kind: str, *, now: datetime | None = None) -> float:
        """Return today's accumulated usage for ``kind``."""
        raw = await self.redis.get(self.usage_key(kind, now=now))
        if raw is None:
            return 0.0
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0

    async def add(self, kind: str, amount: float, *, now: datetime | None = None) -> float:
        """Add ``amount`` to today's usage for ``kind`` and refresh the 3-day TTL."""
        if amount <= 0:
            return await self.used(kind, now=now)
        key = self.usage_key(kind, now=now)
        new_val = float(await self.redis.incrbyfloat(key, float(amount)))
        await self.redis.expire(key, _USAGE_TTL_SECONDS)
        return new_val

    async def try_consume(
        self,
        kind: str,
        amount: float,
        limit: float | None,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Atomically consume ``amount`` if ``used + amount <= limit`` (or ``limit is None``)."""
        if limit is None:
            await self.add(kind, amount, now=now)
            return True

        key = self.usage_key(kind, now=now)
        eval_fn = getattr(self.redis, "eval", None)
        if callable(eval_fn):
            res = await eval_fn(
                _TRY_CONSUME_LUA,
                1,
                key,
                str(float(amount)),
                str(float(limit)),
                str(_USAGE_TTL_SECONDS),
            )
            return bool(int(res or 0))

        # Fallback for fake Redis clients without Lua ``eval`` support.
        new_val = float(await self.redis.incrbyfloat(key, float(amount)))
        await self.redis.expire(key, _USAGE_TTL_SECONDS)
        if new_val > float(limit) + 1e-9:
            await self.redis.incrbyfloat(key, -float(amount))
            return False
        return True
