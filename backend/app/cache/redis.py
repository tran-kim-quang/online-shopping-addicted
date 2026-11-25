"""Async Redis client wrapper (Singleton)."""

from __future__ import annotations

import redis.asyncio as aioredis

from app.core.singleton import SingletonMeta


class RedisClient(metaclass=SingletonMeta):
    """Singleton managing the shared async Redis connection pool."""

    def __init__(self) -> None:
        self._client: aioredis.Redis | None = None

    def init(self, url: str) -> None:
        """Create the Redis client from ``url``."""
        self._client = aioredis.from_url(url, decode_responses=True)

    @property
    def client(self) -> aioredis.Redis:
        if self._client is None:
            raise RuntimeError(
                "RedisClient is not initialized. Call RedisClient().init(url) first."
            )
        return self._client

    async def ping(self) -> bool:
        """Return True if Redis responds to PING."""
        if self._client is None:
            return False
        try:
            return bool(await self._client.ping())
        except Exception:
            return False

    async def close(self) -> None:
        """Close the Redis client and release connections."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
