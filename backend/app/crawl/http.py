"""Safe, rate-limited, retrying HTTP client wrapper for crawlers."""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.core.config import get_settings
from app.crawl.errors import BlockedError, NotFoundError, UpstreamError
from app.crawl.rate_limit import DomainRateLimiter
from app.net.pinned import make_safe_client
from app.security.url_validator import DNSResolver, UnsafeURLError, safe_request

DEFAULT_BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
}

_CAPTCHA_MARKERS = (
    "g-recaptcha",
    "h-captcha",
    "cf-challenge",
    "challenge-platform",
    "verify you are human",
    "enter the characters you see",
    "x5sec",
    "punish",
    "captcha",
)

_JSON_BLOCK_KEYS = frozenset(
    {
        "captcha",
        "captcha_url",
        "captcha_id",
        "captcha_token",
        "captcha_type",
        "verify",
        "verify_url",
        "verify_type",
        "verify_fp",
        "need_verify",
        "challenge",
        "challenge_url",
        "x5sec",
        "x5secdata",
        "rgv587_flag",
        "baxia",
        "blocked",
        "is_blocked",
        "bot_detected",
        "waf_action",
    }
)

_JSON_URL_KEYS = frozenset(
    {
        "url",
        "redirect",
        "redirect_url",
        "target_url",
        "next_url",
        "location",
        "action_url",
        "verify_link",
        "challenge_link",
    }
)

_JSON_URL_BLOCK_MARKERS = (
    "captcha",
    "verify",
    "challenge",
    "punish",
    "x5sec",
    "robot",
    "bot-check",
    "botcheck",
    "waf",
    "sec/verify",
    "user_validate",
)

_JSON_MESSAGE_KEYS = frozenset(
    {
        "error",
        "err",
        "message",
        "msg",
        "reason",
        "detail",
        "ret",
        "code",
        "error_code",
        "error_type",
        "action",
    }
)

_JSON_STATUS_BLOCK_VALUES = frozenset(
    {
        "blocked",
        "captcha",
        "captcha_required",
        "challenge",
        "forbidden",
        "denied",
        "access_denied",
        "verify",
        "bot_detected",
    }
)

_JSON_MESSAGE_BLOCK_PHRASES = (
    "captcha",
    "verify you are human",
    "human verification",
    "please verify",
    "access denied",
    "request blocked",
    "you have been blocked",
    "bot detected",
    "anti-bot",
    "antibot",
    "rgv587",
    "fail_sys_user_validate",
    "x5sec",
    "punish",
    "robot check",
    "are you a robot",
    "cf-challenge",
    "security check",
    "forbidden",
    "blocked",
)

_JSON_SKIP_SUBTREE_KEYS = frozenset(
    {
        "description",
        "short_description",
        "specifications",
        "images",
        "breadcrumbs",
        "reviews",
        "productset_group_name",
    }
)

SleepFn = Callable[[float], Awaitable[None]]
JitterFn = Callable[[float], float]


def _parse_retry_after(header_value: str | None) -> float | None:
    if not header_value:
        return None
    stripped = header_value.strip()
    if not stripped:
        return None
    try:
        seconds = float(stripped)
        return max(0.0, seconds)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(stripped)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        delta = (dt - datetime.now(UTC)).total_seconds()
        return max(0.0, delta)
    except Exception:
        return None


def _is_truthy_block_value(value: Any) -> bool:
    if value is None or value is False or value == 0:
        return False
    if isinstance(value, str):
        lowered = value.strip().lower()
        return lowered not in ("", "0", "false", "no", "none", "null")
    if isinstance(value, (list, dict)):
        return len(value) > 0
    return True


