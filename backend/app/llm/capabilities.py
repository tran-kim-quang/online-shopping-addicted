"""Provider capability probing for OpenAI-compatible LLM endpoints."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from app.core.config import get_settings
from app.core.errors import LLMAuthError, LLMNotFoundError, sanitize_error_message
from app.llm.client import LLMClient
from app.llm.types import Capabilities, LLMConfig, StreamDone, TextDelta, ToolSpec

_TIME_TOOL = ToolSpec(
    name="get_current_time",
    description="Return the current UTC date and time.",
    parameters={"type": "object", "properties": {}, "required": []},
)


def _default_probe_timeout() -> float:
    try:
        return get_settings().LLM_PROBE_TIMEOUT_SECONDS
    except Exception:
        return 20.0


async def probe(
    client: LLMClient,
    config: LLMConfig,
    *,
    timeout: float | None = None,
) -> Capabilities:
    """Probe an OpenAI-compatible provider for supported features.

    Steps:
    1. ``GET /models`` -> ``models_endpoint``, ``models`` (404 does not abort).
    2. Short chat -> ``chat`` (stops early on auth or model-not-found errors).
    3. Tool calling with ``get_current_time`` -> ``tool_calling``.
    4. JSON object response_format -> ``json_mode``.
    5. Streaming chat -> ``streaming``.
    6. Embeddings (only if ``config.embedding_model`` is set) -> ``embeddings``, ``embedding_dim``.
    """
    step_timeout = timeout if timeout is not None else _default_probe_timeout()
    raw_key = config.api_key.get_secret_value() if config.api_key is not None else None
    secrets = (raw_key,) if raw_key else ()

    def _clean(exc: Exception) -> str:
        return sanitize_error_message(str(exc) or exc.__class__.__name__, secrets)

    caps = Capabilities(probed_at=datetime.now(UTC))

    # 1. Models listing
    try:
        async with asyncio.timeout(step_timeout):
            models = await client.list_models()
        caps.models_endpoint = True
        caps.models = models
    except Exception as exc:
        caps.models_endpoint = False
        caps.errors["models"] = _clean(exc)

    # 2. Basic chat connectivity + credentials + model check
    try:
        async with asyncio.timeout(step_timeout):
            chat_res = await client.chat(
                [{"role": "user", "content": "Reply with: ok"}],
                model=config.chat_model,
                max_tokens=16,
                temperature=0,
            )
        caps.chat = chat_res.content is not None or bool(chat_res.tool_calls)
        if not caps.chat:
            caps.errors["chat"] = "Empty response from chat completion."
    except (LLMAuthError, LLMNotFoundError) as exc:
        caps.chat = False
        caps.errors["chat"] = _clean(exc)
        return caps
    except Exception as exc:
        caps.chat = False
        caps.errors["chat"] = _clean(exc)

    # 3. Native tool calling
    try:
        async with asyncio.timeout(step_timeout):
            tool_res = await client.chat(
                [
                    {
                        "role": "user",
                        "content": (
                            "What is the current UTC time? "
                            "You must call the get_current_time tool to answer."
                        ),
                    }
                ],
                model=config.chat_model,
                tools=[_TIME_TOOL],
                tool_choice="auto",
                max_tokens=128,
                temperature=0,
            )
        caps.tool_calling = any(tc.name == _TIME_TOOL.name for tc in tool_res.tool_calls)
        if not caps.tool_calling:
            caps.errors["tool_calling"] = "Provider did not emit the requested tool call."
    except Exception as exc:
        caps.tool_calling = False
        caps.errors["tool_calling"] = _clean(exc)

    # 4. JSON mode
    try:
        async with asyncio.timeout(step_timeout):
            json_res = await client.chat(
                [
                    {
                        "role": "user",
                        "content": 'Return a JSON object with a single key "ok" set to true.',
                    }
                ],
                model=config.chat_model,
                response_format={"type": "json_object"},
                max_tokens=64,
                temperature=0,
            )
        content = (json_res.content or "").strip()
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            caps.json_mode = True
        else:
            caps.json_mode = False
            caps.errors["json_mode"] = "Response JSON was not a JSON object."
    except Exception as exc:
        caps.json_mode = False
        caps.errors["json_mode"] = _clean(exc)

    # 5. Streaming
    try:
        saw_event = False
        async with asyncio.timeout(step_timeout):
            async for event in client.chat_stream(
                [{"role": "user", "content": "Reply with: ok"}],
                model=config.chat_model,
                max_tokens=16,
                temperature=0,
            ):
                if isinstance(event, (TextDelta, StreamDone)):
                    saw_event = True
        caps.streaming = saw_event
        if not saw_event:
            caps.errors["streaming"] = "Stream ended without events."
    except Exception as exc:
        caps.streaming = False
        caps.errors["streaming"] = _clean(exc)

    # 6. Embeddings (optional, only if embedding_model configured)
    if config.embedding_model:
        try:
            async with asyncio.timeout(step_timeout):
                vectors = await client.embed(["xin chào"], model=config.embedding_model)
            if vectors and len(vectors[0]) > 0:
                caps.embeddings = True
                caps.embedding_dim = len(vectors[0])
            else:
                caps.embeddings = False
                caps.errors["embeddings"] = "Empty embedding vector returned."
        except Exception as exc:
            caps.embeddings = False
            caps.embedding_dim = None
            caps.errors["embeddings"] = _clean(exc)

    return caps
