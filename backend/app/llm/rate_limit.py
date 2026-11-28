"""Client-side pacing against provider token-per-minute limits.

OpenAI-compatible providers (OpenAI, Groq, Together, ...) report the remaining
budget on every response through ``x-ratelimit-remaining-tokens`` and
``x-ratelimit-reset-tokens``. ``RateLimitState`` remembers the latest values so the
client can wait for the window to refill *before* sending a request that would be
rejected, instead of hammering the provider with 429 retries. A 429 response feeds
the same state through its ``retry-after`` hint, so providers without the
``x-ratelimit-*`` headers are paced too.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping
from typing import Any

# Longest wait the client absorbs silently. Longer windows (daily caps) are left to
# fail fast so a job never hangs for minutes on one request.
MAX_PACE_WAIT_SECONDS = 60.0
# Output budget assumed per request when estimating what it will consume.
DEFAULT_OUTPUT_TOKENS = 512

_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")
_TRY_AGAIN = re.compile(r"try again in\s+((?:\d+(?:\.\d+)?(?:ms|h|m|s))+)", re.IGNORECASE)


def parse_duration(value: str | None) -> float | None:
    """Parse ``"7.66s"``, ``"1m2.5s"``, ``"120ms"`` or a bare number of seconds."""
    if value is None:
        return None
    text = value.strip().lower()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    parts = _DURATION_PART.findall(text)
    if not parts or "".join(num + unit for num, unit in parts) != text:
        return None
    factors = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
    return sum(float(num) * factors[unit] for num, unit in parts)


def _parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value.strip()))
    except ValueError:
        return None


def estimate_tokens(messages: list[dict[str, Any]], *, max_tokens: int | None = None) -> int:
    """Rough upper estimate of what a chat request consumes (prompt + output).

    About three characters per token keeps Vietnamese text on the safe side.
    """
    prompt_chars = len(json.dumps(messages, ensure_ascii=False))
    return prompt_chars // 3 + (max_tokens or DEFAULT_OUTPUT_TOKENS)


def retry_after_seconds(headers: Mapping[str, str] | None, message: str = "") -> float | None:
    """Wait hint of a 429: ``retry-after-ms``/``retry-after`` headers or the message text."""
    if headers is not None:
        ms = headers.get("retry-after-ms")
        if ms is not None:
            parsed = parse_duration(ms)
            if parsed is not None:
                return parsed / 1000.0
        parsed = parse_duration(headers.get("retry-after"))
        if parsed is not None:
            return parsed
    match = _TRY_AGAIN.search(message or "")
    if match:
        return parse_duration(match.group(1))
    return None


class RateLimitState:
    """Last known token budget of one provider key (shared by its requests)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._remaining: int | None = None
        self._reset_at: float = 0.0

    def update_from_headers(self, headers: Mapping[str, str]) -> None:
        remaining = _parse_int(headers.get("x-ratelimit-remaining-tokens"))
        reset = parse_duration(headers.get("x-ratelimit-reset-tokens"))
        if remaining is None:
            return
        self._remaining = remaining
        self._reset_at = self._clock() + (reset or 0.0)

    def note_rate_limited(self, retry_after: float | None) -> None:
        """Record a 429: nothing left until ``retry_after`` (default 5s) has passed."""
        self._remaining = 0
        self._reset_at = self._clock() + (retry_after if retry_after is not None else 5.0)

    def wait_for(self, estimated_tokens: int) -> float:
        """Seconds to wait before a request of ``estimated_tokens`` fits the budget."""
        if self._remaining is None:
            return 0.0
        now = self._clock()
        if now >= self._reset_at:
            self._remaining = None
            return 0.0
        if estimated_tokens <= self._remaining:
            return 0.0
        return self._reset_at - now

    def reserve(self, estimated_tokens: int) -> None:
        """Count a request in flight so concurrent callers see the smaller budget."""
        if self._remaining is not None:
            self._remaining = max(0, self._remaining - estimated_tokens)

    def window_refilled(self) -> None:
        """Forget the budget after waiting for the window to reset."""
        self._remaining = None
