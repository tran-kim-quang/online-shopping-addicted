"""Configurable in-process mock of an OpenAI-compatible provider.

The mock is a FastAPI ASGI app so it can be served by ``httpx.ASGITransport``
without opening a real socket: unit tests never touch the network.

Usage::

    behavior = MockBehavior(supports_tools=False)
    app = build_mock_app(behavior)
    http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://mock"
    )
    client = LLMClient(config, http_client=http_client)

Everything the mock did (request bodies, call counts) is recorded on the
``MockBehavior`` instance that was passed in, so tests can assert on prompts
and on batching behaviour.
"""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

CHAT_MODEL = "mock-chat"
EMBED_MODEL = "mock-embed"
DEFAULT_TOOL_ARGS = {"tz": "UTC"}


class MockBehavior(BaseModel):
    """Knobs that change how the mock provider behaves.

    Mutable on purpose: tests flip attributes between requests and inspect
    ``requests`` / ``chat_calls`` afterwards.
    """

    # --- auth -------------------------------------------------------------
    require_api_key: bool = True
    expected_api_key: str = "sk-test-key-1234567890"

    # --- feature availability --------------------------------------------
    models_endpoint: bool = True
    models: list[str] = Field(default_factory=lambda: [CHAT_MODEL, EMBED_MODEL])
    supports_tools: bool = True
    supports_json_mode: bool = True
    embeddings: bool = False
    embedding_dim: int = 8

    # --- failure injection ------------------------------------------------
    fail_status: int | None = None
    fail_message: str = "upstream exploded"
    malformed_tool_args: bool = False
    tool_args_non_object: bool = False
    empty_content: bool = False
    empty_choices: bool = False

    # --- streaming quirks -------------------------------------------------
    stream_tool_calls_without_index: bool = False
    stream_omit_tool_ids: bool = False
    stream_tool_call_count: int = 1
    stream_usage_chunk: bool = False
    stream_empty: bool = False

    # --- scripted content -------------------------------------------------
    # Consumed in order for every non-tool, non-error reply; the last entry
    # repeats once the queue is exhausted.
    scripted_texts: list[str] = Field(default_factory=list)
    # Text returned for ReAct-style prompts (takes precedence over reply_text).
    react_reply: str | None = None
    # Text returned for plain chat replies when scripted_texts is empty.
    reply_text: str = "ok"

    # --- observations -----------------------------------------------------
    requests: list[dict[str, Any]] = Field(default_factory=list)
    embedding_requests: list[list[str]] = Field(default_factory=list)
    chat_calls: int = 0
    models_calls: int = 0

    def reset_calls(self) -> None:
        """Forget recorded requests (keeps configuration knobs intact)."""
        self.requests.clear()
        self.embedding_requests.clear()
        self.chat_calls = 0
        self.models_calls = 0

    def next_text(self) -> str:
        """Pop the next scripted reply, if any."""
        if self.scripted_texts:
            return (
                self.scripted_texts.pop(0)
                if len(self.scripted_texts) > 1
                else self.scripted_texts[0]
            )
        if self.react_reply is not None:
            return self.react_reply
        return self.reply_text


def _error_body(message: str, code: str) -> dict[str, Any]:
    return {"error": {"message": message, "type": "invalid_request_error", "code": code}}


def _error_response(status_code: int, message: str, code: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=_error_body(message, code))


def _auth_failure(behavior: MockBehavior, request: Request) -> JSONResponse | None:
    if not behavior.require_api_key:
        return None
    header = request.headers.get("authorization", "")
    received = header[len("Bearer ") :] if header.startswith("Bearer ") else ""
    if received != behavior.expected_api_key:
        # Deliberately echoes the received key: exercises error sanitisation.
        return _error_response(
            401,
            f"Incorrect API key provided: {received or '<missing>'}. "
            "You can find your API key at https://example.invalid/keys",
            "invalid_api_key",
        )
    return None


