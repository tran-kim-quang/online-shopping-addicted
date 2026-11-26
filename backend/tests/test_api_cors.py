"""CORS contract used by the Phase 5 Next.js client (``frontend/``).

The web app runs on ``http://localhost:3000`` and calls the API cross-origin, streams
SSE from ``/api/jobs/{id}/events`` (browser sends ``Last-Event-ID`` while reconnecting)
and downloads CSV via plain ``<a download>`` links (filename comes from
``Content-Disposition``). These tests lock that contract: configuration parsing,
origin allow-listing, preflight headers, and the exposed response header.

No DB, no outbound network: the app is driven in-process through ``httpx.ASGITransport``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
import pytest

from app.core.config import DEFAULT_CORS_ALLOW_ORIGINS, Settings, parse_cors_origins
from app.main import _resolve_cors_origins, create_app

ALLOWED_ORIGIN = "http://localhost:3000"
BLOCKED_ORIGIN = "https://evil.example"


# ---------------------------------------------------------------------------
# Configuration parsing
# ---------------------------------------------------------------------------


def test_default_origins_are_localhost_web_app() -> None:
    assert DEFAULT_CORS_ALLOW_ORIGINS == ("http://localhost:3000",)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, ["http://localhost:3000"]),
        ("http://localhost:3000", ["http://localhost:3000"]),
        (
            " http://localhost:3000 , https://shop-qa.example ",
            ["http://localhost:3000", "https://shop-qa.example"],
        ),
        ("http://a,http://b,", ["http://a", "http://b"]),
        ('["http://a", "http://b"]', ["http://a", "http://b"]),
        (["http://a", " http://b "], ["http://a", "http://b"]),
        ("   ", []),
    ],
)
def test_parse_cors_origins_accepts_comma_string_json_and_sequence(
    raw: object, expected: list[str]
) -> None:
    assert parse_cors_origins(raw) == expected


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        ("*", "Wildcard"),
        ("http://a,*", "Wildcard"),
        ("[not json", "Expecting"),
        ("[", "Expecting"),
        (42, "comma-separated string"),
    ],
)
def test_parse_cors_origins_rejects_wildcard_and_malformed_values(raw: object, match: str) -> None:
    with pytest.raises((ValueError, json.JSONDecodeError), match=match):
        parse_cors_origins(raw)


def test_parse_cors_origins_json_object_like_string_is_kept_literal() -> None:
    # Only values starting with ``[`` are JSON-decoded; a brace-delimited value is kept as
    # one literal origin, which simply never matches a browser ``Origin`` header.
    assert parse_cors_origins('{"origins": ["http://a"]}') == ['{"origins": ["http://a"]}']


def test_settings_reads_comma_separated_env_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "http://localhost:3000,https://app.example")
    settings = Settings(SECRET_KEY="vN5y6rM0J9h2xL8qK3pW1zB4cF7dG0tH2sV5mN8wX4Q=")

    assert settings.CORS_ALLOW_ORIGINS == ["http://localhost:3000", "https://app.example"]


def test_settings_default_when_env_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORS_ALLOW_ORIGINS", raising=False)
    settings = Settings(SECRET_KEY="vN5y6rM0J9h2xL8qK3pW1zB4cF7dG0tH2sV5mN8wX4Q=")

    assert settings.CORS_ALLOW_ORIGINS == ["http://localhost:3000"]


def test_resolve_origins_falls_back_to_env_when_settings_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Missing SECRET_KEY makes ``Settings()`` raise; the env fallback must still work.
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000")

    assert _resolve_cors_origins() == ["http://localhost:3000", "http://127.0.0.1:3000"]


# ---------------------------------------------------------------------------
# Middleware behaviour
# ---------------------------------------------------------------------------


@pytest.fixture
async def cors_client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    """App instance with the default allow-list, driven in-process (no sockets)."""
    monkeypatch.delenv("CORS_ALLOW_ORIGINS", raising=False)
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        yield client


async def test_simple_request_from_allowed_origin_is_echoed_with_exposed_headers(
    cors_client: httpx.AsyncClient,
) -> None:
    response = await cors_client.get("/health", headers={"Origin": ALLOWED_ORIGIN})

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    # CSV download reads the filename from this header in the browser.
    assert "content-disposition" in response.headers["access-control-expose-headers"].lower()
    assert "origin" in response.headers["vary"].lower()


async def test_simple_request_from_unknown_origin_gets_no_allow_origin(
    cors_client: httpx.AsyncClient,
) -> None:
    response = await cors_client.get("/health", headers={"Origin": BLOCKED_ORIGIN})

    # The handler still runs server-side; the browser blocks the response because the
    # allow-origin header is absent (no wildcard, ever).
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


async def test_preflight_allows_post_get_patch_delete_only(
    cors_client: httpx.AsyncClient,
) -> None:
    response = await cors_client.options(
        "/api/ingest",
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,last-event-id",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    methods_header = response.headers["access-control-allow-methods"]
    allowed_methods = {method.strip() for method in methods_header.split(",")}
    assert allowed_methods == {"GET", "POST", "PATCH", "DELETE"}
    allowed_headers = {
        h.strip().lower() for h in response.headers["access-control-allow-headers"].split(",")
    }
    assert {"content-type", "last-event-id"} <= allowed_headers
    assert "access-control-allow-credentials" not in response.headers


async def test_preflight_rejects_method_outside_allow_list(cors_client: httpx.AsyncClient) -> None:
    response = await cors_client.options(
        "/api/ingest",
        headers={"Origin": ALLOWED_ORIGIN, "Access-Control-Request-Method": "PUT"},
    )

    # Starlette answers 400 "Disallowed CORS method"; the browser drops the preflight even
    # though the allow-list is echoed, so PUT can never reach the API from the web app.
    assert response.status_code == 400
    assert "disallowed cors method" in response.text.lower()


async def test_preflight_from_unknown_origin_fails(cors_client: httpx.AsyncClient) -> None:
    response = await cors_client.options(
        "/api/ingest",
        headers={"Origin": BLOCKED_ORIGIN, "Access-Control-Request-Method": "POST"},
    )

    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


async def test_configured_extra_origin_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000")
    app = create_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/health", headers={"Origin": "http://127.0.0.1:3000"})

    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:3000"


async def test_sse_endpoint_preflight_allows_last_event_id_header(
    cors_client: httpx.AsyncClient,
) -> None:
    response = await cors_client.options(
        "/api/jobs/job-1/events",
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "last-event-id",
        },
    )

    assert response.status_code == 200
    assert "last-event-id" in response.headers["access-control-allow-headers"].lower()
