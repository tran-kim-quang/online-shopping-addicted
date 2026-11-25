"""Unit tests for ``/health`` and app wiring (no external services required)."""

from __future__ import annotations

import httpx

from app.cache.redis import RedisClient
from app.db.database import Database
from app.main import create_app


async def _get_health() -> httpx.Response:
    app = create_app()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    try:
        return await client.get("/health")
    finally:
        await client.aclose()


async def test_health_ok_when_both_services_answer(monkeypatch) -> None:
    async def _ok(self) -> bool:  # noqa: ARG001 - bound method shape
        return True

    monkeypatch.setattr(Database, "ping", _ok)
    monkeypatch.setattr(RedisClient, "ping", _ok)

    response = await _get_health()

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": True, "redis": True}


async def test_health_degraded_when_redis_down(monkeypatch) -> None:
    async def _ok(self) -> bool:  # noqa: ARG001
        return True

    async def _fail(self) -> bool:  # noqa: ARG001
        return False

    monkeypatch.setattr(Database, "ping", _ok)
    monkeypatch.setattr(RedisClient, "ping", _fail)

    response = await _get_health()

    assert response.status_code == 200
    assert response.json() == {"status": "degraded", "db": True, "redis": False}


async def test_health_degraded_when_uninitialised() -> None:
    """Singletons that were never initialised must not blow up the handler."""
    response = await _get_health()

    assert response.status_code == 200
    assert response.json() == {"status": "degraded", "db": False, "redis": False}


async def test_openapi_schema_is_served() -> None:
    app = create_app()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    try:
        response = await client.get("/openapi.json")
    finally:
        await client.aclose()

    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/health" in paths
    assert "/api/llm/profiles" in paths
    assert "/api/llm/models" in paths
