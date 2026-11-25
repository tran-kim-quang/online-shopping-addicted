"""Unit tests for the error hierarchy, sanitisation, and SDK error mapping."""

from __future__ import annotations

import httpx
import openai
import pytest

from app.core.errors import (
    LLMAuthError,
    LLMBadRequestError,
    LLMError,
    LLMInvalidResponseError,
    LLMNotFoundError,
    LLMRateLimitError,
    LLMUnavailableError,
    map_openai_error,
    sanitize_error_message,
)


def _response(status: int) -> httpx.Response:
    return httpx.Response(
        status, request=httpx.Request("POST", "https://provider.invalid/v1/chat/completions")
    )


# ---------------------------------------------------------------------------
# sanitize_error_message
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "Incorrect API key provided: sk-proj-abcdefghijklmnop",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
        "token gsk_abcdefghijklmnop leaked",  # Groq-style underscore key
        "token or-abcdefghijklmnop leaked",
        "key-abcdefghijklmnop",
        "sk_live_abcdefghijklmnop",
        "sk-proj-abcdefghijklmnop",
    ],
)
def test_sanitize_redacts_key_shaped_tokens(message: str) -> None:
    cleaned = sanitize_error_message(message)

    assert "[REDACTED]" in cleaned
    assert "abcdefghijklmnop" not in cleaned


@pytest.mark.parametrize(
    "message",
    ["risk-free-download", "task-related work", "disk-usage report"],
)
def test_sanitize_does_not_mangle_normal_hyphenated_words(message: str) -> None:
    assert sanitize_error_message(message) == message


def test_sanitize_redacts_explicit_secret() -> None:
    cleaned = sanitize_error_message("bad credentials: hunter2hunter2", ("hunter2hunter2",))

    assert cleaned == "bad credentials: [REDACTED]"


def test_sanitize_ignores_too_short_secret() -> None:
    assert sanitize_error_message("value abc", ("abc",)) == "value abc"


def test_sanitize_leaves_normal_text_alone() -> None:
    assert sanitize_error_message("model gpt-4o-mini not found") == "model gpt-4o-mini not found"


# ---------------------------------------------------------------------------
# map_openai_error
# ---------------------------------------------------------------------------


def test_maps_authentication_error() -> None:
    exc = openai.AuthenticationError("bad key", response=_response(401), body=None)

    mapped = map_openai_error(exc)

    assert isinstance(mapped, LLMAuthError)
    assert mapped.status_code == 401


def test_maps_not_found_error() -> None:
    exc = openai.NotFoundError("no such model", response=_response(404), body=None)

    assert isinstance(map_openai_error(exc), LLMNotFoundError)


def test_maps_rate_limit_error() -> None:
    exc = openai.RateLimitError("slow down", response=_response(429), body=None)

    assert isinstance(map_openai_error(exc), LLMRateLimitError)


def test_maps_bad_request_error() -> None:
    exc = openai.BadRequestError("bad payload", response=_response(400), body=None)

    assert isinstance(map_openai_error(exc), LLMBadRequestError)


def test_maps_unprocessable_entity() -> None:
    exc = openai.UnprocessableEntityError("nope", response=_response(422), body=None)

    assert isinstance(map_openai_error(exc), LLMBadRequestError)


def test_maps_internal_server_error() -> None:
    exc = openai.InternalServerError("boom", response=_response(500), body=None)

    assert isinstance(map_openai_error(exc), LLMUnavailableError)


def test_maps_transport_errors() -> None:
    assert isinstance(map_openai_error(httpx.ConnectError("no route")), LLMUnavailableError)
    assert isinstance(map_openai_error(httpx.ReadTimeout("slow")), LLMUnavailableError)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (403, LLMAuthError),
        (404, LLMNotFoundError),
        (429, LLMRateLimitError),
        (400, LLMBadRequestError),
        (422, LLMBadRequestError),
        (503, LLMUnavailableError),
        (418, LLMError),
    ],
)
def test_maps_generic_api_status_error(status: int, expected: type[LLMError]) -> None:
    exc = openai.APIStatusError("provider said no", response=_response(status), body=None)

    mapped = map_openai_error(exc)

    assert type(mapped) is expected or isinstance(mapped, expected)
    assert mapped.status_code == status


def test_maps_unknown_exception_to_base_error() -> None:
    mapped = map_openai_error(ValueError("something odd"))

    assert type(mapped) is LLMError
    assert mapped.status_code is None


def test_mapping_redacts_api_key_from_message() -> None:
    raw_key = "sk-live-super-secret-0123456789"
    exc = openai.AuthenticationError(
        f"Incorrect API key provided: {raw_key}", response=_response(401), body=None
    )

    mapped = map_openai_error(exc, api_key=raw_key)

    assert raw_key not in mapped.message
    assert "[REDACTED]" in mapped.message


def test_mapping_resanitises_existing_llm_error() -> None:
    raw_key = "sk-live-super-secret-0123456789"
    original = LLMInvalidResponseError(f"failed with key {raw_key}", raw=f'{{"key": "{raw_key}"}}')

    mapped = map_openai_error(original, api_key=raw_key)

    assert isinstance(mapped, LLMInvalidResponseError)
    assert raw_key not in mapped.message
    assert mapped.raw is not None and raw_key not in mapped.raw


def test_mapping_passes_clean_llm_error_through() -> None:
    original = LLMBadRequestError("plain message", status_code=400)

    assert map_openai_error(original) is original
