"""Arq worker settings and process lifecycle hooks."""

from __future__ import annotations

import logging
import os
from typing import Any

from arq.connections import RedisSettings

from app.cache.redis import RedisClient
from app.core.config import get_settings
from app.db.database import Database
from app.jobs.tasks import run_analyze, run_ingest
from app.llm.registry import LLMClientRegistry
from app.security.secrets import SecretBox

logger = logging.getLogger(__name__)


def _resolve_redis_settings() -> RedisSettings:
    try:
        redis_url = get_settings().REDIS_URL
    except Exception:
        redis_url = os.environ.get("REDIS_URL", "redis://localhost:6380/0")
    return RedisSettings.from_dsn(redis_url)


async def on_startup(ctx: dict[str, Any]) -> None:
    """Initialize process-wide singletons inside the Arq worker process."""
    settings = get_settings()
    logging.basicConfig(level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO))

    Database().init(settings.DATABASE_URL)
    RedisClient().init(settings.REDIS_URL)
    SecretBox().init(settings.SECRET_KEY)
    LLMClientRegistry().configure(max_clients=settings.LLM_REGISTRY_MAX_CLIENTS)

    ctx["settings"] = settings
    logger.info("Arq worker initialized (embedding_backend=%s).", settings.EMBEDDING_BACKEND)


async def on_shutdown(ctx: dict[str, Any]) -> None:
    """Close process-wide singletons cleanly on worker shutdown."""
    _ = ctx
    await LLMClientRegistry().aclose_all()
    await RedisClient().close()
    await Database().dispose()
    logger.info("Arq worker shut down cleanly.")


class WorkerSettings:
    """Configuration consumed by ``arq app.jobs.worker.WorkerSettings``."""

    functions = [run_ingest, run_analyze]
    redis_settings = _resolve_redis_settings()
    on_startup = on_startup
    on_shutdown = on_shutdown
    max_jobs = 4
    job_timeout = 1800
    keep_result = 3600
