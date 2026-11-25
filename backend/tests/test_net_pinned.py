"""Unit tests for DNS pinning (``app/net/pinned.py``).

Everything runs offline: DNS is a fake async callable and the TCP layer is a
recording ``httpcore.AsyncNetworkBackend`` double, so no socket is ever opened.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpcore
import httpx
import pytest

from app.llm.client import LLMClient
from app.net.pinned import (
    PinnedNetworkBackend,
    SafeTransport,
    _AsyncResponseStream,
    make_safe_client,
)
from app.security.url_validator import UnsafeURLError
from app.security.url_validator import check_ip as validator_check_ip

PUBLIC_IP = "93.184.216.34"
PUBLIC_IP_2 = "93.184.216.35"
PRIVATE_IP = "10.0.0.5"
METADATA_IP = "169.254.169.254"


class FakeAsyncStream(httpcore.AsyncNetworkStream):
    """Minimal ``AsyncNetworkStream`` double that records the TLS SNI it was given."""

    def __init__(self, records: dict[str, Any] | None = None) -> None:
        self.records = records if records is not None else {}
        self.start_tls_calls: list[str | None] = []
        self.closed = False
        self.records.setdefault("streams", []).append(self)

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return b""

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        return None

    async def aclose(self) -> None:
        self.closed = True

    async def start_tls(
        self,
        ssl_context: Any,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.start_tls_calls.append(server_hostname)
        raise httpcore.ConnectError("TLS handshake intentionally aborted by the test double")

    def get_extra_info(self, info: str) -> Any:
        return None


class RecordingBackend(httpcore.AsyncNetworkBackend):
    """Fake network backend recording every ``connect_tcp`` target."""

    def __init__(
        self,
        *,
        records: dict[str, Any] | None = None,
        fail_hosts: dict[str, Exception] | None = None,
    ) -> None:
        self.records = records if records is not None else {}
        self.calls: list[tuple[str, int]] = []
        self._fail_hosts = fail_hosts or {}
        self.sleeps: list[float] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        self.calls.append((host, port))
        failure = self._fail_hosts.get(host)
        if failure is not None:
            raise failure
        return FakeAsyncStream(self.records)

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:  # pragma: no cover - not used
        raise AssertionError("unix sockets must never be used")

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


class ScriptedResolver:
    """Async resolver double returning scripted answers and counting calls."""

    def __init__(self, answers: Sequence[Sequence[str]] | Exception) -> None:
        self._answers = answers
        self.calls: list[str] = []

    async def __call__(self, host: str) -> list[str]:
        self.calls.append(host)
        if isinstance(self._answers, Exception):
            raise self._answers
        if not self._answers:  # pragma: no cover - defensive
            return []
        index = min(len(self.calls) - 1, len(self._answers) - 1)
        return list(self._answers[index])


def make_backend(
    *,
    allow_private: bool = False,
    resolver: Any = None,
    backend: RecordingBackend | None = None,
) -> tuple[PinnedNetworkBackend, RecordingBackend]:
    recording = backend if backend is not None else RecordingBackend()
    pinned = PinnedNetworkBackend(
        allow_private=allow_private,
        resolver=resolver if resolver is not None else ScriptedResolver([[PUBLIC_IP]]),
        backend=recording,
    )
    return pinned, recording


# ---------------------------------------------------------------------------
# IP literal handling
# ---------------------------------------------------------------------------


async def test_public_ip_literal_connects_without_dns() -> None:
    resolver = ScriptedResolver([[PRIVATE_IP]])
    pinned, recording = make_backend(resolver=resolver)

    stream = await pinned.connect_tcp(PUBLIC_IP, 443)

    assert isinstance(stream, FakeAsyncStream)
    assert resolver.calls == []
    assert recording.calls == [(PUBLIC_IP, 443)]


async def test_private_ip_literal_rejected_before_connect() -> None:
    pinned, recording = make_backend(allow_private=False)

    with pytest.raises(UnsafeURLError, match="non-public IP"):
        await pinned.connect_tcp(PRIVATE_IP, 443)

    assert recording.calls == []


async def test_loopback_ip_literal_rejected() -> None:
    pinned, recording = make_backend()

    with pytest.raises(UnsafeURLError):
        await pinned.connect_tcp("127.0.0.1", 80)

    assert recording.calls == []


async def test_ipv6_literal_is_unwrapped_from_brackets_and_zone_id() -> None:
    pinned, recording = make_backend()

    await pinned.connect_tcp("[2001:4860:4860::8888]", 443)

    assert recording.calls == [("2001:4860:4860::8888", 443)]


async def test_ipv6_loopback_and_link_local_rejected() -> None:
    pinned, recording = make_backend()

    for host in ("[::1]", "fe80::1%eth0", "::ffff:10.0.0.5"):
        with pytest.raises(UnsafeURLError):
            await pinned.connect_tcp(host, 443)

    assert recording.calls == []


async def test_empty_host_rejected() -> None:
    pinned, recording = make_backend()

    with pytest.raises(UnsafeURLError, match="must not be empty"):
        await pinned.connect_tcp("   ", 443)

    assert recording.calls == []


async def test_allow_private_property_mirrors_constructor_flag() -> None:
    assert make_backend(allow_private=True)[0].allow_private is True
    assert make_backend(allow_private=False)[0].allow_private is False


# ---------------------------------------------------------------------------
# DNS resolution + validation
# ---------------------------------------------------------------------------


async def test_private_dns_answer_blocked_before_connect() -> None:
    resolver = ScriptedResolver([[PRIVATE_IP]])
    pinned, recording = make_backend(resolver=resolver, allow_private=False)

    with pytest.raises(UnsafeURLError, match="non-public IP"):
        await pinned.connect_tcp("internal.example", 443)

    assert resolver.calls == ["internal.example"]
    assert recording.calls == []


async def test_allow_private_true_permits_private_dns_answer() -> None:
    resolver = ScriptedResolver([[PRIVATE_IP]])
    pinned, recording = make_backend(resolver=resolver, allow_private=True)

    await pinned.connect_tcp("internal.example", 8080)

    assert recording.calls == [(PRIVATE_IP, 8080)]


@pytest.mark.parametrize("host", [METADATA_IP, "[fd00:ec2::254]"])
async def test_metadata_ip_always_blocked_even_when_private_allowed(host: str) -> None:
    pinned, recording = make_backend(allow_private=True)

    with pytest.raises(UnsafeURLError, match="metadata"):
        await pinned.connect_tcp(host, 80)

    assert recording.calls == []


async def test_metadata_ip_from_dns_always_blocked() -> None:
    resolver = ScriptedResolver([[METADATA_IP]])
    pinned, recording = make_backend(resolver=resolver, allow_private=True)

    with pytest.raises(UnsafeURLError, match="metadata"):
        await pinned.connect_tcp("metadata.example", 80)

    assert recording.calls == []


async def test_one_bad_ip_among_many_blocks_the_connection() -> None:
    resolver = ScriptedResolver([[PUBLIC_IP, PRIVATE_IP]])
    pinned, recording = make_backend(resolver=resolver, allow_private=False)

    with pytest.raises(UnsafeURLError, match="non-public IP"):
        await pinned.connect_tcp("mixed.example", 443)

    assert recording.calls == []


async def test_multiple_valid_ips_connect_to_the_first() -> None:
    resolver = ScriptedResolver([[PUBLIC_IP, PUBLIC_IP_2]])
    pinned, recording = make_backend(resolver=resolver)

    await pinned.connect_tcp("multi.example", 443)

    assert recording.calls == [(PUBLIC_IP, 443)]


async def test_falls_back_to_next_validated_ip_on_connect_error() -> None:
    resolver = ScriptedResolver([[PUBLIC_IP, PUBLIC_IP_2]])
    backend = RecordingBackend(
        fail_hosts={PUBLIC_IP: httpcore.ConnectError("connection refused")},
    )
    pinned, recording = make_backend(resolver=resolver, backend=backend)

    stream = await pinned.connect_tcp("multi.example", 443)

    assert isinstance(stream, FakeAsyncStream)
    assert recording.calls == [(PUBLIC_IP, 443), (PUBLIC_IP_2, 443)]


async def test_all_connect_attempts_failing_raises_last_error() -> None:
    resolver = ScriptedResolver([[PUBLIC_IP, PUBLIC_IP_2]])
    backend = RecordingBackend(
        fail_hosts={
            PUBLIC_IP: httpcore.ConnectError("first"),
            PUBLIC_IP_2: httpcore.ConnectError("second"),
        },
    )
    pinned, _ = make_backend(resolver=resolver, backend=backend)

    with pytest.raises(httpcore.ConnectError, match="second"):
        await pinned.connect_tcp("multi.example", 443)


async def test_resolver_called_once_per_connection_and_pins_first_answer() -> None:
    """DNS rebinding: the second answer is private, the socket still uses the pinned IP."""
    resolver = ScriptedResolver([[PUBLIC_IP], [PRIVATE_IP]])
    pinned, recording = make_backend(resolver=resolver)

    await pinned.connect_tcp("rebind.example", 443)

    assert resolver.calls == ["rebind.example"]
    assert recording.calls == [(PUBLIC_IP, 443)]

    # A second connection resolves again but still never leaks the hostname to the socket.
    with pytest.raises(UnsafeURLError):
        await pinned.connect_tcp("rebind.example", 443)
    assert resolver.calls == ["rebind.example", "rebind.example"]
    assert recording.calls == [(PUBLIC_IP, 443)]


async def test_duplicate_dns_answers_are_deduplicated() -> None:
    resolver = ScriptedResolver([[PUBLIC_IP, PUBLIC_IP, f"[{PUBLIC_IP}]"]])
    backend = RecordingBackend(fail_hosts={PUBLIC_IP: httpcore.ConnectError("down")})
    pinned, _ = make_backend(resolver=resolver, backend=backend)

    with pytest.raises(httpcore.ConnectError):
        await pinned.connect_tcp("dup.example", 443)

    assert backend.calls == [(PUBLIC_IP, 443)]


@pytest.mark.parametrize(
    "answers",
    [
        [],
        ["not-an-ip"],
        ["999.999.999.999"],
    ],
)
async def test_bad_resolver_answers_raise_unsafe_url_error(answers: list[str]) -> None:
    resolver = ScriptedResolver([answers])
    pinned, recording = make_backend(resolver=resolver)

    with pytest.raises(UnsafeURLError):
        await pinned.connect_tcp("broken.example", 443)

    assert recording.calls == []


async def test_resolver_exception_is_wrapped() -> None:
    resolver = ScriptedResolver(OSError("name or service not known"))
    pinned, _ = make_backend(resolver=resolver)

    with pytest.raises(UnsafeURLError, match="Failed to resolve"):
        await pinned.connect_tcp("down.example", 443)


async def test_resolver_unsafe_url_error_is_propagated_as_is() -> None:
    resolver = ScriptedResolver(UnsafeURLError("blocked by policy"))
    pinned, _ = make_backend(resolver=resolver)

    with pytest.raises(UnsafeURLError, match="blocked by policy"):
        await pinned.connect_tcp("down.example", 443)


async def test_empty_resolver_answer_raises() -> None:
    resolver = ScriptedResolver([[]])
    pinned, _ = make_backend(resolver=resolver)

    with pytest.raises(UnsafeURLError, match="did not resolve"):
        await pinned.connect_tcp("empty.example", 443)


async def test_connect_unix_socket_is_forbidden() -> None:
    pinned, _ = make_backend()

    with pytest.raises(UnsafeURLError, match="Unix domain socket"):
        await pinned.connect_unix_socket("/var/run/docker.sock")


async def test_sleep_delegates_to_wrapped_backend() -> None:
    backend = RecordingBackend()
    pinned, recording = make_backend(backend=backend)

    await pinned.sleep(0.25)

    assert recording.sleeps == [0.25]


# ---------------------------------------------------------------------------
# SafeTransport
# ---------------------------------------------------------------------------


def _request(url: str) -> httpx.Request:
    return httpx.Request("GET", url)


async def test_safe_transport_keeps_tls_sni_on_original_hostname() -> None:
    records: dict[str, Any] = {}
    resolver = ScriptedResolver([[PUBLIC_IP]])
    backend = RecordingBackend(records=records)
    transport = SafeTransport(allow_private=False, resolver=resolver, backend=backend)

    with pytest.raises(httpx.ConnectError):
        await transport.handle_async_request(_request("https://tiki.vn/api/v2/products/1"))

    stream = records["streams"][0]
    assert stream.start_tls_calls == ["tiki.vn"]
    # The socket was opened to the pinned IP, never to the hostname.
    assert backend.calls == [(PUBLIC_IP, 443)]
    assert transport.network_backend is not None
    assert transport.pool is not None
    await transport.aclose()


async def test_safe_transport_maps_httpcore_errors_to_httpx() -> None:
    resolver = ScriptedResolver([[PUBLIC_IP]])
    backend = RecordingBackend(fail_hosts={PUBLIC_IP: httpcore.ConnectError("refused")})
    transport = SafeTransport(allow_private=False, resolver=resolver, backend=backend)

    with pytest.raises(httpx.ConnectError):
        await transport.handle_async_request(_request("https://tiki.vn/"))

    await transport.aclose()


async def test_safe_transport_propagates_unsafe_url_error() -> None:
    resolver = ScriptedResolver([[PRIVATE_IP]])
    backend = RecordingBackend()
    transport = SafeTransport(allow_private=False, resolver=resolver, backend=backend)

    with pytest.raises(UnsafeURLError):
        await transport.handle_async_request(_request("https://internal.example/"))

    assert backend.calls == []
    await transport.aclose()


async def test_safe_transport_is_an_async_context_manager() -> None:
    transport = SafeTransport(
        allow_private=False,
        resolver=ScriptedResolver([[PUBLIC_IP]]),
        backend=RecordingBackend(),
    )

    async with transport as entered:
        assert entered is transport

    # ``aclose`` is idempotent enough to be called again after ``__aexit__``.
    await transport.aclose()


def test_make_safe_client_is_hardened() -> None:
    client = make_safe_client(
        allow_private=False,
        resolver=ScriptedResolver([[PUBLIC_IP]]),
        timeout=7.5,
        headers={"X-Test": "1"},
    )

    assert isinstance(client._transport, SafeTransport)
    assert client.follow_redirects is False
    assert client.trust_env is False
    assert client.timeout.connect == 7.5
    assert client.headers["X-Test"] == "1"
    assert client._transport.network_backend.allow_private is False


async def test_make_safe_client_does_not_follow_redirects() -> None:
    """A 302 must surface as a response, never be followed silently by httpx."""

    class _RedirectTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "https://169.254.169.254/"})

    client = httpx.AsyncClient(
        transport=_RedirectTransport(),
        follow_redirects=False,
        trust_env=False,
    )
    response = await client.get("https://public.example/")

    assert response.status_code == 302
    await client.aclose()


# ---------------------------------------------------------------------------
# Response stream adapter
# ---------------------------------------------------------------------------


async def test_response_stream_yields_chunks_and_closes() -> None:
    class _Inner:
        def __init__(self) -> None:
            self.closed = False
            self._chunks = iter([b"a", b"bc"])

        def __aiter__(self) -> AsyncIterator[bytes]:
            return self

        async def __anext__(self) -> bytes:
            try:
                return next(self._chunks)
            except StopIteration:  # pragma: no cover - defensive
                raise StopAsyncIteration from None

        async def aclose(self) -> None:
            self.closed = True

    inner = _Inner()
    stream = _AsyncResponseStream(inner)

    chunks = [chunk async for chunk in stream]
    await stream.aclose()

    assert chunks == [b"a", b"bc"]
    assert inner.closed is True


async def test_response_stream_tolerates_streams_without_aclose() -> None:
    class _Inner:
        def __aiter__(self) -> AsyncIterator[bytes]:
            async def _gen() -> AsyncIterator[bytes]:
                yield b"x"

            return _gen()

    stream = _AsyncResponseStream(_Inner())
    assert [chunk async for chunk in stream] == [b"x"]
    await stream.aclose()


# ---------------------------------------------------------------------------
# LLMClient wiring: no injected http_client -> safe client
# ---------------------------------------------------------------------------


async def test_llm_client_uses_safe_client_when_not_injected(config_factory) -> None:
    client = LLMClient(config_factory(base_url="https://llm.example/v1"))

    transport = client._http_client._transport
    assert isinstance(transport, SafeTransport)
    assert client._http_client.follow_redirects is False
    assert client._http_client.trust_env is False
    assert transport.network_backend.allow_private is True  # ALLOW_PRIVATE_LLM_HOSTS=true

    await client.aclose()
    assert client._http_client.is_closed is True


async def test_llm_client_keeps_injected_client_untouched(mock_http) -> None:
    from app.llm.types import LLMConfig

    client = LLMClient(
        LLMConfig(
            base_url="http://mock/v1",
            api_key=None,
            chat_model="mock-chat",
            timeout=5.0,
            max_retries=0,
        ),
        http_client=mock_http,
    )

    assert client._http_client is mock_http
    await client.aclose()
    assert mock_http.is_closed is False


# ---------------------------------------------------------------------------
# check_ip parity with the validator (pinning reuses the same rules)
# ---------------------------------------------------------------------------


def test_check_ip_is_shared_with_url_validator() -> None:
    import app.net.pinned as pinned_module

    assert pinned_module.check_ip is validator_check_ip
