"""Shared fixtures for the Phase 0/1 test suite.

All unit tests run fully offline: the LLM provider is an in-process FastAPI mock
served through ``httpx.ASGITransport`` and DNS resolution is faked.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# --- Environment must be prepared before importing ``app`` modules ---------
# A deterministic Fernet key keeps SecretBox round-trips reproducible.
TEST_SECRET_KEY = "vN5y6rM0J9h2xL8qK3pW1zB4cF7dG0tH2sV5mN8wX4Q="
TEST_API_KEY = "sk-test-key-1234567890"
# The key a user types into the UI; same value as the mock provider expects.
TEST_RAW_API_KEY = TEST_API_KEY

os.environ["SECRET_KEY"] = TEST_SECRET_KEY
os.environ["DATABASE_URL"] = "postgresql+asyncpg://shopqa:shopqa@localhost:5433/shopqa"
os.environ["REDIS_URL"] = "redis://localhost:6380/0"
os.environ["ALLOW_PRIVATE_LLM_HOSTS"] = "true"
os.environ["LLM_PROBE_TIMEOUT_SECONDS"] = "5"
# Retries would make failure-injection tests slow and noisy.
os.environ["LLM_MAX_RETRIES"] = "0"
os.environ["LOG_LEVEL"] = "WARNING"

import httpx  # noqa: E402
import pytest  # noqa: E402
from pydantic import SecretStr  # noqa: E402

from app.api.deps import (  # noqa: E402
    get_registry,
    get_secret_box,
    get_session,
    get_url_resolver,
)
from app.core.singleton import SingletonMeta  # noqa: E402
from app.db.models import LLMProfile  # noqa: E402
from app.llm.client import LLMClient  # noqa: E402
from app.llm.registry import LLMClientRegistry  # noqa: E402
from app.llm.types import LLMConfig  # noqa: E402
from app.main import create_app  # noqa: E402
from app.security.secrets import SecretBox  # noqa: E402
from tests.mock_openai import CHAT_MODEL, MockBehavior, build_mock_app  # noqa: E402

PUBLIC_TEST_IP = "93.184.216.34"


async def fake_resolver(_host: str) -> list[str]:
    """Deterministic resolver returning a public IP (no DNS lookups)."""
    return [PUBLIC_TEST_IP]


# ---------------------------------------------------------------------------
# Generic fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_singletons() -> Any:
    """Isolate Singleton instances (and their locks) between tests."""
    SingletonMeta.reset()
    yield
    SingletonMeta.reset()


@pytest.fixture(autouse=True)
def _block_real_network(request, monkeypatch) -> Any:
    """Fail loudly if a non-integration test opens a real socket connection."""
    if request.node.get_closest_marker("integration") is not None:
        yield
        return

    import socket

    def _refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("unit tests must not perform real network I/O")

    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse)
    yield


@pytest.fixture
def behavior() -> MockBehavior:
    """Mutable behaviour of the mock provider for the current test."""
    return MockBehavior()


@pytest.fixture
def mock_app(behavior: MockBehavior):
    return build_mock_app(behavior)


@pytest.fixture
async def mock_http(mock_app) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client bound to the mock provider via ASGI transport (no sockets)."""
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_app),
        base_url="http://mock",
    )
    yield client
    await client.aclose()


@pytest.fixture
def secret_box() -> SecretBox:
    return SecretBox(TEST_SECRET_KEY)


@pytest.fixture
def config_factory():
    """Build ``LLMConfig`` objects pointing at the mock provider."""

    def _make(**overrides: Any) -> LLMConfig:
        data: dict[str, Any] = {
            "base_url": "http://mock/v1",
            "api_key": SecretStr(TEST_API_KEY),
            "chat_model": CHAT_MODEL,
            "timeout": 5.0,
            "max_retries": 0,
        }
        data.update(overrides)
        return LLMConfig(**data)

    return _make


@pytest.fixture
def llm_client(config_factory, mock_http) -> LLMClient:
    return LLMClient(config_factory(), http_client=mock_http)


