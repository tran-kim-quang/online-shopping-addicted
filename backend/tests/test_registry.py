"""Unit tests for ``app.llm.registry.LLMClientRegistry`` (singleton LRU cache)."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import get_settings
from app.core.singleton import SingletonMeta
from app.llm.registry import LLMClientRegistry
from tests.mock_openai import build_mock_app


@pytest.fixture
def registry(behavior) -> LLMClientRegistry:
    mock_app = build_mock_app(behavior)
    created: list[httpx.AsyncClient] = []

    def _factory() -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=mock_app),
            base_url="http://mock",
        )
        created.append(client)
        return client

    instance = LLMClientRegistry()
    instance.configure(max_clients=4)
    instance.set_http_client_factory(_factory)
    instance.created_clients = created  # type: ignore[attr-defined]  # test observation hook
    return instance


async def test_registry_is_singleton(registry: LLMClientRegistry) -> None:
    assert LLMClientRegistry() is registry


async def test_same_config_returns_same_client(registry: LLMClientRegistry, config_factory) -> None:
    first = await registry.get(config_factory())
    second = await registry.get(config_factory())

    assert first is second
    assert len(registry.created_clients) == 1  # type: ignore[attr-defined]


async def test_different_api_key_returns_different_client(
    registry: LLMClientRegistry, config_factory
) -> None:
    first = await registry.get(config_factory(api_key=SecretStr("key-one-aaaaaaaa")))
    second = await registry.get(config_factory(api_key=SecretStr("key-two-bbbbbbbb")))

    assert first is not second
    assert len(registry.created_clients) == 2  # type: ignore[attr-defined]


async def test_different_timeout_returns_different_client(
    registry: LLMClientRegistry, config_factory
) -> None:
    first = await registry.get(config_factory(timeout=1.0))
    second = await registry.get(config_factory(timeout=2.0))

    assert first is not second


# ---------------------------------------------------------------------------
# F3 — model selection is part of the cache identity
# ---------------------------------------------------------------------------


async def test_different_chat_model_returns_different_client(
    registry: LLMClientRegistry, config_factory
) -> None:
    first = await registry.get(config_factory(chat_model="model-a"))
    second = await registry.get(config_factory(chat_model="model-b"))

    assert first is not second
    assert first.config.chat_model == "model-a"
    assert second.config.chat_model == "model-b"
    assert len(registry.created_clients) == 2  # type: ignore[attr-defined]


async def test_different_small_model_returns_different_client(
    registry: LLMClientRegistry, config_factory
) -> None:
    first = await registry.get(config_factory(small_model="small-a"))
    second = await registry.get(config_factory(small_model="small-b"))

    assert first is not second
    assert first.config.small_model == "small-a"
    assert second.config.small_model == "small-b"


async def test_different_embedding_model_returns_different_client(
    registry: LLMClientRegistry, config_factory
) -> None:
    first = await registry.get(config_factory(embedding_model="embed-a"))
    second = await registry.get(config_factory(embedding_model="embed-b"))

    assert first is not second
    assert first.config.embedding_model == "embed-a"
    assert second.config.embedding_model == "embed-b"


async def test_small_model_presence_differs_from_absence(
    registry: LLMClientRegistry, config_factory
) -> None:
    with_small = await registry.get(config_factory(small_model="small-a"))
    without_small = await registry.get(config_factory(small_model=None))

    assert with_small is not without_small


async def test_same_models_and_credentials_share_one_client(
    registry: LLMClientRegistry, config_factory
) -> None:
    first = await registry.get(
        config_factory(chat_model="model-a", small_model="small-a", embedding_model="embed-a")
    )
    second = await registry.get(
        config_factory(chat_model="model-a", small_model="small-a", embedding_model="embed-a")
    )

    assert first is second
    assert len(registry.created_clients) == 1  # type: ignore[attr-defined]


async def test_model_aware_key_still_hides_raw_api_key(
    registry: LLMClientRegistry, config_factory
) -> None:
    from tests.conftest import TEST_RAW_API_KEY

    await registry.get(config_factory(api_key=SecretStr(TEST_RAW_API_KEY), chat_model="model-a"))

    for key in registry._clients:  # noqa: SLF001 - asserting on internal cache
        assert TEST_RAW_API_KEY not in key
        assert "model-a" not in key
        assert len(key) == 64
        assert all(ch in "0123456789abcdef" for ch in key)


async def test_cache_keys_never_contain_raw_api_key(
    registry: LLMClientRegistry, config_factory
) -> None:
    raw_key = "sk-very-secret-cache-key-1234567890"
    await registry.get(config_factory(api_key=SecretStr(raw_key)))

    keys = list(registry._clients)  # noqa: SLF001 - asserting on internal cache
    assert keys
    for key in keys:
        assert raw_key not in key
        assert all(ch in "0123456789abcdef" for ch in key)
        assert len(key) == 64


async def test_lru_eviction_closes_oldest_client(
    registry: LLMClientRegistry, config_factory
) -> None:
    registry.configure(max_clients=2)
    await registry.get(config_factory(api_key=SecretStr("key-a-aaaaaaaa")))
    await registry.get(config_factory(api_key=SecretStr("key-b-bbbbbbbb")))
    await registry.get(config_factory(api_key=SecretStr("key-c-cccccccc")))

    await asyncio.sleep(0.05)

    created = registry.created_clients  # type: ignore[attr-defined]
    assert len(created) == 3
    assert created[0].is_closed  # LRU (first) was evicted and closed
    assert not created[1].is_closed
    assert not created[2].is_closed
    assert len(registry._clients) == 2  # noqa: SLF001


async def test_recently_used_client_survives_eviction(
    registry: LLMClientRegistry, config_factory
) -> None:
    registry.configure(max_clients=2)
    cfg_a = config_factory(api_key=SecretStr("key-a-aaaaaaaa"))
    cfg_b = config_factory(api_key=SecretStr("key-b-bbbbbbbb"))
    cfg_c = config_factory(api_key=SecretStr("key-c-cccccccc"))

    await registry.get(cfg_a)
    await registry.get(cfg_b)
    await registry.get(cfg_a)  # touch A -> B is now least recently used
    await registry.get(cfg_c)

    await asyncio.sleep(0.05)

    created = registry.created_clients  # type: ignore[attr-defined]
    assert not created[0].is_closed  # A kept
    assert created[1].is_closed  # B evicted
    assert not created[2].is_closed


async def test_invalidate_removes_and_closes(registry: LLMClientRegistry, config_factory) -> None:
    config = config_factory()
    first = await registry.get(config)
    first_http = registry.created_clients[0]  # type: ignore[attr-defined]

    await registry.invalidate(config)

    assert first_http.is_closed
    assert registry._clients == {}  # noqa: SLF001
    second = await registry.get(config)
    assert second is not first


async def test_invalidate_unknown_config_is_noop(
    registry: LLMClientRegistry, config_factory
) -> None:
    await registry.invalidate(config_factory())

    assert registry._clients == {}  # noqa: SLF001


async def test_aclose_all_closes_every_client(registry: LLMClientRegistry, config_factory) -> None:
    await registry.get(config_factory(api_key=SecretStr("key-a-aaaaaaaa")))
    await registry.get(config_factory(api_key=SecretStr("key-b-bbbbbbbb")))

    await registry.aclose_all()

    assert registry._clients == {}  # noqa: SLF001
    assert all(client.is_closed for client in registry.created_clients)  # type: ignore[attr-defined]
    await registry.aclose_all()  # idempotent


async def test_aclose_all_releases_factory_http_client_bookkeeping(
    registry: LLMClientRegistry, config_factory
) -> None:
    await registry.get(config_factory(api_key=SecretStr("key-a-aaaaaaaa")))
    await registry.get(config_factory(api_key=SecretStr("key-b-bbbbbbbb")))

    await registry.aclose_all()

    assert registry._factory_http_clients == {}  # noqa: SLF001


async def test_eviction_releases_factory_http_client_bookkeeping(
    registry: LLMClientRegistry, config_factory
) -> None:
    registry.configure(max_clients=1)
    await registry.get(config_factory(api_key=SecretStr("key-a-aaaaaaaa")))
    await registry.get(config_factory(api_key=SecretStr("key-b-bbbbbbbb")))

    await asyncio.sleep(0.05)

    assert len(registry._factory_http_clients) == 1  # noqa: SLF001
    assert registry.created_clients[0].is_closed  # type: ignore[attr-defined]


def test_configure_rejects_invalid_max_clients(registry: LLMClientRegistry) -> None:
    with pytest.raises(ValueError, match="max_clients"):
        registry.configure(max_clients=0)


def test_default_max_clients_reads_settings(registry: LLMClientRegistry) -> None:
    registry.configure(max_clients=1)
    registry.configure()

    assert registry.max_clients == 1
    assert get_settings().LLM_REGISTRY_MAX_CLIENTS >= 1


def test_default_max_clients_falls_back_without_settings(monkeypatch) -> None:
    import app.llm.registry as registry_module

    def _boom():
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(registry_module, "get_settings", _boom)
    SingletonMeta.reset(LLMClientRegistry)

    assert LLMClientRegistry().max_clients == 64


def test_http_client_factory_property_round_trip(registry: LLMClientRegistry) -> None:
    factory = registry.http_client_factory
    assert factory is not None

    registry.http_client_factory = None
    assert registry.http_client_factory is None

    registry.http_client_factory = factory
    assert registry.http_client_factory is factory


async def test_aclose_all_awaits_pending_eviction_tasks(
    registry: LLMClientRegistry, config_factory
) -> None:
    registry.configure(max_clients=1)
    await registry.get(config_factory(api_key=SecretStr("key-a-aaaaaaaa")))
    await registry.get(config_factory(api_key=SecretStr("key-b-bbbbbbbb")))

    # A close request that is still in flight must be awaited at shutdown.
    pending = asyncio.get_running_loop().create_task(asyncio.sleep(0.01))
    registry._closing_tasks.add(pending)  # noqa: SLF001

    await registry.aclose_all()

    assert pending.done()


async def test_client_uses_config_timeout(registry: LLMClientRegistry, config_factory) -> None:
    client = await registry.get(config_factory(timeout=12.5))

    assert client.config.timeout == 12.5
