"""Custom QA Agent tool loop (no LangChain): native streaming + ReAct-JSON fallback."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.agent.citations import process_citations
from app.agent.prompts import build_system_prompt
from app.agent.tools import (
    AgentContext,
    AgentTool,
    ToolResult,
    build_agent_tools,
    format_tool_result,
)
from app.core.errors import (
    LLMAuthError,
    LLMBadRequestError,
    LLMError,
    LLMInvalidResponseError,
    LLMNotFoundError,
    LLMQuotaExceededError,
    LLMRateLimitError,
    LLMUnavailableError,
    sanitize_error_message,
)
from app.llm.structured import extract_json_object
from app.llm.tool_calling import (
    FinalStep,
    NativeToolCalling,
    ReactJsonToolCalling,
    ToolCallsStep,
    select_strategy,
    tool_result_message,
)
from app.llm.types import (
    Capabilities,
    StreamDone,
    TextDelta,
    ToolCall,
    ToolCallsReady,
    ToolSpec,
)
from app.security.url_validator import UnsafeURLError

logger = logging.getLogger(__name__)

_FINAL_FALLBACK_INSTRUCTION = (
    "Bạn đã đạt giới hạn số bước gọi công cụ. Hãy trả lời câu hỏi của người dùng ngay bây giờ "
    "chỉ dựa trên dữ liệu đã thu thập được, kèm mã trích dẫn [P#]/[R#]/[D#] hợp lệ. "
    "Nếu còn thiếu thông tin nào, hãy nêu rõ phần còn thiếu."
)


# ---------------------------------------------------------------------------
# Agent streaming events
# ---------------------------------------------------------------------------


class ToolCallEvent(BaseModel):
    """Emitted right before a tool is invoked."""

    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class ToolResultEvent(BaseModel):
    """Emitted immediately after a tool invocation completes."""

    name: str
    ok: bool
    count: int = 0
    ms: int = 0
    error: str | None = None


class TokenEvent(BaseModel):
    """Incremental answer text chunk streamed to the caller."""

    text: str


class DiscardEvent(BaseModel):
    """Signals that previously streamed draft tokens belong to a tool-calling step."""


class FinalEvent(BaseModel):
    """Terminal event carrying the post-processed answer, validated citations, and usage."""

    content: str
    citations: list[dict[str, Any]] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    data_as_of: datetime | None = None
    steps: int = 0
    invalid_citations: int = 0
    tool_calls_trace: list[dict[str, Any]] = Field(default_factory=list)
    tool_outputs: list[str] = Field(default_factory=list)


class ErrorEvent(BaseModel):
    """Terminal event emitted when the LLM or agent encounters an unrecoverable error."""

    code: str
    message: str


AgentEvent = ToolCallEvent | ToolResultEvent | TokenEvent | DiscardEvent | FinalEvent | ErrorEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_capabilities(ctx: AgentContext, override: Capabilities | None) -> Capabilities:
    if override is not None:
        return override
    if ctx.profile is not None and isinstance(ctx.profile.capabilities, dict):
        try:
            return Capabilities.model_validate(ctx.profile.capabilities)
        except Exception:
            logger.warning(
                "Invalid capabilities JSON on profile %s; falling back to conservative defaults.",
                ctx.profile.id,
            )
    return Capabilities(chat=True, tool_calling=False, streaming=False)


def _merge_usage(acc: dict[str, int], incoming: dict[str, Any] | None) -> None:
    if not isinstance(incoming, dict):
        return
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        val = incoming.get(key)
        if isinstance(val, int):
            acc[key] = acc.get(key, 0) + val


def _error_code_for(exc: Exception) -> str:
    if isinstance(exc, LLMQuotaExceededError):
        return "system_quota_exceeded"
    if isinstance(exc, LLMAuthError):
        return "llm_auth"
    if isinstance(exc, LLMNotFoundError):
        return "llm_not_found"
    if isinstance(exc, LLMRateLimitError):
        return "llm_rate_limit"
    if isinstance(exc, LLMUnavailableError):
        return "llm_unavailable"
    if isinstance(exc, LLMBadRequestError):
        return "llm_bad_request"
    if isinstance(exc, LLMInvalidResponseError):
        return "llm_invalid_response"
    if isinstance(exc, LLMError):
        return "llm_error"
    if isinstance(exc, UnsafeURLError):
        return "unsafe_url"
    return "agent_error"


def _sanitize_for_client(ctx: AgentContext, message: str) -> str:
    secrets: list[str] = []
    if ctx.llm_client.config.api_key is not None:
        raw = ctx.llm_client.config.api_key.get_secret_value()
        if raw:
            secrets.append(raw)
    return sanitize_error_message(message, secrets)


def _unwrap_react_final(raw_text: str) -> str:
    """If a ReAct-trained model wraps a fallback answer in ``{"final": "..."}``, unwrap it."""
    extracted = extract_json_object(raw_text)
    if extracted is None:
        return raw_text
    try:
        parsed = json.loads(extracted)
    except json.JSONDecodeError:
        return raw_text
    if isinstance(parsed, dict) and isinstance(parsed.get("final"), str):
        return parsed["final"]
    return raw_text


def _prepare_history_messages(
    history: Sequence[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, str]]:
    """Keep the last ``limit`` user/assistant text messages (stripping all tool traces)."""
    if limit <= 0 or not history:
        return []
    cleaned: list[dict[str, str]] = []
    for item in history:
        role = item.get("role")
        content = item.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            cleaned.append({"role": role, "content": content})
    return cleaned[-limit:]


# ---------------------------------------------------------------------------
# QAAgent (intentionally NOT a singleton — 1 instance per turn)
# ---------------------------------------------------------------------------


class QAAgent:
    """Per-turn Agentic RAG runner orchestrating tools, LLM strategy, streaming, and citations.

    Note: ``QAAgent`` is intentionally NOT a singleton; instantiate a fresh agent per turn.
    """

    def __init__(
        self,
        *,
        tools: dict[str, AgentTool] | None = None,
        capabilities: Capabilities | None = None,
    ) -> None:
        self._tools: dict[str, AgentTool] = tools if tools is not None else build_agent_tools()
        self._capabilities_override = capabilities

    @property
    def tool_specs(self) -> list[ToolSpec]:
        return [tool.to_spec() for tool in self._tools.values()]

    async def _execute_tool(
        self,
        ctx: AgentContext,
        tool_call: ToolCall,
    ) -> tuple[ToolResult, str, int]:
        started = time.monotonic()
        tool = self._tools.get(tool_call.name)
        if tool is None:
            result = ToolResult(
                ok=False,
                error=(
                    f"Unknown tool '{tool_call.name}'. "
                    f"Available tools: {sorted(self._tools.keys())}"
                ),
            )
        else:
            result = await tool.run(ctx, tool_call.arguments)
        elapsed_ms = int(round((time.monotonic() - started) * 1000))
        formatted = format_tool_result(
            result,
            max_chars=ctx.settings.AGENT_TOOL_RESULT_MAX_CHARS,
        )
        return result, formatted, elapsed_ms

    def _build_final_event(
        self,
        ctx: AgentContext,
        raw_content: str,
        *,
        usage_acc: dict[str, int],
        steps: int,
        tool_calls_trace: list[dict[str, Any]],
        tool_outputs: list[str],
    ) -> FinalEvent:
        processed = process_citations(raw_content, ctx.citations, ctx.products)
        usage_payload: dict[str, Any] = dict(usage_acc)
        if processed.invalid_citations > 0:
            usage_payload["invalid_citations"] = processed.invalid_citations
        return FinalEvent(
            content=processed.content,
            citations=processed.citations,
            usage=usage_payload,
            data_as_of=processed.data_as_of,
            steps=steps,
            invalid_citations=processed.invalid_citations,
            tool_calls_trace=tool_calls_trace,
            tool_outputs=tool_outputs,
        )

    async def run(
        self,
        ctx: AgentContext,
        history: Sequence[dict[str, Any]],
        user_message: str,
    ) -> AsyncIterator[AgentEvent]:
        """Run the agent tool loop and yield streaming :data:`AgentEvent` items."""
        system_prompt = build_system_prompt(ctx)
        recent_history = _prepare_history_messages(
            history,
            limit=ctx.settings.AGENT_HISTORY_MESSAGES,
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            *recent_history,
            {"role": "user", "content": user_message},
        ]

        caps = _parse_capabilities(ctx, self._capabilities_override)
        strategy = select_strategy(caps)
        use_streaming = isinstance(strategy, NativeToolCalling) and caps.streaming
        chat_model = (
            ctx.profile.chat_model if ctx.profile is not None else ctx.llm_client.config.chat_model
        )
        specs = self.tool_specs
        max_steps = max(1, ctx.settings.AGENT_MAX_STEPS)

        usage_acc: dict[str, int] = {}
        tool_calls_trace: list[dict[str, Any]] = []
        tool_outputs: list[str] = []
        steps_completed = 0

        try:
            for step_idx in range(1, max_steps + 1):
                steps_completed = step_idx
                if use_streaming:
                    step_text_parts: list[str] = []
                    step_tool_calls: list[ToolCall] = []

                    async for stream_ev in ctx.llm_client.chat_stream(
                        messages,
                        model=chat_model,
                        tools=specs,
                        tool_choice="auto",
                    ):
                        if isinstance(stream_ev, TextDelta):
                            step_text_parts.append(stream_ev.text)
                            yield TokenEvent(text=stream_ev.text)
                        elif isinstance(stream_ev, ToolCallsReady):
                            step_tool_calls = stream_ev.tool_calls
                        elif isinstance(stream_ev, StreamDone):
                            _merge_usage(usage_acc, stream_ev.usage)

                    if not step_tool_calls:
                        raw_final = "".join(step_text_parts)
                        yield self._build_final_event(
                            ctx,
                            raw_final,
                            usage_acc=usage_acc,
                            steps=steps_completed,
                            tool_calls_trace=tool_calls_trace,
                            tool_outputs=tool_outputs,
                        )
                        return

                    # Draft tokens were streamed before tool calls arrived: tell client to discard.
                    if step_text_parts:
                        yield DiscardEvent()

                    assistant_content = "".join(step_text_parts) or None
                    messages.append(
                        {
                            "role": "assistant",
                            "content": assistant_content,
                            "tool_calls": [
                                {
                                    "id": tc.id,
                                    "type": "function",
                                    "function": {
                                        "name": tc.name,
                                        "arguments": tc.raw_arguments,
                                    },
                                }
                                for tc in step_tool_calls
                            ],
                        }
                    )
                    tool_calls = step_tool_calls
                else:
                    step_result = await strategy.step(
                        ctx.llm_client,
                        messages,
                        specs,
                        model=chat_model,
                    )
                    _merge_usage(usage_acc, step_result.usage)

                    if isinstance(step_result, FinalStep):
                        final_event = self._build_final_event(
                            ctx,
                            step_result.content,
                            usage_acc=usage_acc,
                            steps=steps_completed,
                            tool_calls_trace=tool_calls_trace,
                            tool_outputs=tool_outputs,
                        )
                        if final_event.content:
                            yield TokenEvent(text=final_event.content)
                        yield final_event
                        return

                    assert isinstance(step_result, ToolCallsStep)
                    messages.append(step_result.assistant_message)
                    tool_calls = step_result.tool_calls

                # Execute all tool calls requested in this step sequentially.
                for tc in tool_calls:
                    yield ToolCallEvent(name=tc.name, args=tc.arguments)
                    result, formatted, elapsed_ms = await self._execute_tool(ctx, tc)
                    tool_outputs.append(formatted)
                    tool_calls_trace.append(
                        {
                            "name": tc.name,
                            "args": tc.arguments,
                            "ok": result.ok,
                            "ms": elapsed_ms,
                            "result_count": result.result_count,
                        }
                    )
                    yield ToolResultEvent(
                        name=tc.name,
                        ok=result.ok,
                        count=result.result_count,
                        ms=elapsed_ms,
                        error=result.error,
                    )
                    messages.append(tool_result_message(strategy, tc, formatted))

            # Exhausted AGENT_MAX_STEPS: one final LLM turn WITHOUT tools.
            steps_completed = max_steps + 1
            messages.append({"role": "user", "content": _FINAL_FALLBACK_INSTRUCTION})

            if use_streaming:
                final_parts: list[str] = []
                async for stream_ev in ctx.llm_client.chat_stream(
                    messages,
                    model=chat_model,
                    tools=None,
                    tool_choice=None,
                ):
                    if isinstance(stream_ev, TextDelta):
                        final_parts.append(stream_ev.text)
                        yield TokenEvent(text=stream_ev.text)
                    elif isinstance(stream_ev, StreamDone):
                        _merge_usage(usage_acc, stream_ev.usage)
                raw_final = "".join(final_parts)
                yield self._build_final_event(
                    ctx,
                    raw_final,
                    usage_acc=usage_acc,
                    steps=steps_completed,
                    tool_calls_trace=tool_calls_trace,
                    tool_outputs=tool_outputs,
                )
            else:
                chat_res = await ctx.llm_client.chat(
                    messages,
                    model=chat_model,
                    tools=None,
                    tool_choice=None,
                )
                _merge_usage(usage_acc, chat_res.usage)
                raw_final = chat_res.content or ""
                if isinstance(strategy, ReactJsonToolCalling):
                    raw_final = _unwrap_react_final(raw_final)
                final_event = self._build_final_event(
                    ctx,
                    raw_final,
                    usage_acc=usage_acc,
                    steps=steps_completed,
                    tool_calls_trace=tool_calls_trace,
                    tool_outputs=tool_outputs,
                )
                if final_event.content:
                    yield TokenEvent(text=final_event.content)
                yield final_event

        except (LLMError, UnsafeURLError) as exc:
            code = _error_code_for(exc)
            clean_msg = _sanitize_for_client(ctx, str(exc))
            logger.warning("QAAgent failed with %s: %s", code, clean_msg)
            yield ErrorEvent(code=code, message=clean_msg)
        except Exception as exc:
            clean_msg = _sanitize_for_client(ctx, f"Unexpected agent error: {exc}")
            logger.exception("Unhandled error in QAAgent.run: %s", clean_msg)
            yield ErrorEvent(code="agent_error", message=clean_msg)
