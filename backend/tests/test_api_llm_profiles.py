"""Unit tests for the ``/api/llm`` router (mock provider + in-memory session)."""

from __future__ import annotations

import uuid

import pytest

from app.api.deps import get_settings as deps_get_settings
from app.core.config import get_settings
from tests.conftest import TEST_RAW_API_KEY

RAW_KEY = TEST_RAW_API_KEY  # "sk-raw-secret-abcdef1234567890"


def _payload(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "name": "mock profile",
        "base_url": "http://mock:9999",
        "api_key": RAW_KEY,
        "chat_model": "mock-chat",
    }
    data.update(overrides)
    return data


def _assert_no_secret(response, *secrets: str) -> None:
    body = response.text
    for secret in secrets:
        assert secret not in body, f"secret leaked in response: {body}"


async def test_create_profile_success(api_harness, behavior) -> None:
    harness = await api_harness()

    response = await harness.client.post("/api/llm/profiles", json=_payload())

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "mock profile"
    assert body["base_url"] == "http://mock:9999/v1"  # normalized
    assert body["api_key_masked"] == "sk-\u20267890"
    assert body["max_context_tokens"] == 32000
    assert body["capabilities"]["chat"] is True
    assert body["capabilities"]["tool_calling"] is True
    assert body["capabilities"]["json_mode"] is True
    assert body["capabilities"]["streaming"] is True
    _assert_no_secret(response, RAW_KEY)
    assert 'api_key"' not in response.text

    stored = next(iter(harness.session.profiles.values()))
    assert stored.api_key_enc is not None
    assert stored.api_key_enc != RAW_KEY
    assert harness.secret_box.decrypt(stored.api_key_enc) == RAW_KEY
    assert behavior.chat_calls == 4  # chat, tools, json_mode, streaming


async def test_create_profile_short_key_is_fully_masked(api_harness, behavior) -> None:
    behavior.expected_api_key = "short"
    harness = await api_harness()

    response = await harness.client.post("/api/llm/profiles", json=_payload(api_key="short"))

    assert response.status_code == 201
    assert response.json()["api_key_masked"] == "****"
    _assert_no_secret(response, "short")


async def test_create_profile_without_api_key(api_harness, behavior) -> None:
    behavior.require_api_key = False
    harness = await api_harness()

    response = await harness.client.post(
        "/api/llm/profiles", json=_payload(api_key=None, name="local ollama")
    )

    assert response.status_code == 201
    assert response.json()["api_key_masked"] is None
    stored = next(iter(harness.session.profiles.values()))
    assert stored.api_key_enc is None


async def test_create_profile_probe_failure_returns_422(api_harness, behavior) -> None:
    behavior.expected_api_key = "some-other-key"
    harness = await api_harness()

    response = await harness.client.post("/api/llm/profiles", json=_payload())

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error"] == "llm_probe_failed"
    assert body["capabilities"]["chat"] is False
    assert body["errors"]["chat"]
    _assert_no_secret(response, RAW_KEY)
    assert harness.session.profiles == {}


async def test_create_profile_unavailable_provider_returns_422(api_harness, behavior) -> None:
    behavior.fail_status = 502
    harness = await api_harness()

    response = await harness.client.post("/api/llm/profiles", json=_payload())

    assert response.status_code == 422
    assert response.json()["capabilities"]["chat"] is False


@pytest.mark.parametrize(
    "base_url",
    [
        "http://169.254.169.254/latest/meta-data",
        "http://[fd00:ec2::254]/v1",
        "file:///etc/passwd",
        "http://user:pass@mock:9999",
    ],
)
async def test_create_profile_rejects_unsafe_url(api_harness, behavior, base_url) -> None:
    harness = await api_harness()

    response = await harness.client.post("/api/llm/profiles", json=_payload(base_url=base_url))

    assert response.status_code == 400, response.text
    assert response.json()["error"] == "unsafe_url"
    assert behavior.chat_calls == 0  # never contacted the provider
    assert harness.session.profiles == {}


async def test_create_profile_private_host_blocked_when_disabled(api_harness, behavior) -> None:
    behavior.require_api_key = False
    harness = await api_harness()
    harness.app.dependency_overrides[deps_get_settings] = lambda: get_settings().model_copy(
        update={"ALLOW_PRIVATE_LLM_HOSTS": False}
    )

    response = await harness.client.post(
        "/api/llm/profiles",
        json=_payload(base_url="http://10.0.0.5:8000", api_key=None),
    )

    assert response.status_code == 400
    assert response.json()["error"] == "unsafe_url"

    allowed = await harness.client.post(
        "/api/llm/profiles",
        json=_payload(base_url="https://api.example.com", api_key=None, name="public"),
    )
    assert allowed.status_code == 201, allowed.text