def _completion_payload(
    *,
    content: str | None,
    tool_calls: list[dict[str, Any]] | None,
    finish_reason: str,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": CHAT_MODEL,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    }


def _chunk(delta: dict[str, Any], finish_reason: str | None = None) -> str:
    payload = {
        "id": "chatcmpl-mock",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": CHAT_MODEL,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload)}\n\n"


def _sse(chunks: list[str]) -> StreamingResponse:
    async def _gen():
        for chunk in chunks:
            yield chunk
        yield "data: [DONE]\n\n"

    return StreamingResponse(_gen(), media_type="text/event-stream")


def build_mock_app(behavior: MockBehavior) -> FastAPI:
    """Create the mock provider app bound to ``behavior``."""
    app = FastAPI(title="mock-openai")

    @app.get("/v1/models")
    async def list_models(request: Request) -> Any:
        behavior.models_calls += 1
        if (failure := _auth_failure(behavior, request)) is not None:
            return failure
        if behavior.fail_status is not None:
            return _error_response(behavior.fail_status, behavior.fail_message, "server_error")
        if not behavior.models_endpoint:
            return _error_response(404, "Not Found", "not_found")
        return {
            "object": "list",
            "data": [
                {"id": name, "object": "model", "created": 0, "owned_by": "mock"}
                for name in behavior.models
            ],
        }

    @app.post("/v1/embeddings")
    async def create_embeddings(request: Request) -> Any:
        if (failure := _auth_failure(behavior, request)) is not None:
            return failure
        body = await request.json()
        inputs = body.get("input")
        if isinstance(inputs, str):
            inputs = [inputs]
        inputs = list(inputs or [])
        behavior.embedding_requests.append(inputs)
        if behavior.fail_status is not None:
            return _error_response(behavior.fail_status, behavior.fail_message, "server_error")
        if not behavior.embeddings:
            return _error_response(404, "Not Found", "not_found")
        data = []
        for index, _text in enumerate(inputs):
            vector = [
                round(0.001 * (index + 1) + 0.01 * dim, 6) for dim in range(behavior.embedding_dim)
            ]
            data.append({"object": "embedding", "index": index, "embedding": vector})
        return {
            "object": "list",
            "data": data,
            "model": body.get("model") or EMBED_MODEL,
            "usage": {"prompt_tokens": len(inputs), "total_tokens": len(inputs)},
        }

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Any:
        body = await request.json()
        behavior.requests.append(body)
        behavior.chat_calls += 1

        if (failure := _auth_failure(behavior, request)) is not None:
            return failure
        if behavior.fail_status is not None:
            return _error_response(behavior.fail_status, behavior.fail_message, "server_error")
        if body.get("response_format") is not None and not behavior.supports_json_mode:
            return _error_response(
                400,
                "response_format is not supported for this model",
                "invalid_request_error",
            )

        tools = body.get("tools") or []
        wants_tool_call = bool(tools) and behavior.supports_tools
        tool_name = (tools[0].get("function") or {}).get("name", "unknown_tool") if tools else ""
        if behavior.malformed_tool_args:
            arguments = "{this is not json"
        elif behavior.tool_args_non_object:
            arguments = "[1, 2]"
        else:
            arguments = json.dumps(DEFAULT_TOOL_ARGS)

        if body.get("stream"):
            if behavior.stream_empty:
                return _sse([])
            chunks: list[str] = []
            chunks.append(_chunk({"role": "assistant", "content": ""}))
            if wants_tool_call:
                call_count = max(1, min(behavior.stream_tool_call_count, len(tools)))
                names = [
                    (tools[i].get("function") or {}).get("name", tool_name)
                    for i in range(call_count)
                ]

                # Opening chunks (name + first half of the arguments), one per call.
                for idx, name in enumerate(names):
                    opening: dict[str, Any] = {
                        "type": "function",
                        "function": {"name": name, "arguments": arguments[:5]},
                    }
                    if not behavior.stream_tool_calls_without_index:
                        opening["index"] = idx
                    if not behavior.stream_omit_tool_ids:
                        opening["id"] = f"call_stream_{idx + 1}"
                    chunks.append(_chunk({"tool_calls": [opening]}))

                # Continuations arrive keyed only by index (or id): the client must
                # merge them back into the right call.
                for idx in range(call_count):
                    cont: dict[str, Any] = {"function": {"arguments": arguments[5:]}}
                    if not behavior.stream_tool_calls_without_index:
                        cont["index"] = idx
                    if not behavior.stream_omit_tool_ids:
                        cont["id"] = f"call_stream_{idx + 1}"
                    chunks.append(_chunk({"tool_calls": [cont]}))

                chunks.append(_chunk({}, finish_reason="tool_calls"))
            else:
                text = behavior.next_text()
                midpoint = max(1, len(text) // 2)
                chunks.append(_chunk({"content": text[:midpoint]}))
                chunks.append(_chunk({"content": text[midpoint:]}))
                chunks.append(_chunk({}, finish_reason="stop"))
            if behavior.stream_usage_chunk:
                chunks.append(
                    "data: "
                    + json.dumps(
                        {
                            "id": "chatcmpl-mock",
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": CHAT_MODEL,
                            "choices": [],
                            "usage": {
                                "prompt_tokens": 7,
                                "completion_tokens": 3,
                                "total_tokens": 10,
                            },
                        }
                    )
                    + "\n\n"
                )
            return _sse(chunks)

        if behavior.empty_choices:
            return {
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": CHAT_MODEL,
                "choices": [],
                "usage": None,
            }

        if wants_tool_call:
            return _completion_payload(
                content=None,
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": tool_name, "arguments": arguments},
                    }
                ],
                finish_reason="tool_calls",
            )

        if body.get("response_format") is not None:
            content = behavior.next_text() if behavior.scripted_texts else '{"ok": true}'
        else:
            content = None if behavior.empty_content else behavior.next_text()
        return _completion_payload(content=content, tool_calls=None, finish_reason="stop")

    return app
