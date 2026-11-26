"""Unit tests for the Arq worker settings/lifecycle (``app.jobs.worker``)."""

from __future__ import annotations

from typing import Any

import pytest

import app.jobs.worker as worker_module
from app.core.config import Settings
from app.jobs.tasks import run_analyze, run_ingest
from app.jobs.worker import WorkerSettings, _resolve_redis_settings, on_shutdown, on_startup

SECRET = "vN5y6rM0J9h2xL8qK3pW1zB4cF7dG0tH2sV5mN8wX4Q="


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "SECRET_KEY": SECRET,
        "DATABASE_URL": "postgresql+asyncpg://shopqa:shopqa@localhost:5433/shopqa_phase3_test",
        "REDIS_URL": "redis://localhost:6380/15",
        "LOG_LEVEL": "WARNING",
    }
    values.update(overrides)
    return Settings(**values)


def test_worker_settings_register_the_ingest_and_analyze_tasks() -> None:
    """``run_analyze`` must be registered, otherwise the API can enqueue a job nobody runs.

    The analyze endpoint holds a per-source Redis lock and only releases it from the task's
    ``finally`` block, so an unregistered function would block the source until the lock TTL.
    """
    assert WorkerSettings.functions == [run_ingest, run_analyze]
    assert WorkerSettings.max_jobs >= 1
    assert WorkerSettings.job_timeout > 0
    assert WorkerSettings.keep_result > 0
    assert WorkerSettings.on_startup is on_startup
    assert WorkerSettings.on_shutdown is on_shutdown


def test_redis_settings_come_from_the_application_settings() -> None:
    settings = _resolve_redis_settings()

    assert settings.host == "localhost"
    assert settings.port == 6380
    assert settings.database == 0  # conftest exports redis://localhost:6380/0


def test_redis_settings_fall_back_to_the_environment(monkeypatch: Any) -> None:
    def _boom() -> Any:
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(worker_module, "get_settings", _boom)
    monkeypatch.setenv("REDIS_URL", "redis://cache.internal:6399/4")

    settings = _resolve_redis_settings()

    assert settings.host == "cache.internal"
    assert settings.port == 6399
    assert settings.database == 4


async def test_worker_startup_and_shutdown_wire_the_singletons(monkeypatch: Any) -> None:
    from app.cache.redis import RedisClient
    from app.db.database import Database

    monkeypatch.setattr(worker_module, "get_settings", make_settings)
    ctx: dict[str, Any] = {}

    await on_startup(ctx)

    assert isinstance(ctx["settings"], Settings)
    assert RedisClient().ping is not None
    assert Database()._engine is not None  # type: ignore[attr-defined]

    await on_shutdown(ctx)

    with pytest.raises(RuntimeError, match="RedisClient is not initialized"):
        _ = RedisClient().client