async def test_list_models(api_harness) -> None:
    harness = await api_harness()

    response = await harness.client.post(
        "/api/llm/models",
        json={"base_url": "http://mock:9999", "api_key": RAW_KEY},
    )

    assert response.status_code == 200
    assert response.json()["models"] == ["mock-chat", "mock-embed"]
    _assert_no_secret(response, RAW_KEY)


async def test_list_models_provider_without_models_endpoint(api_harness, behavior) -> None:
    behavior.models_endpoint = False
    harness = await api_harness()

    response = await harness.client.post(
        "/api/llm/models", json={"base_url": "http://mock:9999", "api_key": RAW_KEY}
    )

    assert response.status_code == 404
    assert response.json()["error"] == "llm_not_found"
    _assert_no_secret(response, RAW_KEY)


async def test_list_models_rejects_unsafe_url(api_harness) -> None:
    harness = await api_harness()

    response = await harness.client.post(
        "/api/llm/models", json={"base_url": "http://169.254.169.254", "api_key": RAW_KEY}
    )

    assert response.status_code == 400
    assert response.json()["error"] == "unsafe_url"


async def test_list_profiles_empty(api_harness) -> None:
    harness = await api_harness()

    response = await harness.client.get("/api/llm/profiles")

    assert response.status_code == 200
    assert response.json() == []


async def test_list_and_get_profiles(api_harness) -> None:
    harness = await api_harness()
    created = (await harness.client.post("/api/llm/profiles", json=_payload())).json()

    listed = await harness.client.get("/api/llm/profiles")
    fetched = await harness.client.get(f"/api/llm/profiles/{created['id']}")

    assert listed.status_code == 200
    assert [p["id"] for p in listed.json()] == [created["id"]]
    assert fetched.status_code == 200
    assert fetched.json()["id"] == created["id"]
    _assert_no_secret(listed, RAW_KEY)
    _assert_no_secret(fetched, RAW_KEY)


async def test_get_profile_not_found(api_harness) -> None:
    harness = await api_harness()

    response = await harness.client.get(f"/api/llm/profiles/{uuid.uuid4()}")

    assert response.status_code == 404


async def test_patch_name_only_skips_reprobe(api_harness, behavior, monkeypatch) -> None:
    harness = await api_harness()
    created = (await harness.client.post("/api/llm/profiles", json=_payload())).json()
    calls = _spy_invalidate(harness.registry, monkeypatch)
    chat_calls_before = behavior.chat_calls

    response = await harness.client.patch(
        f"/api/llm/profiles/{created['id']}", json={"name": "renamed"}
    )

    assert response.status_code == 200
    assert response.json()["name"] == "renamed"
    assert behavior.chat_calls == chat_calls_before
    assert calls == []
    _assert_no_secret(response, RAW_KEY)


async def test_patch_api_key_reprobes_and_invalidates(api_harness, behavior, monkeypatch) -> None:
    harness = await api_harness()
    created = (await harness.client.post("/api/llm/profiles", json=_payload())).json()
    calls = _spy_invalidate(harness.registry, monkeypatch)
    chat_calls_before = behavior.chat_calls
    new_key = "sk-brand-new-key-9876543210"
    behavior.expected_api_key = new_key  # the rotated key is now valid at the provider

    response = await harness.client.patch(
        f"/api/llm/profiles/{created['id']}", json={"api_key": new_key}
    )

    assert response.status_code == 200, response.text
    assert len(calls) == 1
    assert behavior.chat_calls == chat_calls_before + 4  # re-probed
    assert response.json()["api_key_masked"] == "sk-\u20263210"
    _assert_no_secret(response, new_key, RAW_KEY)
    stored = next(iter(harness.session.profiles.values()))
    assert harness.secret_box.decrypt(stored.api_key_enc) == new_key


async def test_patch_chat_model_reprobes(api_harness, behavior, monkeypatch) -> None:
    harness = await api_harness()
    created = (await harness.client.post("/api/llm/profiles", json=_payload())).json()
    calls = _spy_invalidate(harness.registry, monkeypatch)
    chat_calls_before = behavior.chat_calls

    response = await harness.client.patch(
        f"/api/llm/profiles/{created['id']}", json={"chat_model": "mock-chat-v2"}
    )

    assert response.status_code == 200
    assert response.json()["chat_model"] == "mock-chat-v2"
    assert len(calls) == 1
    assert behavior.chat_calls == chat_calls_before + 4