def _inspect_json_for_block(obj: Any, *, depth: int = 0) -> str | None:
    if depth > 3 or not isinstance(obj, dict):
        return None

    for raw_key, value in obj.items():
        if not isinstance(raw_key, str):
            continue
        key = raw_key.lower()
        if key in _JSON_SKIP_SUBTREE_KEYS:
            continue

        if key in _JSON_BLOCK_KEYS and _is_truthy_block_value(value):
            return f"JSON anti-bot/captcha field '{raw_key}'"

        if key in _JSON_URL_KEYS and isinstance(value, str):
            val_lower = value.lower()
            for marker in _JSON_URL_BLOCK_MARKERS:
                if marker in val_lower:
                    return f"JSON verify/captcha redirect URL in '{raw_key}' ({marker})"

        if key == "status" and isinstance(value, str):
            if value.strip().lower() in _JSON_STATUS_BLOCK_VALUES:
                return f"JSON blocked status '{value}'"

        if key in _JSON_MESSAGE_KEYS:
            candidates: list[str] = []
            if isinstance(value, str):
                candidates.append(value)
            elif isinstance(value, list):
                candidates.extend(item for item in value if isinstance(item, str))
            for text_val in candidates:
                val_lower = text_val.lower()
                for phrase in _JSON_MESSAGE_BLOCK_PHRASES:
                    if phrase in val_lower:
                        return f"JSON block indicator '{phrase}' in '{raw_key}'"

        if isinstance(value, dict):
            nested = _inspect_json_for_block(value, depth=depth + 1)
            if nested is not None:
                return nested

    return None


def detect_block(response: httpx.Response) -> str | None:
    """Detect captcha/anti-bot block signals in HTML, plain text, or JSON responses.

    Returns a short human-readable reason string when a block/challenge is detected,
    or ``None`` if the response looks normal.
    """
    content_type = response.headers.get("content-type", "").lower()
    try:
        body = response.text
    except Exception:
        return None

    stripped = body.lstrip()

    # 1. JSON payload inspection (covers anti-bot APIs returning HTTP 200 JSON).
    is_json_candidate = "json" in content_type or stripped.startswith("{")
    if is_json_candidate and stripped:
        try:
            payload = json.loads(stripped)
        except Exception:
            payload = None
        else:
            if isinstance(payload, dict):
                reason = _inspect_json_for_block(payload)
                if reason is not None:
                    return reason
            # Valid JSON without block signals: don't run HTML substring scan on raw JSON.
            if payload is not None:
                return None

    # 2. HTML / text / malformed-JSON-that-is-HTML captcha marker scan.
    if "html" in content_type or "text/plain" in content_type or stripped.startswith("<"):
        body_lower = body[:32768].lower()
        for marker in _CAPTCHA_MARKERS:
            if marker in body_lower:
                return f"captcha/challenge marker '{marker}' in response body"

    return None


def _is_captcha_response(response: httpx.Response) -> bool:
    return detect_block(response) is not None


