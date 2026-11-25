"""Unit tests for ``app.llm.tool_calling`` (native + ReAct-JSON strategies)."""

from __future__ import annotations

import pytest

from app.core.errors import LLMInvalidResponseError
from app.llm.tool_calling import (
    FinalStep,
    NativeToolCalling,
    ReactJsonToolCalling,
    ToolCallsStep,
    select_strategy,
    tool_result_message,
)
from app.llm.types import Capabilities, ToolCall, ToolSpec

TIME_TOOL = ToolSpec(
    name="get_current_time",
    description="Return the current UTC date and time.",
    parameters={"type": "object", "properties": {}, "required": []},
)
PRODUCT_TOOL = ToolSpec(
    name="get_product",
    description="Fetch a product by id.",
    parameters={
        "type": "object",
        "properties": {"product_id": {"type": "string"}},
        "required": ["product_id"],
    },
)
TOOLS = [TIME_TOOL, PRODUCT_TOOL]


def test_select_strategy_native() -> None:
    assert isinstance(select_strategy(Capabilities(tool_calling=True)), NativeToolCalling)


def test_select_strategy_react_fallback() -> None:
    strategy = select_strategy(Capabilities(tool_calling=False))

    assert isinstance(strategy, ReactJsonToolCalling)


# ---------------------------------------------------------------------------
# Native strategy
# ---------------------------------------------------------------------------


async def test_native_step_returns_tool_calls(llm_client, behavior) -> None:
    step = await NativeToolCalling().step(llm_client, [{"role": "user", "content": "time?"}], TOOLS)

    assert isinstance(step, ToolCallsStep)
    assert step.tool_calls[0].name == "get_current_time"
    message = step.assistant_message
    assert message["role"] == "assistant"
    assert message["tool_calls"][0]["id"] == "call_1"
    assert message["tool_calls"][0]["function"]["name"] == "get_current_time"
    assert message["tool_calls"][0]["function"]["arguments"] == '{"tz": "UTC"}'
    assert behavior.requests[0]["tool_choice"] == "auto"


async def test_native_step_returns_final(llm_client, behavior) -> None:
    behavior.reply_text = "The answer is 42."

    step = await NativeToolCalling().step(llm_client, [{"role": "user", "content": "q"}], [])

    assert isinstance(step, FinalStep)
    assert step.content == "The answer is 42."


# ---------------------------------------------------------------------------
# ReAct-JSON strategy
# ---------------------------------------------------------------------------


async def test_react_step_final(llm_client, behavior) -> None:
    behavior.react_reply = '{"final": "Đây là câu trả lời."}'

    step = await ReactJsonToolCalling().step(llm_client, [{"role": "user", "content": "q"}], TOOLS)

    assert isinstance(step, FinalStep)
    assert step.content == "Đây là câu trả lời."
    system_prompt = behavior.requests[0]["messages"][0]["content"]
    assert "get_product" in system_prompt
    assert '"tool"' in system_prompt


async def test_react_step_tool_call(llm_client, behavior) -> None:
    behavior.scripted_texts = ['{"tool": "get_product", "args": {"product_id": "42"}}']

    step = await ReactJsonToolCalling().step(llm_client, [{"role": "user", "content": "q"}], TOOLS)

    assert isinstance(step, ToolCallsStep)
    call = step.tool_calls[0]
    assert call.name == "get_product"
    assert call.arguments == {"product_id": "42"}
    assert call.id.startswith("react_")
    assert step.assistant_message == {
        "role": "assistant",
        "content": behavior.scripted_texts[0],
    }


async def test_react_step_retries_malformed_json(llm_client, behavior) -> None:
    behavior.scripted_texts = ["I will think about it.", '{"final": "ok"}']

    step = await ReactJsonToolCalling().step(llm_client, [{"role": "user", "content": "q"}], TOOLS)

    assert isinstance(step, FinalStep)
    assert behavior.chat_calls == 2
    retry_prompt = behavior.requests[1]["messages"][-1]["content"]
    assert "Invalid tool response" in retry_prompt


