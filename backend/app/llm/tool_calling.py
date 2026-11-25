"""Tool calling strategies: native OpenAI tool calls and ReAct-JSON fallback."""

from __future__ import annotations

import json
import uuid
from typing import Any, Protocol

from pydantic import BaseModel

from app.core.errors import LLMInvalidResponseError
from app.llm.client import LLMClient
from app.llm.structured import extract_json_object
from app.llm.types import Capabilities, ToolCall, ToolSpec


class ToolCallsStep(BaseModel):
    """Agent step requesting execution of one or more tools."""

    tool_calls: list[ToolCall]
    assistant_message: dict[str, Any]


class FinalStep(BaseModel):
    """Agent step returning a final answer to the user."""

    content: str


AgentStep = ToolCallsStep | FinalStep


class ToolCallingStrategy(Protocol):
    """Protocol implemented by native and fallback tool-calling strategies."""

    async def step(
        self,
        client: LLMClient,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
        *,
        model: str | None = None,
    ) -> AgentStep: ...


class NativeToolCalling:
    """Uses the provider's native ``tools`` / ``tool_calls`` chat completion API."""

    async def step(
        self,
        client: LLMClient,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
        *,
        model: str | None = None,
    ) -> AgentStep:
        result = await client.chat(
            messages,
            model=model,
            tools=tools if tools else None,
            tool_choice="auto" if tools else None,
        )
        if result.tool_calls:
            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": result.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": tc.raw_arguments,
                        },
                    }
                    for tc in result.tool_calls
                ],
            }
            return ToolCallsStep(
                tool_calls=result.tool_calls,
                assistant_message=assistant_message,
            )
        return FinalStep(content=result.content or "")


def _build_react_system_prompt(tools: list[ToolSpec]) -> str:
    tools_desc = [
        {
            "name": t.name,
            "description": t.description,
            "parameters": t.parameters,
        }
        for t in tools
    ]
    tools_json = json.dumps(tools_desc, ensure_ascii=False, indent=2)
    return (
        "You are an assistant that can call tools by responding with a single JSON object.\n"
        "Available tools:\n"
        f"{tools_json}\n\n"
        "Respond with ONLY one of these JSON formats (no extra prose):\n"
        '1) To call a tool: {"tool": "<tool_name>", "args": {<arguments>}}\n'
        '2) To give the final answer: {"final": "<your final response>"}\n'
    )


class ReactJsonToolCalling:
    """Fallback tool-calling strategy using ReAct JSON instructions in the prompt."""

    def __init__(self, *, max_retries: int = 2) -> None:
        self.max_retries = max_retries

    async def step(
        self,
        client: LLMClient,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
        *,
        model: str | None = None,
    ) -> AgentStep:
        valid_tools = {t.name for t in tools}
        working_messages: list[dict[str, Any]] = [
            {"role": "system", "content": _build_react_system_prompt(tools)},
            *messages,
        ]

        last_raw = ""
        last_error = "Unknown ReAct parsing error."

        for attempt in range(self.max_retries + 1):
            result = await client.chat(working_messages, model=model)
            last_raw = result.content or ""
            extracted = extract_json_object(last_raw)

            if extracted is None:
                last_error = "Response did not contain a JSON object."
            else:
                try:
                    data = json.loads(extracted)
                except json.JSONDecodeError as exc:
                    last_error = f"Malformed JSON: {exc.msg}"
                else:
                    if not isinstance(data, dict):
                        last_error = "JSON root must be an object."
                    elif "final" in data and isinstance(data["final"], str):
                        return FinalStep(content=data["final"])
                    elif "tool" in data and isinstance(data["tool"], str):
                        tool_name = data["tool"]
                        args = data.get("args", {})
                        if tool_name not in valid_tools:
                            last_error = (
                                f"Unknown tool '{tool_name}'. "
                                f"Available tools: {sorted(valid_tools)}"
                            )
                        elif not isinstance(args, dict):
                            last_error = "'args' must be a JSON object."
                        else:
                            call = ToolCall(
                                id=f"react_{uuid.uuid4().hex[:8]}",
                                name=tool_name,
                                arguments=args,
                                raw_arguments=json.dumps(args, ensure_ascii=False),
                            )
                            return ToolCallsStep(
                                tool_calls=[call],
                                assistant_message={"role": "assistant", "content": last_raw},
                            )
                    else:
                        last_error = (
                            'JSON must have either {"tool": "...", "args": {...}} '
                            'or {"final": "..."}.'
                        )

            if attempt < self.max_retries:
                working_messages.append({"role": "assistant", "content": last_raw})
                working_messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"Invalid tool response: {last_error} "
                            "Reply ONLY with valid JSON using the required format."
                        ),
                    }
                )

        raise LLMInvalidResponseError(
            f"ReAct-JSON step failed after {self.max_retries + 1} attempts: {last_error}",
            raw=last_raw,
        )


def select_strategy(caps: Capabilities) -> ToolCallingStrategy:
    """Choose ``NativeToolCalling`` when supported, otherwise ``ReactJsonToolCalling``."""
    if caps.tool_calling:
        return NativeToolCalling()
    return ReactJsonToolCalling()


def tool_result_message(
    strategy: ToolCallingStrategy,
    tool_call: ToolCall,
    result: str,
) -> dict[str, Any]:
    """Format a tool execution output message appropriate for ``strategy``."""
    if isinstance(strategy, NativeToolCalling):
        return {
            "role": "tool",
            "tool_call_id": tool_call.id,
            "name": tool_call.name,
            "content": result,
        }
    return {
        "role": "user",
        "content": f"Tool `{tool_call.name}` result:\n{result}",
    }
