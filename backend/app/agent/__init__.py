"""QA Agent package: citation tracking, 6 scoped tools, prompts, and tool loop."""

from __future__ import annotations

from app.agent.citations import CitationEntry, CitationProcessResult, CitationRegistry
from app.agent.loop import (
    AgentEvent,
    DiscardEvent,
    ErrorEvent,
    FinalEvent,
    QAAgent,
    TokenEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from app.agent.prompts import build_system_prompt
from app.agent.tools import (
    AgentContext,
    AgentTool,
    ToolResult,
    build_agent_tools,
    format_tool_result,
)

__all__ = [
    "AgentContext",
    "AgentEvent",
    "AgentTool",
    "CitationEntry",
    "CitationProcessResult",
    "CitationRegistry",
    "DiscardEvent",
    "ErrorEvent",
    "FinalEvent",
    "QAAgent",
    "TokenEvent",
    "ToolCallEvent",
    "ToolResult",
    "ToolResultEvent",
    "build_agent_tools",
    "build_system_prompt",
    "format_tool_result",
]