@pytest.fixture
def client_factory(config_factory, mock_http):
    """Build extra ``LLMClient`` instances sharing the mock transport."""

    def _make(**overrides: Any) -> LLMClient:
        return LLMClient(config_factory(**overrides), http_client=mock_http)

    return _make


# ---------------------------------------------------------------------------
# In-memory session double for API unit tests (no Postgres required)
# ---------------------------------------------------------------------------


class _FakeScalars:
    def __init__(self, rows: list[LLMProfile]) -> None:
        self._rows = rows

    def all(self) -> list[LLMProfile]:
        return list(self._rows)


class _FakeResult:
    def __init__(self, rows: list[LLMProfile]) -> None:
        self._rows = rows

    def scalars(self) -> _FakeScalars:
        return _FakeScalars(self._rows)


class FakeSession:
    """Minimal async SQLAlchemy session stand-in backed by a dict.

    Applies the column defaults that Postgres would otherwise supply so the
    route handlers can serialise freshly-created rows.
    """

    def __init__(self) -> None:
        self.profiles: dict[uuid.UUID, LLMProfile] = {}
        self.commits = 0

    def _apply_defaults(self, profile: LLMProfile) -> None:
        now = datetime.now(UTC)
        if profile.id is None:
            profile.id = uuid.uuid4()
        if profile.user_id is None:
            profile.user_id = "local"
        if profile.max_context_tokens is None:
            profile.max_context_tokens = 32000
        if profile.capabilities is None:
            profile.capabilities = {}
        if profile.created_at is None:
            profile.created_at = now
        if profile.updated_at is None:
            profile.updated_at = now

    def add(self, profile: LLMProfile) -> None:
        self._apply_defaults(profile)
        self.profiles[profile.id] = profile

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, profile: LLMProfile) -> None:
        self._apply_defaults(profile)
        profile.updated_at = datetime.now(UTC)

    async def get(self, _model: type, pk: uuid.UUID) -> LLMProfile | None:
        return self.profiles.get(pk)

    async def execute(self, _stmt: Any) -> _FakeResult:
        rows = sorted(self.profiles.values(), key=lambda p: p.created_at, reverse=True)
        return _FakeResult(rows)

    async def delete(self, profile: LLMProfile) -> None:
        self.profiles.pop(profile.id, None)


@dataclass
class ApiHarness:
    """Everything an API test needs to drive the app and inspect side effects."""

    app: Any
    client: httpx.AsyncClient
    session: FakeSession
    registry: LLMClientRegistry
    secret_box: SecretBox
    http_clients: list[httpx.AsyncClient] = field(default_factory=list)


@pytest.fixture
async def api_harness(behavior: MockBehavior, secret_box: SecretBox) -> AsyncIterator[Any]:
    """Factory for an app wired to the mock provider + in-memory DB session."""
    teardown: list[tuple[httpx.AsyncClient, LLMClientRegistry, list[httpx.AsyncClient]]] = []

    async def _make(*, allow_private_hosts: bool | None = None) -> ApiHarness:
        mock_app = build_mock_app(behavior)
        http_clients: list[httpx.AsyncClient] = []

        def _factory() -> httpx.AsyncClient:
            http_client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=mock_app),
                base_url="http://mock",
            )
            http_clients.append(http_client)
            return http_client

        registry = LLMClientRegistry()
        registry.configure(max_clients=8)
        registry.set_http_client_factory(_factory)

        app = create_app()
        session = FakeSession()
        app.dependency_overrides[get_session] = lambda: session
        app.dependency_overrides[get_registry] = lambda: registry
        app.dependency_overrides[get_secret_box] = lambda: secret_box
        app.dependency_overrides[get_url_resolver] = lambda: fake_resolver

        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )
        teardown.append((client, registry, http_clients))
        return ApiHarness(
            app=app,
            client=client,
            session=session,
            registry=registry,
            secret_box=secret_box,
            http_clients=http_clients,
        )

    yield _make

    for client, registry, http_clients in teardown:
        await registry.aclose_all()
        await client.aclose()
        for http_client in http_clients:
            if not http_client.is_closed:
                await http_client.aclose()
