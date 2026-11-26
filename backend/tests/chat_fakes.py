"""In-memory chat doubles: a SQLAlchemy-session stand-in plus app wiring for ``/api/chat``.

Only the four query shapes the chat router issues are emulated (``get`` by primary key,
message list by session, session list, delete). No Postgres, Redis, sockets or models.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from app.api import chat as chat_module
from app.api.deps import (
    get_embedder_override,
    get_job_events,
    get_job_queue,
    get_registry,
    get_secret_box,
    get_session,
    get_session_factory,
    get_settings,
    get_url_resolver,
)
from app.db.models import ChatMessage, ChatSession, LLMProfile, Source
from app.llm.registry import LLMClientRegistry
from app.main import create_app
from app.security.secrets import SecretBox
from tests.ingest_fakes import FakeEmbedder, RecordingJobEvents, RecordingJobQueue
from tests.mock_openai import MockBehavior, build_mock_app

SECRET = "vN5y6rM0J9h2xL8qK3pW1zB4cF7dG0tH2sV5mN8wX4Q="
# The key the mock provider accepts (shared with the rest of the suite).
from tests.conftest import TEST_API_KEY  # noqa: E402

RAW_API_KEY = TEST_API_KEY


class _Scalars:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _Scalars:
        return _Scalars(self._rows)

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None


class ChatStore:
    """Row store shared by the request session and the fresh-session factory."""

    def __init__(self) -> None:
        self.sources: dict[uuid.UUID, Source] = {}
        self.profiles: dict[uuid.UUID, LLMProfile] = {}
        self.sessions: dict[uuid.UUID, ChatSession] = {}
        self.messages: dict[uuid.UUID, ChatMessage] = {}
        self.commits = 0
        self.deleted_messages = 0

    def add(self, row: Any) -> None:
        now = datetime.now(UTC)
        if row.id is None:
            row.id = uuid.uuid4()
        if isinstance(row, ChatSession):
            row.created_at = row.created_at or now
            row.updated_at = row.updated_at or now
            row.user_id = row.user_id or "local"
            self.sessions[row.id] = row
        elif isinstance(row, ChatMessage):
            row.created_at = row.created_at or now
            row.tool_calls = list(row.tool_calls or [])
            row.citations = list(row.citations or [])
            self.messages[row.id] = row
        elif isinstance(row, (Source, LLMProfile)):
            self.add_seed(row)
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unexpected row type {type(row)!r}")

    def add_seed(self, row: Any) -> None:
        if isinstance(row, Source):
            self.sources[row.id] = row
        elif isinstance(row, LLMProfile):
            self.profiles[row.id] = row
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unexpected seed row {type(row)!r}")

    def get(self, model: type, pk: uuid.UUID) -> Any | None:
        if model is Source:
            return self.sources.get(pk)
        if model is LLMProfile:
            return self.profiles.get(pk)
        if model is ChatSession:
            return self.sessions.get(pk)
        if model is ChatMessage:
            return self.messages.get(pk)
        return None  # pragma: no cover - defensive

    def execute(self, stmt: Any) -> _Result:
        entity = stmt.column_descriptions[0].get("entity")
        params = stmt.compile().params
        target = next((v for v in params.values() if isinstance(v, uuid.UUID)), None)
        if entity is ChatMessage:
            rows = [m for m in self.messages.values() if target is None or m.session_id == target]
            rows.sort(key=lambda m: (m.created_at, str(m.id)))
            return _Result(rows)
        if entity is ChatSession:
            rows = [s for s in self.sessions.values() if target is None or s.source_id == target]
            if target is None:
                rows = list(self.sessions.values())
            rows.sort(key=lambda s: (s.updated_at, s.created_at), reverse=True)
            return _Result(rows)
        return _Result([])  # pragma: no cover - defensive

    def delete(self, row: Any) -> None:
        if isinstance(row, ChatSession):
            doomed = [m.id for m in self.messages.values() if m.session_id == row.id]
            for message_id in doomed:
                self.messages.pop(message_id, None)
            self.deleted_messages += len(doomed)
            self.sessions.pop(row.id, None)


class FakeChatSession:
    """Async session façade over :class:`ChatStore`."""

    def __init__(self, store: ChatStore) -> None:
        self.store = store
        self.committed = 0
        self.rolled_back = 0

    def add(self, row: Any) -> None:
        self.store.add(row)

    async def get(self, model: type, pk: uuid.UUID) -> Any | None:
        return self.store.get(model, pk)

    async def execute(self, stmt: Any) -> _Result:
        return self.store.execute(stmt)

    async def commit(self) -> None:
        self.committed += 1
        self.store.commits += 1

    async def rollback(self) -> None:
        self.rolled_back += 1

    async def delete(self, row: Any) -> None:
        self.store.delete(row)

    async def refresh(self, row: Any) -> None:
        return None

    async def close(self) -> None:
        return None


class FakeSessionFactory:
    """Session factory yielding a fresh façade over the same store."""

    def __init__(self, store: ChatStore) -> None:
        self.store = store
        self.opens = 0

    def __call__(self) -> Any:
        factory = self

        class _Ctx:
            async def __aenter__(self) -> FakeChatSession:
                factory.opens += 1
                return FakeChatSession(factory.store)

            async def __aexit__(self, *_exc: Any) -> None:
                return None

        return _Ctx()


@dataclass
class ChatHarness:
    """App + client + recorded state for ``/api/chat`` tests."""

    app: Any
    client: httpx.AsyncClient
    store: ChatStore
    session: FakeChatSession
    session_factory: FakeSessionFactory
    behavior: MockBehavior
    job_events: RecordingJobEvents
    job_queue: RecordingJobQueue
    registry: LLMClientRegistry
    http_clients: list[httpx.AsyncClient] = field(default_factory=list)
    stubs: Any = None

    def seed_source(
        self,
        *,
        status: str = "done",
        stats: dict[str, Any] | None = None,
        url_type: str = "shop",
        source_id: uuid.UUID | None = None,
    ) -> Source:
        source = Source(
            id=source_id or uuid.uuid4(),
            url="https://tiki.vn/cua-hang/tiki-trading",
            url_type=url_type,
            platform="tiki",
            status=status,
            error=None,
            stats={"ingest": {"chunks": 3}} if stats is None else stats,
            external_ref={"seller_id": "1"},
            crawled_at=datetime.now(UTC),
        )
        self.store.add_seed(source)
        return source

    def seed_profile(
        self,
        secret_box: SecretBox,
        *,
        api_key: str | None = RAW_API_KEY,
        chat_model: str = "mock-chat",
        small_model: str | None = None,
        capabilities: dict[str, Any] | None = None,
    ) -> LLMProfile:
        profile = LLMProfile(
            id=uuid.uuid4(),
            user_id="local",
            name=f"chat-test-{uuid.uuid4().hex[:6]}",
            base_url="http://mock/v1",
            api_key_enc=secret_box.encrypt(api_key) if api_key else None,
            chat_model=chat_model,
            small_model=small_model,
            embedding_model=None,
            max_context_tokens=32000,
            capabilities=capabilities or {"chat": True, "tool_calling": True, "streaming": True},
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        self.store.add_seed(profile)
        return profile


def build_chat_harness(
    *,
    behavior: MockBehavior,
    secret_box: SecretBox,
    monkeypatch: Any,
    products: list[Any] | None = None,
    shop: Any = None,
    settings: Any = None,
    embedder: Any = None,
    tools: dict[str, Any] | None = None,
    capabilities: Any = None,
) -> ChatHarness:
    """Wire the real app with in-memory chat/session dependencies and a mock provider."""
    from pydantic import SecretStr

    from app.core.config import Settings

    store = ChatStore()
    session = FakeChatSession(store)
    session_factory = FakeSessionFactory(store)
    job_events = RecordingJobEvents()
    job_queue = RecordingJobQueue()

    mock_app = build_mock_app(behavior)
    http_clients: list[httpx.AsyncClient] = []

    def _http_factory() -> httpx.AsyncClient:
        http_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=mock_app),
            base_url="http://mock",
        )
        http_clients.append(http_client)
        return http_client

    registry = LLMClientRegistry()
    registry.configure(max_clients=8)
    registry.set_http_client_factory(_http_factory)

    scoped_products = list(products or [])

    async def _fake_resolve_scope(_session: Any, _source: Any) -> tuple[Any, list[Any]]:
        return shop, scoped_products

    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_registry] = lambda: registry
    app.dependency_overrides[get_secret_box] = lambda: secret_box
    app.dependency_overrides[get_settings] = lambda: (
        settings or Settings(SECRET_KEY=SecretStr(SECRET))
    )
    # The scope resolver is DB-bound; the store below owns products/shops.
    app.dependency_overrides[get_job_events] = lambda: job_events
    app.dependency_overrides[get_job_queue] = lambda: job_queue
    app.dependency_overrides[get_url_resolver] = lambda: None
    app.dependency_overrides[get_embedder_override] = lambda: (
        embedder if embedder is not None else FakeEmbedder()
    )
    monkeypatch.setattr(chat_module, "resolve_source_scope", _fake_resolve_scope)
    if tools is not None:
        from app.agent.loop import QAAgent

        monkeypatch.setattr(
            chat_module,
            "QAAgent",
            lambda: QAAgent(tools=tools, capabilities=capabilities),
        )

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        timeout=10.0,
    )
    return ChatHarness(
        app=app,
        client=client,
        store=store,
        session=session,
        session_factory=session_factory,
        behavior=behavior,
        job_events=job_events,
        job_queue=job_queue,
        registry=registry,
        http_clients=http_clients,
        stubs=tools,
    )


async def close_chat_harness(harness: ChatHarness) -> None:
    """Close every HTTP client the harness created (call in ``finally``)."""
    await harness.registry.aclose_all()
    await harness.client.aclose()
    for http_client in harness.http_clients:
        if not http_client.is_closed:
            await http_client.aclose()
