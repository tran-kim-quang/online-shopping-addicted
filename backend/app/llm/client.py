"""OpenAI-compatible async LLM client (non-singleton, per-profile)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
from openai import AsyncOpenAI

from app.core.config import get_settings
from app.core.errors import (
    LLMBadRequestError,
    LLMError,
    LLMInvalidResponseError,
    LLMQuotaExceededError,
    map_openai_error,
)
from app.core.system_usage import SystemUsage
from app.llm.types import (
    ChatResult,
    LLMConfig,
    StreamDone,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolCallsReady,
    ToolSpec,
)
from app.net.pinned import make_safe_client
from app.security.url_validator import UnsafeURLError

_EMBED_BATCH_SIZE = 64


def _parse_tool_arguments(name: str, raw_arguments: str | None) -> tuple[dict[str, Any], str]:
    """Parse JSON tool call arguments; raise ``LLMInvalidResponseError`` on failure."""
    raw = raw_arguments if raw_arguments is not None else ""
    to_parse = raw if raw.strip() else "{}"
    try:
        parsed = json.loads(to_parse)
    except json.JSONDecodeError as exc:
        raise LLMInvalidResponseError(
            f"Invalid JSON in tool call arguments for '{name}': {exc.msg}",
            raw=raw,
        ) from exc

    if not isinstance(parsed, dict):
        raise LLMInvalidResponseError(
            f"Tool call arguments for '{name}' must be a JSON object.",
            raw=raw,
        )
    return parsed, raw


class LLMClient:
    """Thin provider-agnostic wrapper around ``openai.AsyncOpenAI``.

    Note: ``LLMClient`` is intentionally NOT a singleton because each user/profile
    may use a different ``base_url`` and ``api_key``. Managed by ``LLMClientRegistry``.
    """

    def __init__(
        self,
        config: LLMConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self._raw_key: str | None = (
            config.api_key.get_secret_value() if config.api_key is not None else None
        )
        if http_client is None:
            try:
                allow_private = get_settings().ALLOW_PRIVATE_LLM_HOSTS
            except Exception:
                allow_private = False
            self._http_client = make_safe_client(
                allow_private=allow_private,
                timeout=config.timeout,
            )
            self._owns_http_client = True
        else:
            self._http_client = http_client
            self._owns_http_client = False
        self._client = AsyncOpenAI(
            base_url=config.base_url,
            api_key=self._raw_key or "not-needed",
            timeout=config.timeout,
            max_retries=config.max_retries,
            http_client=self._http_client,
        )

    def _build_chat_kwargs(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None,
        tools: list[ToolSpec] | None,
        tool_choice: Any,
        response_format: dict[str, Any] | None,
        temperature: float | None,
        max_tokens: int | None,
        stream: bool,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": model or self.config.chat_model,
            "messages": messages,
            "stream": stream,
        }
        if tools:
            kwargs["tools"] = [t.to_openai_tool() for t in tools]
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
        if response_format is not None:
            kwargs["response_format"] = response_format
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        return kwargs

    async def list_models(self) -> list[str]:
        """Fetch available model IDs from ``GET /models``."""
        try:
            page = await self._client.models.list()
            return [m.id for m in (page.data or []) if getattr(m, "id", None)]
        except (LLMError, UnsafeURLError):
            raise
        except Exception as exc:
            raise map_openai_error(exc, api_key=self._raw_key) from exc

    async def _consume_system_quota_if_needed(self) -> None:
        if not self.config.usage_kind:
            return
        allowed = await SystemUsage().try_consume(
            self.config.usage_kind,
            1.0,
            self.config.daily_limit,
        )
        if not allowed:
            raise LLMQuotaExceededError(
                "Hạn mức LLM dùng chung hôm nay đã hết.",
                kind=self.config.usage_kind,
            )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        tools: list[ToolSpec] | None = None,
        tool_choice: Any = None,
        response_format: dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        """Execute a non-streaming chat completion request."""
        await self._consume_system_quota_if_needed()
        kwargs = self._build_chat_kwargs(
            messages,
            model=model,
            tools=tools,
            tool_choice=tool_choice,
            response_format=response_format,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=False,
        )
        try:
            response = await self._client.chat.completions.create(**kwargs)
        except (LLMError, UnsafeURLError):
            raise
        except Exception as exc:
            raise map_openai_error(exc, api_key=self._raw_key) from exc

        if not response.choices:
            raise LLMInvalidResponseError("Provider returned no choices in chat completion.")

        choice = response.choices[0]
        message = choice.message
        parsed_tool_calls: list[ToolCall] = []

        if message.tool_calls:
            for idx, tc in enumerate(message.tool_calls):
                fn = tc.function
                name = fn.name or ""
                arguments, raw_args = _parse_tool_arguments(name, fn.arguments)
                parsed_tool_calls.append(
                    ToolCall(
                        id=tc.id or f"call_{idx}",
                        name=name,
                        arguments=arguments,
                        raw_arguments=raw_args,
                    )
                )

        usage = response.usage.model_dump() if response.usage is not None else None
        return ChatResult(
            content=message.content,
            tool_calls=parsed_tool_calls,
            finish_reason=choice.finish_reason,
            usage=usage,
            model=response.model,
        )

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        tools: list[ToolSpec] | None = None,
        tool_choice: Any = None,
        response_format: dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Execute a streaming chat completion request, yielding normalized events."""
        await self._consume_system_quota_if_needed()
        kwargs = self._build_chat_kwargs(
            messages,
            model=model,
            tools=tools,
            tool_choice=tool_choice,
            response_format=response_format,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )

        slots: list[dict[str, str]] = []
        index_to_slot: dict[int, int] = {}
        id_to_slot: dict[str, int] = {}
        finish_reason: str | None = None
        usage: dict[str, Any] | None = None

        try:
            stream = await self._client.chat.completions.create(**kwargs)
            async for chunk in stream:
                if getattr(chunk, "usage", None) is not None:
                    usage = chunk.usage.model_dump()

                if not chunk.choices:
                    continue

                choice = chunk.choices[0]
                if choice.finish_reason is not None:
                    finish_reason = choice.finish_reason

                delta = choice.delta
                if delta is None:
                    continue

                if delta.content:
                    yield TextDelta(text=delta.content)

                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        tc_index = getattr(tc_delta, "index", None)
                        tc_id = getattr(tc_delta, "id", None)
                        fn_delta = getattr(tc_delta, "function", None)
                        fn_name = getattr(fn_delta, "name", None) if fn_delta else None
                        fn_args = getattr(fn_delta, "arguments", None) if fn_delta else None

                        if tc_index is not None:
                            if tc_index in index_to_slot:
                                slot_idx = index_to_slot[tc_index]
                            else:
                                slot_idx = len(slots)
                                slots.append({"id": "", "name": "", "raw_arguments": ""})
                                index_to_slot[tc_index] = slot_idx
                        elif tc_id:
                            if tc_id in id_to_slot:
                                slot_idx = id_to_slot[tc_id]
                            else:
                                slot_idx = len(slots)
                                slots.append({"id": "", "name": "", "raw_arguments": ""})
                                id_to_slot[tc_id] = slot_idx
                        else:
                            # Neither index nor id supplied: attach to active slot unless
                            # a brand new tool call name arrives while previous already has one.
                            if slots and not (fn_name and slots[-1]["name"]):
                                slot_idx = len(slots) - 1
                            else:
                                slot_idx = len(slots)
                                slots.append({"id": "", "name": "", "raw_arguments": ""})

                        slot = slots[slot_idx]
                        if tc_id:
                            slot["id"] = tc_id
                            id_to_slot[tc_id] = slot_idx
                        if fn_name:
                            if not slot["name"]:
                                slot["name"] = fn_name
                            elif fn_name != slot["name"]:
                                slot["name"] += fn_name
                        if fn_args:
                            slot["raw_arguments"] += fn_args
        except (LLMError, UnsafeURLError):
            raise
        except Exception as exc:
            raise map_openai_error(exc, api_key=self._raw_key) from exc

        if slots:
            parsed_calls: list[ToolCall] = []
            for idx, slot in enumerate(slots):
                arguments, raw_args = _parse_tool_arguments(slot["name"], slot["raw_arguments"])
                parsed_calls.append(
                    ToolCall(
                        id=slot["id"] or f"call_{idx}",
                        name=slot["name"],
                        arguments=arguments,
                        raw_arguments=raw_args,
                    )
                )
            yield ToolCallsReady(tool_calls=parsed_calls)

        yield StreamDone(finish_reason=finish_reason, usage=usage)

    async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        """Generate embeddings for ``texts``, batching at most 64 items per request."""
        if not texts:
            return []

        embed_model = model or self.config.embedding_model
        if not embed_model:
            raise LLMBadRequestError("embedding_model must be configured to call embed().")

        results: list[list[float]] = []
        try:
            for offset in range(0, len(texts), _EMBED_BATCH_SIZE):
                batch = texts[offset : offset + _EMBED_BATCH_SIZE]
                response = await self._client.embeddings.create(
                    model=embed_model,
                    input=batch,
                )
                ordered = sorted(response.data, key=lambda item: item.index)
                for item in ordered:
                    results.append(list(item.embedding))
        except (LLMError, UnsafeURLError):
            raise
        except Exception as exc:
            raise map_openai_error(exc, api_key=self._raw_key) from exc

        return results

    async def aclose(self) -> None:
        """Close the underlying HTTP client if owned by this ``LLMClient``."""
        if self._owns_http_client:
            await self._client.close()
