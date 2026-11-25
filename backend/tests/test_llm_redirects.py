"""F1/F4 regression tests: redirect refusal on the LLM client and tool-name assembly.

The provider is a ``httpx.MockTransport`` handler, so these tests never open a
socket. Redirect payloads deliberately point at SSRF-sensitive targets to prove
the client refuses to follow them.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.core.errors import LLMBadRequestError
from app.llm.client import LLMClient
from app.llm.types import StreamDone, TextDelta, ToolCallsReady
from tests.conftest import TEST_RAW_API_KEY

METADATA_LOCATION = "http://169.254.169.254/latest/meta-data/iam/security-credentials/"
PRIVATE_LOCATION = "http://10.0.0.5:8080/internal/v1"


def _redirect_client(config_factory, *, status_code: int = 302, location: str) -> LLMClient:
    """Build an ``LLMClient`` whose provider always answers with a redirect."""

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, headers={"location": location})

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="http://mock",
        follow_redirects=False,
    )
    return LLMClient(config_factory(), http_client=http_client)


# ---------------------------------------------------------------------------
# F1 — 3xx from the provider must surface as LLMBadRequestError
# ---------------------------------------------------------------------------


async def test_list_models_rejects_provider_redirect(config_factory) -> None:
    client = _redirect_client(config_factory, location=METADATA_LOCATION)

    with pytest.raises(LLMBadRequestError) as excinfo:
        await client.list_models()

    message = str(excinfo.value)
    assert "redirect" in message.lower()
    assert "not followed" in message
    assert METADATA_LOCATION in message
    assert excinfo.value.status_code == 302


async def test_chat_rejects_provider_redirect(config_factory) -> None:
    client = _redirect_client(config_factory, location=PRIVATE_LOCATION)

    with pytest.raises(LLMBadRequestError) as excinfo:
        await client.chat([{"role": "user", "content": "hi"}])

    assert PRIVATE_LOCATION in str(excinfo.value)


async def test_chat_stream_rejects_provider_redirect(config_factory) -> None:
    client = _redirect_client(config_factory, location=METADATA_LOCATION)

    with pytest.raises(LLMBadRequestError):
        async for _event in client.chat_stream([{"role": "user", "content": "hi"}]):
            pass


async def test_embed_rejects_provider_redirect(config_factory) -> None:
    client = _redirect_client(config_factory, location=METADATA_LOCATION)

    with pytest.raises(LLMBadRequestError):
        await client.embed(["hello"])


@pytest.mark.parametrize("status_code", [301, 302, 303, 307, 308])
async def test_redirect_status_codes_all_map_to_bad_request(config_factory, status_code) -> None:
    client = _redirect_client(config_factory, status_code=status_code, location=METADATA_LOCATION)

    with pytest.raises(LLMBadRequestError) as excinfo:
        await client.chat([{"role": "user", "content": "hi"}])

    assert f"HTTP {status_code}" in str(excinfo.value)


async def test_redirect_error_does_not_leak_api_key(config_factory) -> None:
    leaked_location = f"http://attacker.example/steal?token={TEST_RAW_API_KEY}"
    client = _redirect_client(config_factory, location=leaked_location)

    with pytest.raises(LLMBadRequestError) as excinfo:
        await client.chat([{"role": "user", "content": "hi"}])

    message = str(excinfo.value)
    assert TEST_RAW_API_KEY not in message
    assert "[REDACTED]" in message


# ---------------------------------------------------------------------------
# F1 — owned vs injected HTTP client lifetime
# ---------------------------------------------------------------------------


async def test_owned_http_client_disables_redirects_and_is_closed(config_factory) -> None:
    client = LLMClient(config_factory(timeout=7.5))

    assert isinstance(client._http_client, httpx.AsyncClient)  # noqa: SLF001
    assert client._http_client.follow_redirects is False  # noqa: SLF001
    assert client._http_client.timeout.connect == 7.5  # noqa: SLF001
    assert not client._http_client.is_closed  # noqa: SLF001

    await client.aclose()

    assert client._http_client.is_closed  # noqa: SLF001


async def test_aclose_does_not_close_injected_http_client(config_factory, mock_http) -> None:
    client = LLMClient(config_factory(), http_client=mock_http)

    await client.aclose()

    assert not mock_http.is_closed
    # The injected client stays usable by its owner after the wrapper is gone.
    assert await client.list_models() == ["mock-chat", "mock-embed"]


async def test_owned_and_injected_clients_are_distinct_objects(config_factory, mock_http) -> None:
    owned = LLMClient(config_factory())
    injected = LLMClient(config_factory(), http_client=mock_http)

    assert owned._http_client is not mock_http  # noqa: SLF001
    assert injected._http_client is mock_http  # noqa: SLF001

    await owned.aclose()
    await injected.aclose()


# ---------------------------------------------------------------------------
# F4 — tool call name assembly while streaming
# ---------------------------------------------------------------------------


def _chunk(delta: dict[str, Any], finish_reason: str | None = None) -> dict[str, Any]:
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "mock-chat",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def _sse(chunks: list[dict[str, Any]]) -> httpx.Response:
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    body += "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)


def _stream_client(config_factory, chunks: list[dict[str, Any]]) -> LLMClient:
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: _sse(chunks)),
        base_url="http://mock",
        follow_redirects=False,
    )
    return LLMClient(config_factory(), http_client=http_client)


async def _streamed_tool_calls(client: LLMClient) -> tuple[list[Any], list[Any]]:
    events = [event async for event in client.chat_stream([{"role": "user", "content": "hi"}])]
    ready = [event for event in events if isinstance(event, ToolCallsReady)]
    assert len(ready) == 1
    assert isinstance(events[-1], StreamDone)
    return ready[0].tool_calls, events


async def test_stream_repeated_full_tool_name_is_not_duplicated(config_factory) -> None:
    """OpenAI-style providers resend the complete name on every delta."""
    chunks = [
        _chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_current_time", "arguments": '{"tz"'},
                    }
                ]
            }
        ),
        _chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_current_time", "arguments": ': "UTC"}'},
                    }
                ]
            }
        ),
        _chunk({}, finish_reason="tool_calls"),
    ]
    client = _stream_client(config_factory, chunks)

    calls, _events = await _streamed_tool_calls(client)

    assert [call.name for call in calls] == ["get_current_time"]
    assert calls[0].arguments == {"tz": "UTC"}
    assert calls[0].id == "call_1"


async def test_stream_tool_name_split_across_chunks_is_joined(config_factory) -> None:
    chunks = [
        _chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_", "arguments": ""},
                    }
                ]
            }
        ),
        _chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "time", "arguments": '{"tz": "UTC"}'},
                    }
                ]
            }
        ),
        _chunk({}, finish_reason="tool_calls"),
    ]
    client = _stream_client(config_factory, chunks)

    calls, _events = await _streamed_tool_calls(client)

    assert [call.name for call in calls] == ["get_time"]
    assert calls[0].arguments == {"tz": "UTC"}


async def test_stream_tool_name_split_after_empty_first_delta(config_factory) -> None:
    """First delta opens the slot without a name, the next one provides it."""
    chunks = [
        _chunk(
            {
                "tool_calls": [
                    {"index": 0, "id": "call_1", "type": "function", "function": {"arguments": ""}}
                ]
            }
        ),
        _chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_time", "arguments": "{}"},
                    }
                ]
            }
        ),
        _chunk({}, finish_reason="tool_calls"),
    ]
    client = _stream_client(config_factory, chunks)

    calls, _events = await _streamed_tool_calls(client)

    assert [call.name for call in calls] == ["get_time"]
    assert calls[0].arguments == {}


async def test_stream_text_still_works_on_mock_transport(config_factory) -> None:
    """Guard for the SSE helper itself: plain text deltas are unaffected."""
    chunks = [
        _chunk({"role": "assistant", "content": ""}),
        _chunk({"content": "hel"}),
        _chunk({"content": "lo"}),
        _chunk({}, finish_reason="stop"),
    ]
    client = _stream_client(config_factory, chunks)

    events = [event async for event in client.chat_stream([{"role": "user", "content": "hi"}])]

    text = "".join(event.text for event in events if isinstance(event, TextDelta))
    assert text == "hello"
    assert isinstance(events[-1], StreamDone)
    assert not any(isinstance(event, ToolCallsReady) for event in events)