async def test_react_step_retries_brace_balanced_invalid_json(llm_client, behavior) -> None:
    """Balanced braces but not valid JSON (e.g. unquoted keys) must also retry."""
    behavior.scripted_texts = ["{tool: get_product, args: {}}", '{"final": "recovered"}']

    step = await ReactJsonToolCalling().step(llm_client, [{"role": "user", "content": "q"}], TOOLS)

    assert isinstance(step, FinalStep)
    assert step.content == "recovered"
    assert behavior.chat_calls == 2


async def test_react_step_retries_unknown_tool(llm_client, behavior) -> None:
    behavior.scripted_texts = [
        '{"tool": "delete_everything", "args": {}}',
        '{"tool": "get_current_time", "args": {}}',
    ]

    step = await ReactJsonToolCalling().step(llm_client, [{"role": "user", "content": "q"}], TOOLS)

    assert isinstance(step, ToolCallsStep)
    assert step.tool_calls[0].name == "get_current_time"
    retry_prompt = behavior.requests[1]["messages"][-1]["content"]
    assert "Unknown tool" in retry_prompt
    assert "delete_everything" in retry_prompt


async def test_react_step_retries_non_object_args(llm_client, behavior) -> None:
    behavior.scripted_texts = [
        '{"tool": "get_product", "args": "not-an-object"}',
        '{"final": "done"}',
    ]

    step = await ReactJsonToolCalling().step(llm_client, [{"role": "user", "content": "q"}], TOOLS)

    assert isinstance(step, FinalStep)
    assert behavior.chat_calls == 2


async def test_react_step_retries_non_object_json(llm_client, behavior) -> None:
    behavior.scripted_texts = ['{"tool": 42}', '{"final": "recovered"}']

    step = await ReactJsonToolCalling().step(llm_client, [{"role": "user", "content": "q"}], TOOLS)

    assert isinstance(step, FinalStep)
    assert step.content == "recovered"


async def test_react_step_exhausts_retries(llm_client, behavior) -> None:
    behavior.scripted_texts = ["definitely not json"]

    with pytest.raises(LLMInvalidResponseError) as excinfo:
        await ReactJsonToolCalling(max_retries=2).step(
            llm_client, [{"role": "user", "content": "q"}], TOOLS
        )

    assert behavior.chat_calls == 3
    assert "3 attempts" in str(excinfo.value)
    assert excinfo.value.raw == "definitely not json"


# ---------------------------------------------------------------------------
# Tool result formatting
# ---------------------------------------------------------------------------


def test_tool_result_message_native() -> None:
    call = ToolCall(
        id="call_9",
        name="get_product",
        arguments={"product_id": "42"},
        raw_arguments='{"product_id": "42"}',
    )

    message = tool_result_message(NativeToolCalling(), call, '{"title": "Áo"}')

    assert message == {
        "role": "tool",
        "tool_call_id": "call_9",
        "name": "get_product",
        "content": '{"title": "Áo"}',
    }


def test_tool_result_message_react() -> None:
    call = ToolCall(id="react_1", name="get_product", arguments={}, raw_arguments="{}")

    message = tool_result_message(ReactJsonToolCalling(), call, "result-body")

    assert message["role"] == "user"
    assert message["content"] == "Tool `get_product` result:\nresult-body"


def test_tool_result_message_react_unknown_strategy_object() -> None:
    """Anything that is not NativeToolCalling is treated as the ReAct format."""

    class _Custom:
        async def step(self, *args, **kwargs):  # pragma: no cover - protocol stub
            raise NotImplementedError

    call = ToolCall(id="x", name="t", arguments={}, raw_arguments="{}")
    message = tool_result_message(_Custom(), call, "r")  # type: ignore[arg-type]

    assert message["role"] == "user"
