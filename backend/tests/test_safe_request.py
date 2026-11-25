"""Unit tests for ``app.security.url_validator.safe_request``.

Every request goes through ``httpx.MockTransport`` (no socket) and DNS is faked
by an in-memory resolver, so public/private/metadata hops are fully deterministic.
"""

from __future__ import annotations

import httpx
import pytest

from app.security.url_validator import UnsafeURLError, safe_request

PUBLIC_IP = "93.184.216.34"
PRIVATE_IP = "10.0.0.5"
METADATA_IP = "169.254.169.254"
METADATA_LOCATION = "http://169.254.169.254/latest/meta-data/iam/security-credentials/"

_HOST_IPS = {
    "public.test": PUBLIC_IP,
    "other-public.test": "93.184.216.35",
    "private.test": PRIVATE_IP,
    "metadata.test": METADATA_IP,
}


async def fake_resolver(host: str) -> list[str]:
    return [_HOST_IPS[host]]


def make_client(handler, *, follow_redirects: bool = False) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://public.test",
        follow_redirects=follow_redirects,
    )


class Recorder:
    """Collects every request that actually reached the transport layer."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, text=f"ok:{request.method}:{request.url.path}")

    @property
    def urls(self) -> list[str]:
        return [str(request.url) for request in self.requests]

    @property
    def methods(self) -> list[str]:
        return [request.method for request in self.requests]


async def test_public_to_public_redirect_is_followed() -> None:
    recorder = Recorder()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            recorder.requests.append(request)
            return httpx.Response(302, headers={"location": "/final"})
        return recorder.handler(request)

    client = make_client(handler)
    try:
        response = await safe_request(
            client,
            "GET",
            "http://public.test/start",
            allow_private=False,
            resolver=fake_resolver,
        )
    finally:
        await client.aclose()

    assert response.status_code == 200
    assert response.text == "ok:GET:/final"
    assert recorder.urls == ["http://public.test/start", "http://public.test/final"]


async def test_redirect_to_metadata_ip_is_blocked_before_request() -> None:
    recorder = Recorder()

    async def handler(request: httpx.Request) -> httpx.Response:
        recorder.requests.append(request)
        if request.url.host == "public.test":
            return httpx.Response(302, headers={"location": METADATA_LOCATION})
        return httpx.Response(200)

    client = make_client(handler)
    try:
        with pytest.raises(UnsafeURLError, match="metadata"):
            await safe_request(
                client,
                "GET",
                "http://public.test/start",
                allow_private=False,
                resolver=fake_resolver,
            )
    finally:
        await client.aclose()

    # Only the public hop was contacted; the metadata hop never was.
    assert [str(request.url) for request in recorder.requests] == ["http://public.test/start"]


async def test_redirect_to_private_host_blocked_when_private_disallowed() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://private.test/secret"})

    client = make_client(handler)
    try:
        with pytest.raises(UnsafeURLError, match="non-public"):
            await safe_request(
                client,
                "GET",
                "http://public.test/start",
                allow_private=False,
                resolver=fake_resolver,
            )
    finally:
        await client.aclose()


async def test_redirect_to_private_host_allowed_when_private_allowed() -> None:
    recorder = Recorder()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "public.test":
            recorder.requests.append(request)
            return httpx.Response(302, headers={"location": "http://private.test/secret"})
        return recorder.handler(request)

    client = make_client(handler)
    try:
        response = await safe_request(
            client,
            "GET",
            "http://public.test/start",
            allow_private=True,
            resolver=fake_resolver,
        )
    finally:
        await client.aclose()

    assert response.text == "ok:GET:/secret"
    assert recorder.urls == ["http://public.test/start", "http://private.test/secret"]


async def test_relative_location_is_resolved_against_current_url() -> None:
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/a/b/start":
            return httpx.Response(302, headers={"location": "../final?x=1"})
        return httpx.Response(200, text="done")

    client = make_client(handler)
    try:
        response = await safe_request(
            client,
            "GET",
            "http://public.test/a/b/start",
            allow_private=False,
            resolver=fake_resolver,
        )
    finally:
        await client.aclose()

    assert response.status_code == 200
    assert seen == ["http://public.test/a/b/start", "http://public.test/a/final?x=1"]


async def test_redirect_loop_exceeding_max_redirects_raises() -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(307, headers={"location": "/loop"})

    client = make_client(handler)
    try:
        with pytest.raises(UnsafeURLError, match="Exceeded maximum redirects"):
            await safe_request(
                client,
                "GET",
                "http://public.test/loop",
                allow_private=False,
                max_redirects=2,
                resolver=fake_resolver,
            )
    finally:
        await client.aclose()

    # initial request + 2 followed hops (the 3rd redirect is rejected before sending)
    assert len(calls) == 3


async def test_zero_max_redirects_rejects_first_redirect() -> None:
    """``max_redirects=0`` allows no hop at all: the 302 is reported as unsafe."""
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "/final"})

    client = make_client(handler)
    try:
        with pytest.raises(UnsafeURLError, match="Exceeded maximum redirects"):
            await safe_request(
                client,
                "GET",
                "http://public.test/start",
                allow_private=False,
                max_redirects=0,
                resolver=fake_resolver,
            )
    finally:
        await client.aclose()

    assert calls == ["http://public.test/start"]


async def test_post_303_switches_to_get_and_drops_body() -> None:
    recorded: list[tuple[str, bytes]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        recorded.append((request.method, request.content))
        if request.method == "POST":
            return httpx.Response(303, headers={"location": "/result"})
        return httpx.Response(200, text="created")

    client = make_client(handler)
    try:
        response = await safe_request(
            client,
            "POST",
            "http://public.test/submit",
            allow_private=False,
            resolver=fake_resolver,
            json={"answer": 42},
            headers={"content-type": "application/json", "x-trace": "abc"},
        )
    finally:
        await client.aclose()

    assert response.status_code == 200
    assert [method for method, _body in recorded] == ["POST", "GET"]
    assert recorded[0][1]  # first hop carried the JSON body
    assert recorded[1][1] == b""  # body stripped after the method change


async def test_307_post_keeps_method_and_body() -> None:
    recorded: list[tuple[str, bytes]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        recorded.append((request.method, request.content))
        if request.method == "POST" and request.url.path == "/submit":
            return httpx.Response(307, headers={"location": "/submit-final"})
        return httpx.Response(200, text="ok")

    client = make_client(handler)
    try:
        await safe_request(
            client,
            "POST",
            "http://public.test/submit",
            allow_private=False,
            resolver=fake_resolver,
            json={"answer": 42},
        )
    finally:
        await client.aclose()

    assert [method for method, _body in recorded] == ["POST", "POST"]
    assert recorded[1][1] == recorded[0][1]


async def test_cross_origin_redirect_strips_authorization_header() -> None:
    seen_auth: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers.get("authorization"))
        if request.url.host == "public.test":
            return httpx.Response(302, headers={"location": "http://other-public.test/final"})
        return httpx.Response(200, text="ok")

    client = make_client(handler)
    try:
        await safe_request(
            client,
            "GET",
            "http://public.test/start",
            allow_private=False,
            resolver=lambda host: fake_resolver(host),
            headers={"authorization": "Bearer super-secret-token", "x-trace": "abc"},
        )
    finally:
        await client.aclose()

    assert seen_auth[0] == "Bearer super-secret-token"
    assert seen_auth[1] is None


async def test_redirect_without_location_header_returns_response() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302)

    client = make_client(handler)
    try:
        response = await safe_request(
            client,
            "GET",
            "http://public.test/start",
            allow_private=False,
            resolver=fake_resolver,
        )
    finally:
        await client.aclose()

    assert response.status_code == 302


async def test_initial_url_is_validated_before_any_request() -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200)

    client = make_client(handler)
    try:
        with pytest.raises(UnsafeURLError):
            await safe_request(
                client,
                "GET",
                "http://metadata.test/latest",
                allow_private=True,
                resolver=fake_resolver,
            )
    finally:
        await client.aclose()

    assert calls == []


async def test_requires_client_with_redirects_disabled() -> None:
    client = make_client(lambda _request: httpx.Response(200), follow_redirects=True)
    try:
        with pytest.raises(ValueError, match="follow_redirects=False"):
            await safe_request(client, "GET", "http://public.test/x", allow_private=False)
    finally:
        await client.aclose()


async def test_rejects_follow_redirects_kwarg() -> None:
    client = make_client(lambda _request: httpx.Response(200))
    try:
        with pytest.raises(ValueError, match="follow_redirects=True"):
            await safe_request(
                client,
                "GET",
                "http://public.test/x",
                allow_private=False,
                follow_redirects=True,
            )
    finally:
        await client.aclose()


async def test_rejects_negative_max_redirects() -> None:
    client = make_client(lambda _request: httpx.Response(200))
    try:
        with pytest.raises(ValueError, match="max_redirects"):
            await safe_request(
                client,
                "GET",
                "http://public.test/x",
                allow_private=False,
                max_redirects=-1,
            )
    finally:
        await client.aclose()


async def test_non_redirect_response_is_returned_as_is() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text=f"missing {request.url.path}")

    client = make_client(handler)
    try:
        response = await safe_request(
            client,
            "get",
            "http://public.test/nope",
            allow_private=False,
            resolver=fake_resolver,
        )
    finally:
        await client.aclose()

    assert response.status_code == 404
    assert response.text == "missing /nope"


async def test_post_301_switches_to_get_and_drops_body() -> None:
    recorded: list[tuple[str, bytes]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        recorded.append((request.method, request.content))
        if request.method == "POST":
            return httpx.Response(301, headers={"location": "/moved"})
        return httpx.Response(200, text="moved")

    client = make_client(handler)
    try:
        response = await safe_request(
            client,
            "POST",
            "http://public.test/submit",
            allow_private=False,
            resolver=fake_resolver,
            data={"answer": "42"},
        )
    finally:
        await client.aclose()

    assert response.text == "moved"
    assert [method for method, _body in recorded] == ["POST", "GET"]
    assert recorded[1][1] == b""


async def test_head_request_keeps_method_on_302() -> None:
    recorded: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request.method)
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/final"})
        return httpx.Response(200)

    client = make_client(handler)
    try:
        await safe_request(
            client,
            "HEAD",
            "http://public.test/start",
            allow_private=False,
            resolver=fake_resolver,
        )
    finally:
        await client.aclose()

    assert recorded == ["HEAD", "HEAD"]
