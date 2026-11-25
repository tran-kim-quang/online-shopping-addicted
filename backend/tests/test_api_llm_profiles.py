"""Unit tests for the ``/api/llm`` router (mock provider + in-memory session)."""

from __future__ import annotations

import uuid

import pytest

from app.api.deps import get_settings as deps_get_settings
from app.core.config import get_settings
from app.security.secrets import mask_secret
from tests.conftest import TEST_RAW_API_KEY

RAW_KEY = TEST_RAW_API_KEY  # "sk-raw-secret-abcdef1234567890"
# Simulates rotating SECRET_KEY in the environment: tokens sealed with the old
# key can no longer be decrypted.
ROTATED_SECRET_KEY = "r4e8G9U2I3ro73oyQB9zuCbKFZMLYVCGDDJIpTLk6lQ="
ROTATED_RAW_KEY = "sk-rotated-key-9876543210"


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


# ---------------------------------------------------------------------------
# F2 — rotated SECRET_KEY: stored tokens become undecryptable
# ---------------------------------------------------------------------------


def _rotate_secret_key(harness) -> None:
    """Point the app's SecretBox at a new key, as a SECRET_KEY rotation would."""
    harness.secret_box.init(ROTATED_SECRET_KEY)


async def test_profile_without_api_key_reports_status_none(api_harness, behavior) -> None:
    behavior.require_api_key = False
    harness = await api_harness()

    created = (
        await harness.client.post(
            "/api/llm/profiles", json=_payload(api_key=None, name="keyless profile")
        )
    ).json()

    assert created["api_key_status"] == "none"
    assert created["api_key_masked"] is None

    fetched = await harness.client.get(f"/api/llm/profiles/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["api_key_status"] == "none"


async def test_patch_keyless_profile_still_works(api_harness, behavior) -> None:
    """A profile without a stored key has nothing to decrypt, so PATCH must not 409."""
    behavior.require_api_key = False
    harness = await api_harness()
    created = (
        await harness.client.post(
            "/api/llm/profiles", json=_payload(api_key=None, name="local ollama")
        )
    ).json()

    response = await harness.client.patch(
        f"/api/llm/profiles/{created['id']}", json={"name": "renamed ollama"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "renamed ollama"
    assert response.json()["api_key_status"] == "none"


async def test_rotated_secret_key_keeps_list_and_detail_at_200(api_harness) -> None:
    harness = await api_harness()
    created = (await harness.client.post("/api/llm/profiles", json=_payload())).json()
    assert created["api_key_status"] == "set"

    _rotate_secret_key(harness)

    listed = await harness.client.get("/api/llm/profiles")
    fetched = await harness.client.get(f"/api/llm/profiles/{created['id']}")

    assert listed.status_code == 200, listed.text
    assert fetched.status_code == 200, fetched.text

    listed_item = listed.json()[0]
    assert listed_item["api_key_status"] == "undecryptable"
    assert listed_item["api_key_masked"] is None
    assert fetched.json()["api_key_status"] == "undecryptable"
    assert fetched.json()["api_key_masked"] is None

    stored = harness.session.profiles[uuid.UUID(created["id"])]
    assert stored.api_key_enc is not None
    assert stored.api_key_enc not in listed.text
    assert stored.api_key_enc not in fetched.text
    _assert_no_secret(listed, RAW_KEY)
    _assert_no_secret(fetched, RAW_KEY)


async def test_broken_profile_does_not_hide_healthy_rows(api_harness) -> None:
    harness = await api_harness()
    await harness.client.post("/api/llm/profiles", json=_payload(name="sealed-with-old-key"))
    _rotate_secret_key(harness)
    await harness.client.post("/api/llm/profiles", json=_payload(name="sealed-with-new-key"))

    response = await harness.client.get("/api/llm/profiles")

    assert response.status_code == 200, response.text
    by_name = {item["name"]: item for item in response.json()}
    assert set(by_name) == {"sealed-with-old-key", "sealed-with-new-key"}
    assert by_name["sealed-with-old-key"]["api_key_status"] == "undecryptable"
    assert by_name["sealed-with-old-key"]["api_key_masked"] is None
    assert by_name["sealed-with-new-key"]["api_key_status"] == "set"
    assert by_name["sealed-with-new-key"]["api_key_masked"] == mask_secret(RAW_KEY)
    _assert_no_secret(response, RAW_KEY)


async def test_test_endpoint_returns_409_when_key_undecryptable(api_harness, behavior) -> None:
    harness = await api_harness()
    created = (await harness.client.post("/api/llm/profiles", json=_payload())).json()
    stored = harness.session.profiles[uuid.UUID(created["id"])]
    _rotate_secret_key(harness)
    chat_calls_before = behavior.chat_calls

    response = await harness.client.post(f"/api/llm/profiles/{created['id']}/test")

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["error"] == "secret_key_mismatch"
    assert "Re-enter the API key" in body["detail"]
    assert behavior.chat_calls == chat_calls_before  # provider never contacted
    assert stored.api_key_enc not in response.text
    _assert_no_secret(response, RAW_KEY)


async def test_patch_without_new_key_returns_409_when_undecryptable(api_harness, behavior) -> None:
    harness = await api_harness()
    created = (await harness.client.post("/api/llm/profiles", json=_payload())).json()
    stored = harness.session.profiles[uuid.UUID(created["id"])]
    original_name = stored.name
    _rotate_secret_key(harness)
    chat_calls_before = behavior.chat_calls

    response = await harness.client.patch(
        f"/api/llm/profiles/{created['id']}", json={"name": "renamed"}
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"] == "secret_key_mismatch"
    assert stored.name == original_name  # nothing persisted
    assert behavior.chat_calls == chat_calls_before
    _assert_no_secret(response, RAW_KEY)


async def test_patch_with_new_key_recovers_undecryptable_profile(
    api_harness, behavior, monkeypatch
) -> None:
    harness = await api_harness()
    created = (await harness.client.post("/api/llm/profiles", json=_payload())).json()
    stored = harness.session.profiles[uuid.UUID(created["id"])]
    old_token = stored.api_key_enc
    _rotate_secret_key(harness)
    calls = _spy_invalidate(harness.registry, monkeypatch)
    behavior.expected_api_key = ROTATED_RAW_KEY
    chat_calls_before = behavior.chat_calls

    response = await harness.client.patch(
        f"/api/llm/profiles/{created['id']}", json={"api_key": ROTATED_RAW_KEY}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["api_key_status"] == "set"
    assert body["api_key_masked"] == mask_secret(ROTATED_RAW_KEY)
    assert stored.api_key_enc != old_token
    assert harness.secret_box.decrypt(stored.api_key_enc) == ROTATED_RAW_KEY
    # The old key is unavailable, so cache invalidation is skipped on purpose.
    assert calls == []
    assert behavior.chat_calls == chat_calls_before + 4  # re-probed with the new key
    _assert_no_secret(response, RAW_KEY, ROTATED_RAW_KEY)


async def test_delete_undecryptable_profile(api_harness) -> None:
    harness = await api_harness()
    created = (await harness.client.post("/api/llm/profiles", json=_payload())).json()
    _rotate_secret_key(harness)

    response = await harness.client.delete(f"/api/llm/profiles/{created['id']}")

    assert response.status_code == 204
    assert response.text == ""
    assert harness.session.profiles == {}


# --- Round 4: stored key must not follow base_url to another host ---------------------


async def test_patch_host_change_without_api_key_is_rejected(api_harness, behavior) -> None:
    harness = await api_harness()
    created = (await harness.client.post("/api/llm/profiles", json=_payload())).json()
    chat_calls_before = behavior.chat_calls

    response = await harness.client.patch(
        f"/api/llm/profiles/{created['id']}", json={"base_url": "http://other-provider:9999"}
    )

    assert response.status_code == 422
    assert response.json()["error"] == "api_key_required_on_host_change"
    assert behavior.chat_calls == chat_calls_before  # old key never sent anywhere
    stored = next(iter(harness.session.profiles.values()))
    assert stored.base_url == created["base_url"]
    _assert_no_secret(response, RAW_KEY)


async def test_patch_port_change_counts_as_host_change(api_harness) -> None:
    harness = await api_harness()
    created = (await harness.client.post("/api/llm/profiles", json=_payload())).json()

    response = await harness.client.patch(
        f"/api/llm/profiles/{created['id']}", json={"base_url": "http://mock:1234"}
    )

    assert response.status_code == 422
    assert response.json()["error"] == "api_key_required_on_host_change"


async def test_patch_same_host_path_change_keeps_key(api_harness) -> None:
    harness = await api_harness()
    created = (await harness.client.post("/api/llm/profiles", json=_payload())).json()

    response = await harness.client.patch(
        f"/api/llm/profiles/{created['id']}", json={"base_url": "HTTP://MOCK:9999/v1/"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["api_key_status"] == "set"


async def test_patch_host_change_with_new_key_succeeds(api_harness, behavior) -> None:
    harness = await api_harness()
    created = (await harness.client.post("/api/llm/profiles", json=_payload())).json()
    new_key = "sk-other-provider-key-1111"
    behavior.expected_api_key = new_key

    response = await harness.client.patch(
        f"/api/llm/profiles/{created['id']}",
        json={"base_url": "http://other-provider:9999", "api_key": new_key},
    )

    assert response.status_code == 200, response.text
    assert response.json()["base_url"].startswith("http://other-provider:9999")
    stored = next(iter(harness.session.profiles.values()))
    assert harness.secret_box.decrypt(stored.api_key_enc) == new_key


async def test_patch_host_change_with_null_key_clears_key(api_harness, behavior) -> None:
    harness = await api_harness()
    created = (await harness.client.post("/api/llm/profiles", json=_payload())).json()
    behavior.require_api_key = False

    response = await harness.client.patch(
        f"/api/llm/profiles/{created['id']}",
        json={"base_url": "http://other-provider:9999", "api_key": None},
    )

    assert response.status_code == 200, response.text
    assert response.json()["api_key_status"] == "none"


async def test_patch_host_change_allowed_when_profile_has_no_key(api_harness, behavior) -> None:
    behavior.require_api_key = False
    harness = await api_harness()
    created = (await harness.client.post("/api/llm/profiles", json=_payload(api_key=None))).json()

    response = await harness.client.patch(
        f"/api/llm/profiles/{created['id']}", json={"base_url": "http://other-provider:9999"}
    )

    assert response.status_code == 200, response.text
