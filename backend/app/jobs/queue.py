"""Arq job queue client wrapper (Singleton)."""

from __future__ import annotations

import asyncio
from typing import Any

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from app.core.config import get_settings
from app.core.singleton import SingletonMeta


class JobQueue(metaclass=SingletonMeta):
    """Singleton managing the shared Arq Redis enqueue pool."""

    def __init__(self) -> None:
        self._redis_url: str | None = None
        self._pool: ArqRedis | None = None
        self._lock = asyncio.Lock()

    def init(self, redis_url: str) -> None:
        """Configure the Redis URL used for creating the Arq pool."""
        self._redis_url = redis_url

    async def get_pool(self) -> ArqRedis:
        if self._pool is not None:
            return self._pool
        async with self._lock:
            if self._pool is None:
                url = self._redis_url
                if not url:
                    url = get_settings().REDIS_URL
                self._pool = await create_pool(RedisSettings.from_dsn(url))
        return self._pool

    async def enqueue_ingest(
        self,
        *,
        job_id: str,
        url: str,
        llm_profile_id: str | None = None,
        max_products: int | None = None,
        max_reviews: int | None = None,
        data_provider_profile_id: str | None = None,
    ) -> Any:
        """Enqueue ``run_ingest`` on the Arq Redis queue."""
        pool = await self.get_pool()
        args: list[Any] = [job_id, url, llm_profile_id, max_products, max_reviews]
        if data_provider_profile_id is not None:
            args.append(data_provider_profile_id)
        return await pool.enqueue_job(
            "run_ingest",
            *args,
            _job_id=job_id,
        )

    async def enqueue_analyze(
        self,
        *,
        job_id: str,
        source_id: str,
        llm_profile_id: str,
    ) -> Any:
        """Enqueue ``run_analyze`` on the Arq Redis queue."""
        pool = await self.get_pool()
        return await pool.enqueue_job(
            "run_analyze",
            job_id,
            source_id,
            llm_profile_id,
            _job_id=job_id,
        )

    async def enqueue_trend_search(
        self,
        *,
        job_id: str,
        trend_search_id: str,
    ) -> Any:
        """Enqueue ``run_trend_search`` on the Arq Redis queue."""
        pool = await self.get_pool()
        return await pool.enqueue_job(
            "run_trend_search",
            job_id,
            trend_search_id,
            _job_id=job_id,
        )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.aclose()
            self._pool = None
