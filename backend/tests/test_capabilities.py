"""Unit tests for ``app.llm.capabilities.probe``."""

from __future__ import annotations

from pydantic import SecretStr

from app.llm.capabilities import probe
from app.llm.client import LLMClient


async def test_probe_full_provider(behavior, llm_client: LLMClient, config_factory) -> None:
    behavior.embeddings = True
    behavior.embedding_dim = 16
    config = config_factory(embedding_model="mock-embed")

    caps = await probe(llm_client, config, timeout=5)

    assert caps.models_endpoint is True
    assert caps.models == ["mock-chat", "mock-embed"]
    assert caps.chat is True
    assert caps.tool_calling is True
    assert caps.json_mode is True
    assert caps.streaming is True
    assert caps.embeddings is True
    assert caps.embedding_dim == 16
    assert caps.errors == {}
    assert caps.probed_at.tzinfo is not None
    # Tool probe used the documented tool name with tool_choice="auto".
    tool_request = next(r for r in behavior.requests if r.get("tools"))
    assert tool_request["tools"][0]["function"]["name"] == "get_current_time"
    assert tool_request["tool_choice"] == "auto"


async def test_probe_without_tool_support(behavior, llm_client, config_factory) -> None:
    behavior.supports_tools = False

    caps = await probe(llm_client, config_factory(), timeout=5)

    assert caps.chat is True
    assert caps.tool_calling is False
    assert "tool_calling" in caps.errors
    # The remaining probes still run.
    assert caps.json_mode is True
    assert caps.streaming is True


async def test_probe_without_json_mode(behavior, llm_client, config_factory) -> None:
    behavior.supports_json_mode = False

    caps = await probe(llm_client, config_factory(), timeout=5)

    assert caps.json_mode is False
    assert "json_mode" in caps.errors
    assert "response_format" in caps.errors["json_mode"]
    assert caps.streaming is True


async def test_probe_without_models_endpoint(behavior, llm_client, config_factory) -> None:
    behavior.models_endpoint = False

    caps = await probe(llm_client, config_factory(), timeout=5)

    assert caps.models_endpoint is False
    assert caps.models == []
    assert "models" in caps.errors
    assert caps.chat is True  # a missing /models endpoint does not fail the probe


async def test_probe_without_embeddings(behavior, llm_client, config_factory) -> None:
    behavior.embeddings = False

    caps = await probe(llm_client, config_factory(embedding_model="mock-embed"), timeout=5)

    assert caps.embeddings is False
    assert caps.embedding_dim is None
    assert "embeddings" in caps.errors


async def test_probe_skips_embedding_step_when_not_configured(
    behavior, llm_client, config_factory
) -> None:
    behavior.embeddings = True

    caps = await probe(llm_client, config_factory(), timeout=5)

    assert caps.embeddings is False
    assert "embeddings" not in caps.errors
    assert behavior.embedding_requests == []


async def test_probe_reports_empty_chat_response(behavior, llm_client, config_factory) -> None:
    behavior.empty_content = True

    caps = await probe(llm_client, config_factory(), timeout=5)

    assert caps.chat is False
    assert "Empty response" in caps.errors["chat"]


async def test_probe_reports_non_object_json(behavior, llm_client, config_factory) -> None:
    behavior.scripted_texts = ["[1, 2, 3]"]

    caps = await probe(llm_client, config_factory(), timeout=5)

    assert caps.json_mode is False
    assert "not a JSON object" in caps.errors["json_mode"]


async def test_probe_reports_empty_embedding(behavior, llm_client, config_factory) -> None:
    behavior.embeddings = True
    behavior.embedding_dim = 0

    caps = await probe(llm_client, config_factory(embedding_model="mock-embed"), timeout=5)

    assert caps.embeddings is False
    assert caps.embedding_dim is None
    assert "Empty embedding vector" in caps.errors["embeddings"]


async def test_probe_stops_early_on_bad_credentials(behavior, llm_client, config_factory) -> None:
    raw_key = "sk-wrong-credential-0123456789"
    behavior.expected_api_key = "a-different-key"

    caps = await probe(llm_client, config_factory(api_key=SecretStr(raw_key)), timeout=5)

    assert caps.chat is False
    assert caps.tool_calling is False
    assert caps.json_mode is False
    assert caps.streaming is False
    assert "chat" in caps.errors
    assert behavior.chat_calls == 1  # stopped right after the chat step
    for message in caps.errors.values():
        assert raw_key not in message
        assert "sk-wrong" not in message


async def test_probe_stops_early_on_unknown_model(behavior, llm_client, config_factory) -> None:
    behavior.fail_status = 404

    caps = await probe(llm_client, config_factory(), timeout=5)

    assert caps.chat is False
    assert behavior.chat_calls == 1
    assert "chat" in caps.errors


async def test_probe_records_unavailable_error(behavior, llm_client, config_factory) -> None:
    behavior.fail_status = 500

    caps = await probe(llm_client, config_factory(), timeout=5)

    assert caps.chat is False
    assert caps.tool_calling is False
    assert caps.errors["chat"]
    # A 5xx is not an auth/not-found error, so all remaining steps are attempted.
    assert behavior.chat_calls >= 3