async def test_patch_base_url_revalidates(api_harness) -> None:
    harness = await api_harness()
    created = (await harness.client.post("/api/llm/profiles", json=_payload())).json()

    response = await harness.client.patch(
        f"/api/llm/profiles/{created['id']}",
        json={"base_url": "http://169.254.169.254/v1"},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "unsafe_url"


async def test_patch_probe_failure_returns_422(api_harness, behavior) -> None:
    harness = await api_harness()
    created = (await harness.client.post("/api/llm/profiles", json=_payload())).json()
    behavior.expected_api_key = "rotated-elsewhere"

    response = await harness.client.patch(
        f"/api/llm/profiles/{created['id']}", json={"api_key": "sk-bad-rotated-key-000000"}
    )

    assert response.status_code == 422
    assert response.json()["error"] == "llm_probe_failed"


async def test_patch_not_found(api_harness) -> None:
    harness = await api_harness()

    response = await harness.client.patch(f"/api/llm/profiles/{uuid.uuid4()}", json={"name": "x"})

    assert response.status_code == 404


async def test_delete_profile(api_harness, monkeypatch) -> None:
    harness = await api_harness()
    created = (await harness.client.post("/api/llm/profiles", json=_payload())).json()
    calls = _spy_invalidate(harness.registry, monkeypatch)

    response = await harness.client.delete(f"/api/llm/profiles/{created['id']}")

    assert response.status_code == 204
    assert response.text == ""
    assert len(calls) == 1
    assert harness.session.profiles == {}
    assert (await harness.client.get(f"/api/llm/profiles/{created['id']}")).status_code == 404


async def test_delete_not_found(api_harness) -> None:
    harness = await api_harness()

    response = await harness.client.delete(f"/api/llm/profiles/{uuid.uuid4()}")

    assert response.status_code == 404


async def test_patch_max_context_tokens_only_skips_reprobe(
    api_harness, behavior, monkeypatch
) -> None:
    harness = await api_harness()
    created = (await harness.client.post("/api/llm/profiles", json=_payload())).json()
    calls = _spy_invalidate(harness.registry, monkeypatch)
    chat_calls_before = behavior.chat_calls

    response = await harness.client.patch(
        f"/api/llm/profiles/{created['id']}", json={"max_context_tokens": 4096}
    )

    assert response.status_code == 200
    assert response.json()["max_context_tokens"] == 4096
    assert calls == []
    assert behavior.chat_calls == chat_calls_before


async def test_delete_profile_without_api_key(api_harness, behavior) -> None:
    behavior.require_api_key = False
    harness = await api_harness()
    created = (
        await harness.client.post("/api/llm/profiles", json=_payload(api_key=None, name="local"))
    ).json()
    assert created["api_key_masked"] is None

    response = await harness.client.delete(f"/api/llm/profiles/{created['id']}")

    assert response.status_code == 204
    assert harness.session.profiles == {}


async def test_delete_profile_tolerates_decrypt_failure(api_harness, monkeypatch) -> None:
    """A profile whose key cannot be decrypted must still be deletable."""
    harness = await api_harness()
    created = (await harness.client.post("/api/llm/profiles", json=_payload())).json()

    def _boom(_token):
        raise ValueError("rotated SECRET_KEY")

    monkeypatch.setattr(harness.secret_box, "decrypt", _boom)

    response = await harness.client.delete(f"/api/llm/profiles/{created['id']}")

    assert response.status_code == 204
    assert harness.session.profiles == {}


async def test_test_endpoint_refreshes_capabilities(api_harness, behavior) -> None:
    harness = await api_harness()
    created = (await harness.client.post("/api/llm/profiles", json=_payload())).json()
    assert created["capabilities"]["json_mode"] is True

    behavior.supports_json_mode = False
    behavior.supports_tools = False

    response = await harness.client.post(f"/api/llm/profiles/{created['id']}/test")

    assert response.status_code == 200, response.text
    caps = response.json()["capabilities"]
    assert caps["json_mode"] is False
    assert caps["tool_calling"] is False
    _assert_no_secret(response, RAW_KEY)


async def test_test_endpoint_not_found(api_harness) -> None:
    harness = await api_harness()

    response = await harness.client.post(f"/api/llm/profiles/{uuid.uuid4()}/test")

    assert response.status_code == 404


def _spy_invalidate(registry, monkeypatch) -> list:
    calls: list = []
    original = registry.invalidate

    async def _spy(config):
        calls.append(config)
        return await original(config)

    monkeypatch.setattr(registry, "invalidate", _spy)
    return calls
