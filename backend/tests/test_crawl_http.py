"""Unit tests for ``CrawlHttp``: SSRF guard, rate limiting, retry, error mapping."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import httpx
import pytest

from app.crawl.errors import BlockedError, NotFoundError, UpstreamError
from app.crawl.http import (
    DEFAULT_BROWSER_HEADERS,
    CrawlHttp,
    _mock_public_resolver,
    _parse_retry_after,
)
from app.security.url_validator import UnsafeURLError
from tests.crawl_fakes import make_crawl_http

URL = "https://tiki.vn/api/v2/products/1"


class RateLimiterSpy:
    def __init__(self) -> None:
        self.domains: list[str] = []

    async def acquire(self, domain: str, *, rate_per_second: float | None = None) -> float:
        self.domains.append(domain)
        return 0.0


# ---------------------------------------------------------------------------
# Successful requests
# ---------------------------------------------------------------------------


async def test_get_returns_response_and_sends_browser_headers() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    http, sleeps = make_crawl_http(handler)
    response = await http.get(URL, params={"page": 1}, headers={"Referer": "https://tiki.vn/"})

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert seen[0].headers["user-agent"] == DEFAULT_BROWSER_HEADERS["User-Agent"]
    assert seen[0].headers["accept-language"] == DEFAULT_BROWSER_HEADERS["Accept-Language"]
    assert seen[0].headers["referer"] == "https://tiki.vn/"
    assert seen[0].url.params["page"] == "1"
    assert sleeps == []


async def test_get_json_returns_object() -> None:
    http, _ = make_crawl_http(lambda _request: httpx.Response(200, json={"id": 1}))

    assert await http.get_json(URL) == {"id": 1}


async def test_rate_limiter_is_called_once_per_attempt() -> None:
    limiter = RateLimiterSpy()
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(500)
        return httpx.Response(200, json={})

    http, _ = make_crawl_http(handler, rate_limiter=limiter, max_retries=3)
    await http.get(URL)

    assert calls["n"] == 3
    assert limiter.domains == ["tiki.vn", "tiki.vn", "tiki.vn"]


# ---------------------------------------------------------------------------
# 403 / 404 / captcha / 4xx
# ---------------------------------------------------------------------------


async def test_403_raises_blocked_without_retry() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(403, text="forbidden")

    http, sleeps = make_crawl_http(handler, max_retries=3)

    with pytest.raises(BlockedError) as excinfo:
        await http.get(URL)

    assert excinfo.value.status_code == 403
    assert calls["n"] == 1
    assert sleeps == []


async def test_404_raises_not_found_without_retry() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, json={"status": 404})

    http, _ = make_crawl_http(handler)

    with pytest.raises(NotFoundError) as excinfo:
        await http.get(URL)

    assert excinfo.value.status_code == 404
    assert calls["n"] == 1


@pytest.mark.parametrize("status", [400, 401, 418, 422])
async def test_other_4xx_raise_upstream_error_without_retry(status: int) -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(status, text="nope")

    http, _ = make_crawl_http(handler)

    with pytest.raises(UpstreamError) as excinfo:
        await http.get(URL)

    assert excinfo.value.status_code == status
    assert calls["n"] == 1


@pytest.mark.parametrize(
    "body",
    [
        "<html><body><div class='g-recaptcha'></div></body></html>",
        "<html><body><h1>Verify you are human</h1></body></html>",
        "<html><body>Please enter the characters you see</body></html>",
        "<html><body><script src='/cf-challenge.js'></script></body></html>",
    ],
)
async def test_html_captcha_page_raises_blocked(body: str) -> None:
    http, sleeps = make_crawl_http(
        lambda _request: httpx.Response(200, text=body, headers={"content-type": "text/html"})
    )

    with pytest.raises(BlockedError, match="captcha"):
        await http.get(URL)

    assert sleeps == []


async def test_plain_html_without_captcha_markers_is_returned() -> None:
    body = "<html><body><p>Sản phẩm</p></body></html>"
    http, _ = make_crawl_http(
        lambda _request: httpx.Response(200, text=body, headers={"content-type": "text/html"})
    )

    response = await http.get(URL)

    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Retry: 429 / 5xx / timeouts
# ---------------------------------------------------------------------------


async def test_429_with_retry_after_seconds_is_honoured() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "1.5"})
        return httpx.Response(200, json={"ok": True})

    http, sleeps = make_crawl_http(handler, max_retries=2)
    response = await http.get_json(URL)

    assert response == {"ok": True}
    assert calls["n"] == 2
    assert sleeps == pytest.approx([1.5])


async def test_429_with_retry_after_http_date_is_honoured() -> None:
    retry_at = format_datetime(datetime.now(UTC) + timedelta(seconds=5), usegmt=True)
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": retry_at})
        return httpx.Response(200, json={})

    http, sleeps = make_crawl_http(handler, max_retries=1)
    await http.get(URL)

    assert len(sleeps) == 1
    assert 3.0 <= sleeps[0] <= 6.0


async def test_5xx_is_retried_with_exponential_backoff() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={})

    http, sleeps = make_crawl_http(handler, max_retries=3, base_backoff=0.5)
    await http.get(URL)

    assert calls["n"] == 3
    assert sleeps == pytest.approx([0.5, 1.0])


async def test_5xx_exhausting_retries_raises_upstream_error() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(502)

    http, sleeps = make_crawl_http(handler, max_retries=2)
    with pytest.raises(UpstreamError, match="HTTP 502") as excinfo:
        await http.get(URL)

    assert excinfo.value.status_code == 502
    assert calls["n"] == 3
    assert len(sleeps) == 2


async def test_zero_retries_fails_immediately_on_429() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, headers={"retry-after": "30"})

    http, sleeps = make_crawl_http(handler, max_retries=0)

    with pytest.raises(UpstreamError):
        await http.get(URL)

    assert calls["n"] == 1
    assert sleeps == []


async def test_timeout_is_retried_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ReadTimeout("timed out", request=None)
        return httpx.Response(200, json={})

    http, sleeps = make_crawl_http(handler, max_retries=3)
    await http.get(URL)

    assert calls["n"] == 3
    assert sleeps == pytest.approx([0.5, 1.0])


async def test_transport_error_exhausting_retries_raises_upstream_error() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("dns down", request=None)

    http, _ = make_crawl_http(handler, max_retries=1)

    with pytest.raises(UpstreamError, match="Network error"):
        await http.get(URL)

    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# SSRF guard
# ---------------------------------------------------------------------------


async def test_metadata_url_is_rejected_before_any_request() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={})

    http, _ = make_crawl_http(handler, max_retries=3)

    with pytest.raises(UnsafeURLError, match="metadata"):
        await http.get("http://169.254.169.254/latest/meta-data/")

    assert calls["n"] == 0


async def test_private_ip_url_is_rejected_unless_allowed() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    http, _ = make_crawl_http(handler, allow_private=False)
    with pytest.raises(UnsafeURLError):
        await http.get("http://10.0.0.5/admin")

    allowed, _ = make_crawl_http(handler, allow_private=True)
    response = await allowed.get("http://10.0.0.5/admin")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# JSON decoding
# ---------------------------------------------------------------------------


async def test_invalid_json_raises_upstream_error() -> None:
    http, _ = make_crawl_http(
        lambda _request: httpx.Response(
            200, text="not json", headers={"content-type": "text/plain"}
        )
    )

    with pytest.raises(UpstreamError, match="Invalid JSON"):
        await http.get_json(URL)


async def test_invalid_json_that_is_a_captcha_page_raises_blocked() -> None:
    body = "<html><body>captcha required</body></html>"
    http, _ = make_crawl_http(
        lambda _request: httpx.Response(200, text=body, headers={"content-type": "text/html"})
    )

    with pytest.raises(BlockedError):
        await http.get_json(URL)


async def test_non_object_json_raises_upstream_error() -> None:
    http, _ = make_crawl_http(lambda _request: httpx.Response(200, json=[1, 2, 3]))

    with pytest.raises(UpstreamError, match="Expected JSON object"):
        await http.get_json(URL)


# ---------------------------------------------------------------------------
# Backoff helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("nonsense", None),
        ("0", 0.0),
        ("-3", 0.0),
        ("2.5", 2.5),
        ("Wed, 99 Foo 2026 00:00:00 GMT", None),
    ],
)
def test_parse_retry_after(header: str | None, expected: float | None) -> None:
    assert _parse_retry_after(header) == expected


def test_compute_backoff_grows_exponentially_without_jitter() -> None:
    http, _ = make_crawl_http(lambda _request: httpx.Response(200))

    assert http._compute_backoff(0) == pytest.approx(0.5)
    assert http._compute_backoff(1) == pytest.approx(1.0)
    assert http._compute_backoff(3) == pytest.approx(4.0)


def test_compute_backoff_prefers_retry_after_header() -> None:
    http, _ = make_crawl_http(lambda _request: httpx.Response(200))

    assert http._compute_backoff(2, retry_after="7") == pytest.approx(7.0)


def test_compute_backoff_adds_jitter(monkeypatch) -> None:
    http, _ = make_crawl_http(lambda _request: httpx.Response(200))
    http._jitter = lambda cap: cap  # type: ignore[method-assign]

    # base 0.5 * 2**1 = 1.0, jitter cap = min(0.5, 0.5) = 0.5
    assert http._compute_backoff(1) == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# Construction / ownership
# ---------------------------------------------------------------------------


async def test_default_client_is_safe_and_not_owned_by_caller() -> None:
    http = CrawlHttp()

    assert http.allow_private is False
    assert http.client.follow_redirects is False
    assert http.client.trust_env is False
    assert http._owns_client is True

    await http.aclose()
    assert http.client.is_closed is True


async def test_injected_client_is_not_closed() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(200)))
    http = CrawlHttp(client=client)

    await http.aclose()

    assert client.is_closed is False
    assert http.client is client
    await client.aclose()


async def test_async_context_manager_closes_owned_client() -> None:
    async with CrawlHttp() as http:
        assert http.client.is_closed is False
    assert http.client.is_closed is True


async def test_mock_public_resolver_is_offline_placeholder() -> None:
    assert await _mock_public_resolver("anything.example") == ["93.184.216.34"]


async def test_explicit_settings_overrides_are_used() -> None:
    http = CrawlHttp(allow_private=True, timeout=3.0, max_retries=0)

    assert http.allow_private is True
    assert http._timeout == 3.0
    assert http._max_retries == 0
    await http.aclose()


async def test_default_headers_can_be_extended() -> None:
    http = CrawlHttp(headers={"Referer": "https://tiki.vn/", "X-Extra": "1"})

    assert http._default_headers["Referer"] == "https://tiki.vn/"
    assert http._default_headers["X-Extra"] == "1"
    assert http._default_headers["User-Agent"] == DEFAULT_BROWSER_HEADERS["User-Agent"]
    await http.aclose()


async def test_merge_headers_does_not_mutate_defaults() -> None:
    http = CrawlHttp()
    merged = http._merge_headers({"X-Test": "1"})
    merged["User-Agent"] = "tampered"

    assert http._default_headers["User-Agent"] == DEFAULT_BROWSER_HEADERS["User-Agent"]
    await http.aclose()


async def test_custom_resolver_is_kept_for_injected_clients() -> None:
    async def _resolver(_host: str) -> Any:
        return ["93.184.216.34"]

    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(200)))
    http = CrawlHttp(client=client, resolver=_resolver)

    assert http._resolver is _resolver
    await client.aclose()


async def test_mock_transport_gets_offline_resolver_by_default() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(200)))
    http = CrawlHttp(client=client)

    assert http._resolver is _mock_public_resolver
    await client.aclose()


async def test_settings_unreadable_falls_back_to_defaults(monkeypatch) -> None:
    import app.crawl.http as module

    def _boom() -> Any:
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(module, "get_settings", _boom)
    http = CrawlHttp()

    assert http.allow_private is False
    assert http._timeout == 20.0
    assert http._max_retries == 3
    await http.aclose()
