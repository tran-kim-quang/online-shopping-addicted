"""LLM error hierarchy, secret sanitization, and OpenAI SDK exception mapping."""

from __future__ import annotations

import re
from collections.abc import Sequence

import httpx
import openai

_SECRET_PATTERNS = (
    re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{8,}", re.IGNORECASE),
    # ``sk-…``/``sk_…``/``gsk_…``/``or-…`` style provider keys (hyphen or underscore).
    re.compile(r"\b(sk|pk|gsk|or|key)[-_][A-Za-z0-9._\-]{8,}", re.IGNORECASE),
    re.compile(r"\b(sk_proj|sk_live|sk_test)_[A-Za-z0-9._\-]{8,}", re.IGNORECASE),
)


def sanitize_error_message(message: str, secrets: Sequence[str] = ()) -> str:
    """Remove any known secret values and common API key tokens from text."""
    sanitized = message
    for secret in secrets:
        if secret and len(secret) >= 4:
            sanitized = sanitized.replace(secret, "[REDACTED]")
    for pattern in _SECRET_PATTERNS:
        sanitized = pattern.sub(
            lambda m: f"{m.group(1)}[REDACTED]" if m.lastindex else "[REDACTED]",
            sanitized,
        )
    return sanitized


class LLMError(Exception):
    """Base error for all LLM layer failures."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class LLMAuthError(LLMError):
    """Authentication or authorization error (HTTP 401/403)."""


class LLMNotFoundError(LLMError):
    """Requested model or endpoint was not found (HTTP 404)."""


class LLMRateLimitError(LLMError):
    """Provider rate limit exceeded (HTTP 429)."""


class LLMUnavailableError(LLMError):
    """Provider server error, timeout, or connection failure (5xx / network)."""


class LLMBadRequestError(LLMError):
    """Invalid request payload, unsupported parameter, or disallowed redirect."""


class LLMInvalidResponseError(LLMError):
    """Provider returned malformed JSON or invalid tool call arguments."""

    def __init__(
        self,
        message: str,
        *,
        raw: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message, status_code=status_code)
        self.raw = raw


def map_openai_error(exc: Exception, *, api_key: str | None = None) -> LLMError:
    """Convert an OpenAI SDK / HTTP exception into a sanitized ``LLMError``."""
    secrets = (api_key,) if api_key else ()

    if isinstance(exc, LLMError):
        clean_msg = sanitize_error_message(exc.message, secrets)
        if clean_msg != exc.message:
            if isinstance(exc, LLMInvalidResponseError):
                return LLMInvalidResponseError(
                    clean_msg,
                    raw=sanitize_error_message(exc.raw, secrets) if exc.raw is not None else None,
                    status_code=exc.status_code,
                )
            return type(exc)(clean_msg, status_code=exc.status_code)
        return exc

    raw_message = sanitize_error_message(str(exc) or exc.__class__.__name__, secrets)

    if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return LLMAuthError(raw_message, status_code=getattr(exc, "status_code", 401))
    if isinstance(exc, openai.NotFoundError):
        return LLMNotFoundError(raw_message, status_code=getattr(exc, "status_code", 404))
    if isinstance(exc, openai.RateLimitError):
        return LLMRateLimitError(raw_message, status_code=getattr(exc, "status_code", 429))
    if isinstance(exc, (openai.BadRequestError, openai.UnprocessableEntityError)):
        return LLMBadRequestError(raw_message, status_code=getattr(exc, "status_code", 400))
    if isinstance(
        exc,
        (
            openai.APITimeoutError,
            openai.APIConnectionError,
            openai.InternalServerError,
            httpx.TimeoutException,
            httpx.ConnectError,
            httpx.TransportError,
        ),
    ):
        return LLMUnavailableError(raw_message, status_code=getattr(exc, "status_code", None))

    if isinstance(exc, openai.APIStatusError):
        code = exc.status_code
        if 300 <= code < 400:
            response = getattr(exc, "response", None)
            location = response.headers.get("location", "") if response is not None else ""
            clean_location = sanitize_error_message(location, secrets) if location else "<unknown>"
            return LLMBadRequestError(
                f"Provider responded with redirect (HTTP {code}) to {clean_location}; "
                "redirects are not followed. Use the final base_url directly.",
                status_code=code,
            )
        if code in (401, 403):
            return LLMAuthError(raw_message, status_code=code)
        if code == 404:
            return LLMNotFoundError(raw_message, status_code=code)
        if code == 429:
            return LLMRateLimitError(raw_message, status_code=code)
        if code in (400, 422):
            return LLMBadRequestError(raw_message, status_code=code)
        if code >= 500:
            return LLMUnavailableError(raw_message, status_code=code)
        return LLMError(raw_message, status_code=code)

    return LLMError(raw_message)
