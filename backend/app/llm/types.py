"""Pydantic data types for the LLM layer."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, SecretStr


class LLMConfig(BaseModel):
    """Connection and model configuration for a single ``LLMClient``."""

    base_url: str
    api_key: SecretStr | None = None
    chat_model: str
    small_model: str | None = None
    embedding_model: str | None = None
    timeout: float = 60.0
    max_retries: int = 2
    usage_kind: str | None = None
    daily_limit: float | None = None

    def __repr__(self) -> str:
        masked = "'***'" if self.api_key is not None else "None"
        return (
            f"LLMConfig(base_url={self.base_url!r}, api_key={masked}, "
            f"chat_model={self.chat_model!r}, small_model={self.small_model!r}, "
            f"embedding_model={self.embedding_model!r}, timeout={self.timeout!r}, "
            f"max_retries={self.max_retries!r})"
        )

    def __str__(self) -> str:
        return repr(self)


class ToolSpec(BaseModel):
    """Tool definition passed to an LLM."""

    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})

    def to_openai_tool(self) -> dict[str, Any]:
        """Convert to the OpenAI chat completions ``tools`` format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolCall(BaseModel):
    """Parsed tool invocation returned by an LLM."""

    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str


class ChatResult(BaseModel):
    """Non-streaming chat completion response."""

    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    model: str | None = None


class TextDelta(BaseModel):
    """Incremental text chunk from a streaming chat completion."""

    text: str


class ToolCallsReady(BaseModel):
    """Complete parsed tool calls accumulated from a streaming chat completion."""

    tool_calls: list[ToolCall]


class StreamDone(BaseModel):
    """Terminal event emitted when a streaming chat completion finishes."""

    finish_reason: str | None = None
    usage: dict[str, Any] | None = None


StreamEvent = TextDelta | ToolCallsReady | StreamDone


class Capabilities(BaseModel):
    """Probed feature matrix for an OpenAI-compatible provider/model pair."""

    models_endpoint: bool = False
    models: list[str] = Field(default_factory=list)
    chat: bool = False
    tool_calling: bool = False
    json_mode: bool = False
    streaming: bool = False
    embeddings: bool = False
    embedding_dim: int | None = None
    errors: dict[str, str] = Field(default_factory=dict)
    probed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