class CrawlHttp:
    """High-level HTTP wrapper combining DNS pinning, ``safe_request``, rate limiting, and retry."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        rate_limiter: DomainRateLimiter | None = None,
        allow_private: bool | None = None,
        resolver: DNSResolver | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        base_backoff: float = 0.5,
        headers: Mapping[str, str] | None = None,
        sleep: SleepFn | None = None,
        jitter: JitterFn | None = None,
    ) -> None:
        try:
            settings = get_settings()
        except Exception:
            settings = None

        self._allow_private: bool = (
            allow_private
            if allow_private is not None
            else (settings.ALLOW_PRIVATE_CRAWL_HOSTS if settings is not None else False)
        )
        self._timeout: float = (
            timeout
            if timeout is not None
            else (settings.CRAWL_TIMEOUT_SECONDS if settings is not None else 20.0)
        )
        self._max_retries: int = (
            max_retries
            if max_retries is not None
            else (settings.CRAWL_MAX_RETRIES if settings is not None else 3)
        )
        self._base_backoff = base_backoff
        self._rate_limiter: DomainRateLimiter = (
            rate_limiter if rate_limiter is not None else DomainRateLimiter()
        )
        self._sleep: SleepFn = sleep if sleep is not None else asyncio.sleep
        self._jitter: JitterFn = (
            jitter if jitter is not None else (lambda cap: random.uniform(0, cap))
        )
        self._default_headers: dict[str, str] = dict(DEFAULT_BROWSER_HEADERS)
        if headers:
            self._default_headers.update(headers)

        if client is None:
            self._client = make_safe_client(
                allow_private=self._allow_private,
                resolver=resolver,
                timeout=self._timeout,
                headers=self._default_headers,
            )
            self._owns_client = True
            self._resolver = resolver
        else:
            self._client = client
            self._owns_client = False
            self._resolver = resolver

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client

    @property
    def allow_private(self) -> bool:
        return self._allow_private

    def _compute_backoff(self, attempt: int, retry_after: str | None = None) -> float:
        parsed_ra = _parse_retry_after(retry_after)
        if parsed_ra is not None:
            return parsed_ra
        base = self._base_backoff * (2**attempt)
        return base + self._jitter(min(0.5, base * 0.5))

    def _merge_headers(self, headers: Mapping[str, str] | None) -> dict[str, str]:
        merged = dict(self._default_headers)
        if headers:
            merged.update(headers)
        return merged

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        max_redirects: int = 5,
        **kwargs: Any,
    ) -> httpx.Response:
        """Send a rate-limited, SSRF-validated HTTP request with retry on 429/5xx/timeouts."""
        parsed = urlsplit(url)
        domain = (parsed.hostname or "").lower()
        merged_headers = self._merge_headers(headers)

        for attempt in range(self._max_retries + 1):
            if domain:
                await self._rate_limiter.acquire(domain)

            try:
                response = await safe_request(
                    self._client,
                    method,
                    url,
                    allow_private=self._allow_private,
                    max_redirects=max_redirects,
                    resolver=self._resolver,
                    params=params,
                    headers=merged_headers,
                    **kwargs,
                )
            except UnsafeURLError:
                raise
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt < self._max_retries:
                    await self._sleep(self._compute_backoff(attempt))
                    continue
                raise UpstreamError(f"Network error requesting {url}: {exc}") from exc

            code = response.status_code

            if code == 403:
                await response.aclose()
                raise BlockedError(f"Access forbidden (HTTP 403) for {url}", status_code=403)

            if code == 404:
                await response.aclose()
                raise NotFoundError(f"Resource not found (HTTP 404) for {url}", status_code=404)

            if code == 429 or 500 <= code < 600:
                retry_after = response.headers.get("retry-after")
                await response.aclose()
                if attempt < self._max_retries:
                    await self._sleep(self._compute_backoff(attempt, retry_after=retry_after))
                    continue
                raise UpstreamError(
                    f"Upstream returned HTTP {code} for {url} after "
                    f"{self._max_retries + 1} attempts.",
                    status_code=code,
                )

            if code >= 400:
                await response.aclose()
                raise UpstreamError(f"Upstream returned HTTP {code} for {url}", status_code=code)

            block_reason = detect_block(response)
            if block_reason is not None:
                await response.aclose()
                raise BlockedError(
                    f"Upstream returned a captcha/block response for {url}: {block_reason}",
                    status_code=code,
                )

            return response

        raise UpstreamError(f"Failed to fetch {url}")  # pragma: no cover

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        max_redirects: int = 5,
    ) -> httpx.Response:
        """Perform a GET request through :meth:`request`."""
        return await self.request(
            "GET",
            url,
            params=params,
            headers=headers,
            max_redirects=max_redirects,
        )

    async def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        max_redirects: int = 5,
    ) -> dict[str, Any]:
        """Perform a GET request and decode a JSON object body."""
        response = await self.get(url, params=params, headers=headers, max_redirects=max_redirects)
        try:
            payload = response.json()
        except Exception as exc:
            block_reason = detect_block(response)
            if block_reason is not None:
                raise BlockedError(
                    f"Upstream returned a captcha/block response for {url}: {block_reason}",
                    status_code=response.status_code,
                ) from exc
            raise UpstreamError(
                f"Invalid JSON response from {url}: {exc}",
                status_code=response.status_code,
            ) from exc
        finally:
            await response.aclose()

        if not isinstance(payload, dict):
            raise UpstreamError(
                f"Expected JSON object from {url}, got {type(payload).__name__}.",
                status_code=response.status_code,
            )
        return payload

    async def aclose(self) -> None:
        """Close the underlying ``httpx.AsyncClient`` if owned by this wrapper."""
        if self._owns_client and not self._client.is_closed:
            await self._client.aclose()

    async def __aenter__(self) -> CrawlHttp:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.aclose()
