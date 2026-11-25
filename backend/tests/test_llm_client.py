"""Unit tests for ``app.llm.client.LLMClient`` against the in-process mock provider."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from app.core.errors import (
    LLMAuthError,
    LLMBadRequestError,
    LLMError,
    LLMInvalidResponseError,
    LLMNotFoundError,
    LLMRateLimitError,
    LLMUnavailableError,
)
from app.llm.client import LLMClient
from app.llm.types import StreamDone, TextDelta, ToolCallsReady, ToolSpec

TIME_TOOL = ToolSpec(
    name="get_current_time",
    description="Return the current UTC date and time.",
    parameters={"type": "object", "properties": {}, "required": []},
)


async def test_chat_plain(llm_client: LLMClient, behavior) -> None:
    behavior.reply_text = "hello there"

    result = await llm_client.chat([{"role": "user", "content": "hi"}])

    assert result.content == "hello there"
    assert result.tool_calls == []
    assert result.finish_reason == "stop"
    assert result.model == "mock-chat"
    assert result.usage is not None and result.usage["total_tokens"] == 10
    assert behavior.requests[0]["model"] == "mock-chat"
    assert behavior.requests[0]["stream"] is False


async def test_chat_uses_model_override(llm_client: LLMClient, behavior) -> None:
    await llm_client.chat([{"role": "user", "content": "hi"}], model="other-model")

    assert behavior.requests[0]["model"] == "other-model"


async def test_chat_tool_call(llm_client: LLMClient, behavior) -> None:
    result = await llm_client.chat(
        [{"role": "user", "content": "time?"}],
        tools=[TIME_TOOL],
        tool_choice="auto",
    )

    assert result.content is None
    assert result.finish_reason == "tool_calls"
    assert len(result.tool_calls) == 1
    call = result.tool_calls[0]
    assert call.name == "get_current_time"
    assert call.arguments == {"tz": "UTC"}
    assert call.raw_arguments == '{"tz": "UTC"}'
    assert call.id == "call_1"
    payload = behavior.requests[0]
    assert payload["tools"][0]["function"]["name"] == "get_current_time"
    assert payload["tool_choice"] == "auto"


async def test_chat_tool_arguments_malformed(llm_client: LLMClient, behavior) -> None:
    behavior.malformed_tool_args = True

    with pytest.raises(LLMInvalidResponseError) as excinfo:
        await llm_client.chat([{"role": "user", "content": "time?"}], tools=[TIME_TOOL])

    assert "get_current_time" in str(excinfo.value)
    assert excinfo.value.raw == "{this is not json"


async def test_chat_tool_arguments_non_object(llm_client: LLMClient, behavior) -> None:
    behavior.tool_args_non_object = True

    with pytest.raises(LLMInvalidResponseError, match="JSON object") as excinfo:
        await llm_client.chat([{"role": "user", "content": "time?"}], tools=[TIME_TOOL])

    assert excinfo.value.raw == "[1, 2]"


async def test_chat_empty_choices_raises_invalid_response(llm_client: LLMClient, behavior) -> None:
    behavior.empty_choices = True

    with pytest.raises(LLMInvalidResponseError, match="no choices"):
        await llm_client.chat([{"role": "user", "content": "hi"}])


async def test_chat_stream_propagates_usage(llm_client: LLMClient, behavior) -> None:
    behavior.stream_usage_chunk = True

    events = [e async for e in llm_client.chat_stream([{"role": "user", "content": "hi"}])]

    assert isinstance(events[-1], StreamDone)
    usage = events[-1].usage
    assert usage is not None
    assert usage["prompt_tokens"] == 7
    assert usage["completion_tokens"] == 3
    assert usage["total_tokens"] == 10
    assert events[-1].finish_reason == "stop"


async def test_chat_stream_empty_stream_still_terminates(llm_client: LLMClient, behavior) -> None:
    behavior.stream_empty = True

    events = [e async for e in llm_client.chat_stream([{"role": "user", "content": "hi"}])]

    assert len(events) == 1
    assert isinstance(events[0], StreamDone)
    assert events[0].finish_reason is None


async def test_chat_stream_text(llm_client: LLMClient) -> None:
    events = [e async for e in llm_client.chat_stream([{"role": "user", "content": "hi"}])]

    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert text == "ok"
    assert isinstance(events[-1], StreamDone)
    assert events[-1].finish_reason == "stop"
    assert not any(isinstance(e, ToolCallsReady) for e in events)


async def test_chat_stream_tool_calls_with_index(llm_client: LLMClient, behavior) -> None:
    events = [
        e
        async for e in llm_client.chat_stream(
            [{"role": "user", "content": "time?"}], tools=[TIME_TOOL]
        )
    ]

    ready = [e for e in events if isinstance(e, ToolCallsReady)]
    assert len(ready) == 1
    call = ready[0].tool_calls[0]
    assert call.name == "get_current_time"
    assert call.arguments == {"tz": "UTC"}
    assert call.id == "call_stream_1"
    assert behavior.requests[0]["stream"] is True
    assert isinstance(events[-1], StreamDone)
    assert events[-1].finish_reason == "tool_calls"


async def test_chat_stream_merges_two_tool_calls_by_index(llm_client: LLMClient, behavior) -> None:
    behavior.stream_tool_call_count = 2
    second_tool = ToolSpec(name="get_product", description="Fetch a product.")

    events = [
        e
        async for e in llm_client.chat_stream(
            [{"role": "user", "content": "time and product?"}], tools=[TIME_TOOL, second_tool]
        )
    ]

    ready = [e for e in events if isinstance(e, ToolCallsReady)]
    assert len(ready) == 1
    calls = ready[0].tool_calls
    assert [c.name for c in calls] == ["get_current_time", "get_product"]
    assert [c.id for c in calls] == ["call_stream_1", "call_stream_2"]
    # Each call must keep its own arguments; merging would break the JSON.
    assert all(c.arguments == {"tz": "UTC"} for c in calls)


async def test_chat_stream_tool_calls_without_index(llm_client: LLMClient, behavior) -> None:
    """Providers that omit the delta ``index`` (SDK leaves the attr unset)."""
    behavior.stream_tool_calls_without_index = True

    events = [
        e
        async for e in llm_client.chat_stream(
            [{"role": "user", "content": "time?"}], tools=[TIME_TOOL]
        )
    ]

    ready = [e for e in events if isinstance(e, ToolCallsReady)]
    assert len(ready) == 1
    call = ready[0].tool_calls[0]
    assert call.name == "get_current_time"
    assert call.arguments == {"tz": "UTC"}
    assert call.id == "call_stream_1"


async def test_chat_stream_tool_calls_without_index_or_id(llm_client: LLMClient, behavior) -> None:
    """Last-resort accumulation path: neither ``index`` nor ``id`` supplied."""
    behavior.stream_tool_calls_without_index = True
    behavior.stream_omit_tool_ids = True

    events = [
        e
        async for e in llm_client.chat_stream(
            [{"role": "user", "content": "time?"}], tools=[TIME_TOOL]
        )
    ]

    ready = [e for e in events if isinstance(e, ToolCallsReady)]
    assert len(ready) == 1
    call = ready[0].tool_calls[0]
    assert call.name == "get_current_time"
    assert call.arguments == {"tz": "UTC"}
    assert call.id.startswith("call_")


async def test_chat_stream_raises_on_malformed_arguments(llm_client: LLMClient, behavior) -> None:
    behavior.malformed_tool_args = True

    with pytest.raises(LLMInvalidResponseError):
        async for _ in llm_client.chat_stream(
            [{"role": "user", "content": "time?"}], tools=[TIME_TOOL]
        ):
            pass


async def test_list_models(llm_client: LLMClient, behavior) -> None:
    behavior.models = ["a", "b"]

    assert await llm_client.list_models() == ["a", "b"]


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, LLMAuthError),
        (404, LLMNotFoundError),
        (429, LLMRateLimitError),
        (500, LLMUnavailableError),
    ],
)
async def test_error_mapping(
    llm_client: LLMClient,
    behavior,
    status: int,
    expected: type[LLMError],
) -> None:
    behavior.fail_status = status

    with pytest.raises(expected) as excinfo:
        await llm_client.chat([{"role": "user", "content": "hi"}])

    assert excinfo.value.status_code == status
    assert behavior.fail_message in str(excinfo.value)


async def test_error_mapping_stream(llm_client: LLMClient, behavior) -> None:
    behavior.fail_status = 503

    with pytest.raises(LLMUnavailableError):
        async for _ in llm_client.chat_stream([{"role": "user", "content": "hi"}]):
            pass


async def test_embed_batches_at_64(config_factory, mock_http, behavior) -> None:
    behavior.embeddings = True
    behavior.embedding_dim = 4
    client = LLMClient(
        config_factory(embedding_model="mock-embed"),
        http_client=mock_http,
    )

    vectors = await client.embed([f"text-{i}" for i in range(70)])

    assert len(vectors) == 70
    assert all(len(vector) == 4 for vector in vectors)
    assert [len(batch) for batch in behavior.embedding_requests] == [64, 6]
    assert behavior.embedding_requests[0][0] == "text-0"
    assert behavior.embedding_requests[0][-1] == "text-63"
    assert behavior.embedding_requests[1] == [f"text-{i}" for i in range(64, 70)]


async def test_embed_requires_model(llm_client: LLMClient) -> None:
    with pytest.raises(LLMBadRequestError, match="embedding_model"):
        await llm_client.embed(["hello"])


async def test_embed_empty_input(llm_client: LLMClient) -> None:
    assert await llm_client.embed([]) == []


async def test_embed_error_mapping(config_factory, mock_http, behavior) -> None:
    behavior.embeddings = False  # provider answers 404
    client = LLMClient(
        config_factory(embedding_model="mock-embed"),
        http_client=mock_http,
    )

    with pytest.raises(LLMNotFoundError):
        await client.embed(["hello"])


async def test_aclose_closes_transport(config_factory, behavior) -> None:
    import httpx

    from tests.mock_openai import build_mock_app

    http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_mock_app(behavior)),
        base_url="http://mock",
    )
    client = LLMClient(config_factory(), http_client=http_client)

    await client.aclose()

    assert http_client.is_closed


def test_config_repr_does_not_leak_key(config_factory) -> None:
    config = config_factory(api_key=SecretStr("sk-super-secret-value-123456"))

    assert "sk-super-secret-value" not in repr(config)
    assert "sk-super-secret-value" not in str(config)
    assert "***" in repr(config)
