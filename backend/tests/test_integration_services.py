"""Integration tests: real Postgres (pgvector) + Redis from docker compose.

B0 (Phase 4): these tests used to run against the project database ``shopqa`` (which holds
real crawled data) and to ``DELETE`` rows from its ``llm_profiles`` table. They now create
and use a dedicated database (``shopqa_phase1_test`` by default) and Redis DB 15, assert
``current_database()`` before writing, and skip automatically when the services are down.
Run with ``uv run pytest -m integration -q``.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import asyncpg
import httpx
import pytest
import redis.asyncio as aioredis

from app.api.deps import get_registry, get_url_resolver
from app.llm.registry import LLMClientRegistry
from app.main import create_app
from tests.conftest import fake_resolver
from tests.mock_openai import build_mock_app

pytestmark = pytest.mark.integration

BACKEND_DIR = Path(__file__).resolve().parents[1]

# Dedicated test database/Redis DB: never the project DB (real data) or Redis DB 0.
TEST_DB_NAME = os.environ.get("TEST_PHASE1_DB_NAME", "shopqa_phase1_test")
PG_HOST = os.environ.get("TEST_PHASE1_PG_HOST", "localhost")
PG_PORT = int(os.environ.get("TEST_PHASE1_PG_PORT", "5433"))
PG_USER = os.environ.get("TEST_PHASE1_PG_USER", "shopqa")
PG_PASSWORD = os.environ.get("TEST_PHASE1_PG_PASSWORD", "shopqa")
PG_MAINTENANCE_DB = os.environ.get("TEST_PHASE1_PG_MAINTENANCE_DB", "shopqa")
TEST_REDIS_DB = int(os.environ.get("TEST_PHASE1_REDIS_DB", "15"))
PROJECT_DB_NAME = "shopqa"

MAINTENANCE_DSN = f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{PG_MAINTENANCE_DB}"
DATABASE_URL_ASYNC = (
    f"postgresql+asyncpg://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{TEST_DB_NAME}"
)
PG_DSN = DATABASE_URL_ASYNC.replace("+asyncpg", "")
REDIS_URL = f"redis://{PG_HOST}:6380/{TEST_REDIS_DB}"
API_KEY = os.environ.get("TEST_API_KEY", "sk-test-key-1234567890")


async def _ensure_test_database() -> None:
    conn = await asyncpg.connect(MAINTENANCE_DSN)
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", TEST_DB_NAME)
        if not exists:
            await conn.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')
    finally:
        await conn.close()


async def _require_services() -> None:
    assert TEST_DB_NAME != PROJECT_DB_NAME, "tests must not run against the project DB"
    try:
        conn = await asyncio.wait_for(asyncpg.connect(MAINTENANCE_DSN), timeout=5)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable at {MAINTENANCE_DSN}: {exc}")
    await conn.close()
    await _ensure_test_database()

    client = aioredis.from_url(REDIS_URL)
    try:
        await asyncio.wait_for(client.ping(), timeout=5)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Redis not reachable at {REDIS_URL}: {exc}")
    finally:
        await client.aclose()


@dataclass
class LiveHarness:
    app: object
    client: httpx.AsyncClient
    registry: LLMClientRegistry
    http_clients: list[httpx.AsyncClient] = field(default_factory=list)


@pytest.fixture
async def live_harness(behavior, monkeypatch):
    """App running its real lifespan (Database + Redis + SecretBox singletons).

    The singletons read ``DATABASE_URL``/``REDIS_URL`` from the environment, so the test
    DSNs must be installed *before* the lifespan starts — otherwise this fixture would write
    ``llm_profiles`` rows into the project database.
    """
    await _require_services()

    monkeypatch.setenv("DATABASE_URL", DATABASE_URL_ASYNC)
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    from app.core.config import get_settings
    from app.core.singleton import SingletonMeta

    get_settings.cache_clear()
    SingletonMeta.reset()
    cached_settings = get_settings
    singleton_meta = SingletonMeta

    app = create_app()
    mock_app = build_mock_app(behavior)
    http_clients: list[httpx.AsyncClient] = []
    client: httpx.AsyncClient | None = None

    def _factory() -> httpx.AsyncClient:
        http_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=mock_app),
            base_url="http://mock",
        )
        http_clients.append(http_client)
        return http_client

    async with app.router.lifespan_context(app):
        registry = LLMClientRegistry()
        registry.set_http_client_factory(_factory)
        app.dependency_overrides[get_registry] = lambda: registry
        app.dependency_overrides[get_url_resolver] = lambda: fake_resolver

        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )
        try:
            yield LiveHarness(
                app=app,
                client=client,
                registry=registry,
                http_clients=http_clients,
            )
        finally:
            await client.aclose()
            conn = await asyncpg.connect(PG_DSN)
            try:
                current = await conn.fetchval("SELECT current_database()")
                assert current == TEST_DB_NAME
                await conn.execute("DELETE FROM llm_profiles WHERE name LIKE 'it-%'")
            finally:
                await conn.close()
            # Drop the cached Settings/engine created with the test DSNs so later tests
            # (which expect the conftest environment) are unaffected.
            cached_settings.cache_clear()
            singleton_meta.reset()


async def test_alembic_upgrade_head_and_vector_extension() -> None:
    await _require_services()

    env = {**os.environ, "DATABASE_URL": DATABASE_URL_ASYNC}
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"alembic failed:\n{result.stdout}\n{result.stderr}"

    conn = await asyncpg.connect(PG_DSN)
    try:
        extension = await conn.fetchval("SELECT extname FROM pg_extension WHERE extname = 'vector'")
        assert extension == "vector"

        revision = await conn.fetchval("SELECT version_num FROM alembic_version")
        assert revision, "alembic_version table is empty after upgrade"

        capabilities_type = await conn.fetchval(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'llm_profiles' AND column_name = 'capabilities'"
        )
        assert capabilities_type == "jsonb"

        columns = {
            row["column_name"]
            for row in await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'llm_profiles'"
            )
        }
        assert {
            "id",
            "user_id",
            "name",
            "base_url",
            "api_key_enc",
            "chat_model",
            "small_model",
            "embedding_model",
            "max_context_tokens",
            "capabilities",
            "created_at",
            "updated_at",
        } <= columns
    finally:
        await conn.close()


async def test_health_reports_ok_with_live_services(live_harness: LiveHarness) -> None:
    response = await live_harness.client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": True, "redis": True}


async def test_profiles_crud_against_postgres(live_harness: LiveHarness) -> None:
    payload = {
        "name": "it-profile",
        "base_url": "http://mock:9999",
        "api_key": API_KEY,
        "chat_model": "mock-chat",
    }

    created = await live_harness.client.post("/api/llm/profiles", json=payload)
    assert created.status_code == 201, created.text
    body = created.json()
    profile_id = body["id"]
    assert body["api_key_masked"] == "sk-\u20267890"
    assert body["capabilities"]["chat"] is True
    assert API_KEY not in created.text

    # The row really is persisted, and the key is stored encrypted.
    conn = await asyncpg.connect(PG_DSN)
    try:
        row = await conn.fetchrow(
            "SELECT api_key_enc, capabilities, user_id FROM llm_profiles WHERE id = $1",
            uuid.UUID(profile_id),
        )
    finally:
        await conn.close()
    assert row is not None
    assert row["user_id"] == "local"
    assert row["api_key_enc"] != API_KEY
    assert row["api_key_enc"].startswith("gAAAAA")  # Fernet token
    assert json.loads(row["capabilities"])["chat"] is True

    listed = await live_harness.client.get("/api/llm/profiles")
    assert listed.status_code == 200
    assert profile_id in [p["id"] for p in listed.json()]
    assert API_KEY not in listed.text

    fetched = await live_harness.client.get(f"/api/llm/profiles/{profile_id}")
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "it-profile"

    patched = await live_harness.client.patch(
        f"/api/llm/profiles/{profile_id}",
        json={"name": "it-profile-renamed", "max_context_tokens": 8000},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["name"] == "it-profile-renamed"
    assert patched.json()["max_context_tokens"] == 8000

    deleted = await live_harness.client.delete(f"/api/llm/profiles/{profile_id}")
    assert deleted.status_code == 204

    gone = await live_harness.client.get(f"/api/llm/profiles/{profile_id}")
    assert gone.status_code == 404


async def test_profiles_isolated_between_runs(live_harness: LiveHarness) -> None:
    """A fresh profile list must not contain leftovers from other tests."""
    response = await live_harness.client.get("/api/llm/profiles")

    assert response.status_code == 200
    assert all(not p["name"].startswith("it-") for p in response.json())
