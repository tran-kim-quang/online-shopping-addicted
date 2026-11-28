"""Structured JSON extraction and Pydantic-validated completion with retry."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from pydantic import BaseModel, ValidationError

from app.core.errors import LLMBadRequestError, LLMInvalidResponseError
from app.llm.client import LLMClient

logger = logging.getLogger(__name__)

_FENCE_PATTERN = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _find_balanced_object(text: str) -> str | None:
    """Return the first balanced ``{...}`` substring, respecting JSON strings & escapes."""
    start = -1
    depth = 0
    in_string = False
    escape = False

    for idx, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = idx
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    return text[start : idx + 1]

    return None


def extract_json_object(text: str) -> str | None:
    """Extract the first balanced JSON object ``{...}`` from free-form model output.

    Checks markdown code fences (````json ... ````) first, then falls back to scanning
    the full text while properly ignoring braces inside quoted/escaped strings.
    """
    if not text:
        return None

    for match in _FENCE_PATTERN.finditer(text):
        candidate = _find_balanced_object(match.group(1))
        if candidate is not None:
            return candidate

    return _find_balanced_object(text)


async def complete_json[T: BaseModel](
    client: LLMClient,
    messages: list[dict[str, Any]],
    schema: type[T],
    *,
    json_mode: bool,
    model: str | None = None,
    max_attempts: int = 3,
    include_schema: bool = True,
    reasoning_effort: str | None = "low",
) -> T:
    """Call ``client.chat`` and validate the response against ``schema`` with retries.

    ``include_schema=False`` leaves the JSON Schema out of the prompt (callers whose
    own prompt already shows the exact output shape save those tokens).
    ``reasoning_effort`` keeps reasoning models from spending the token budget on
    thinking for what is a plain extraction task.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1.")

    if include_schema:
        schema_str = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        system_instruction = (
            "Respond with ONLY a single valid JSON object conforming to this JSON Schema. "
            "Do not include explanations or markdown outside the JSON object.\n"
            f"Schema: {schema_str}"
        )
    else:
        system_instruction = (
            "Respond with ONLY a single valid JSON object in the format shown below. "
            "Do not include explanations or markdown outside the JSON object."
        )

    working_messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_instruction},
        *messages,
    ]
    response_format = {"type": "json_object"} if json_mode else None

    last_raw = ""
    last_error = "Unknown validation error."

    for attempt in range(1, max_attempts + 1):
        try:
            result = await client.chat(
                working_messages,
                model=model,
                response_format=response_format,
                reasoning_effort=reasoning_effort,
            )
        except LLMBadRequestError as exc:
            msg_lower = str(exc).casefold()
            if response_format is not None and (
                "json_validate_failed" in msg_lower or "failed to validate json" in msg_lower
            ):
                logger.info(
                    "Provider rejected json_object (%s); retrying without json_mode",
                    exc,
                )
                response_format = None
                last_error = f"Provider JSON validation error: {exc}"
                continue
            raise
        last_raw = result.content or ""
        extracted = extract_json_object(last_raw)

        if extracted is None:
            last_error = "No JSON object found in response."
        else:
            try:
                return schema.model_validate_json(extracted)
            except (ValidationError, ValueError) as exc:
                last_error = f"Validation error: {exc}"

        if attempt < max_attempts:
            working_messages.append({"role": "assistant", "content": last_raw})
            working_messages.append(
                {
                    "role": "user",
                    "content": (
                        f"Your previous response was invalid ({last_error}). "
                        "Please return ONLY a valid JSON object matching the schema."
                    ),
                }
            )

    raise LLMInvalidResponseError(
        f"Failed to produce valid {schema.__name__} after {max_attempts} attempts: {last_error}",
        raw=last_raw,
    )
