"""DNS-pinning httpcore network backend and httpx transport against DNS rebinding.

How DNS pinning works here
--------------------------
1. ``PinnedNetworkBackend`` wraps an ``httpcore.AsyncNetworkBackend`` (defaulting
   to ``httpcore._backends.auto.AutoBackend``). In ``connect_tcp(host, port, ...)``,
   if ``host`` is an IP literal it is validated directly via
   :func:`app.security.url_validator.check_ip`. Otherwise, ``host`` is resolved
   **once** via the configured async ``resolver`` (defaulting to
   :func:`app.security.url_validator.default_resolver`), **every** returned IP is
   validated against SSRF rules (metadata IPs are always blocked; private/loopback/
   link-local/reserved IPs are blocked unless ``allow_private=True``), and the TCP
   socket is opened directly to the validated IP(s) in order without resolving
   ``host`` a second time.
2. TLS SNI and certificate hostname verification remain intact: verified against
   ``httpcore==1.0.9`` (``httpcore._async.connection.AsyncHTTPConnection._connect``),
   httpcore calls ``self._network_backend.connect_tcp(host=self._origin.host.decode("ascii"), ...)``
   and then upgrades the returned stream via ``stream.start_tls`` with
   ``server_hostname=sni_hostname or self._origin.host.decode("ascii")``.
   Because ``_origin.host`` is preserved as the original hostname, TLS SNI and
   certificate verification still check the original domain name even though the
   underlying TCP connection was established to the pinned IP literal.
3. ``SafeTransport(httpx.AsyncBaseTransport)`` is needed because ``httpx==0.28.1``'s
   ``AsyncHTTPTransport.__init__`` does not accept a ``network_backend`` argument.
   ``SafeTransport`` constructs ``httpcore.AsyncConnectionPool`` directly with
   ``network_backend=PinnedNetworkBackend(...)``, bridges ``httpx.Request`` /
   ``httpx.Response`` to and from ``httpcore``, and maps ``httpcore`` transport
   exceptions to ``httpx`` exceptions while letting :class:`UnsafeURLError`
   propagate directly.
"""

from __future__ import annotations

import contextlib
import ipaddress
import ssl
from collections.abc import AsyncIterator, Iterable, Iterator, Mapping
from types import TracebackType
from typing import Any

import httpcore
import httpx
from httpcore._backends.auto import AutoBackend
from httpcore._backends.base import SOCKET_OPTION

from app.security.url_validator import (
    DNSResolver,
    UnsafeURLError,
    check_ip,
    default_resolver,
)

_DEFAULT_LIMITS = httpx.Limits(
    max_connections=100,
    max_keepalive_connections=20,
    keepalive_expiry=5.0,
)

_HTTPCORE_EXC_MAP: dict[type[Exception], type[httpx.HTTPError]] = {
    httpcore.TimeoutException: httpx.TimeoutException,
    httpcore.ConnectTimeout: httpx.ConnectTimeout,
    httpcore.ReadTimeout: httpx.ReadTimeout,
    httpcore.WriteTimeout: httpx.WriteTimeout,
    httpcore.PoolTimeout: httpx.PoolTimeout,
    httpcore.NetworkError: httpx.NetworkError,
    httpcore.ConnectError: httpx.ConnectError,
    httpcore.ReadError: httpx.ReadError,
    httpcore.WriteError: httpx.WriteError,
    httpcore.ProxyError: httpx.ProxyError,
    httpcore.UnsupportedProtocol: httpx.UnsupportedProtocol,
    httpcore.ProtocolError: httpx.ProtocolError,
    httpcore.LocalProtocolError: httpx.LocalProtocolError,
    httpcore.RemoteProtocolError: httpx.RemoteProtocolError,
}


@contextlib.contextmanager
def _map_httpcore_exceptions() -> Iterator[None]:
    """Translate ``httpcore`` exceptions into ``httpx`` equivalents.

    ``UnsafeURLError`` (and any non-httpcore exception) propagates untouched so
    SSRF/pinning rejections are never masked as generic network errors.
    """
    try:
        yield
    except UnsafeURLError:
        raise
    except Exception as exc:
        mapped_exc: type[httpx.HTTPError] | None = None
        for from_exc, to_exc in _HTTPCORE_EXC_MAP.items():
            if not isinstance(exc, from_exc):
                continue
            if mapped_exc is None or issubclass(to_exc, mapped_exc):
                mapped_exc = to_exc
        if mapped_exc is None:
            raise
        raise mapped_exc(str(exc)) from exc


class _AsyncResponseStream(httpx.AsyncByteStream):
    """Wrap an httpcore async response stream as an ``httpx.AsyncByteStream``."""

    def __init__(self, httpcore_stream: Any) -> None:
        self._httpcore_stream = httpcore_stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        with _map_httpcore_exceptions():
            async for chunk in self._httpcore_stream:
                yield chunk

    async def aclose(self) -> None:
        if hasattr(self._httpcore_stream, "aclose"):
            await self._httpcore_stream.aclose()


class PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Async network backend that validates DNS once and pins the TCP target IP.

    TLS SNI and certificate verification continue to use the original hostname
    because ``httpcore==1.0.9`` passes ``origin.host`` to ``stream.start_tls``
    after ``connect_tcp`` returns.
    """

    def __init__(
        self,
        *,
        allow_private: bool = False,
        resolver: DNSResolver | None = None,
        backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._allow_private = allow_private
        self._resolver: DNSResolver = resolver if resolver is not None else default_resolver
        self._backend: httpcore.AsyncNetworkBackend = (
            backend if backend is not None else AutoBackend()
        )

    @property
    def allow_private(self) -> bool:
        return self._allow_private

    async def _resolve_and_validate(self, host: str) -> list[str]:
        cleaned = host.strip()
        if not cleaned:
            raise UnsafeURLError("Target host must not be empty.")

        ip_candidate = cleaned.strip("[]").split("%", 1)[0]
        try:
            literal_ip = ipaddress.ip_address(ip_candidate)
        except ValueError:
            literal_ip = None

        if literal_ip is not None:
            check_ip(literal_ip, allow_private=self._allow_private)
            return [str(literal_ip)]

        try:
            resolved_ips = await self._resolver(cleaned)
        except UnsafeURLError:
            raise
        except Exception as exc:
            raise UnsafeURLError(f"Failed to resolve host '{cleaned}': {exc}") from exc

        if not resolved_ips:
            raise UnsafeURLError(f"Host '{cleaned}' did not resolve to any IP address.")

        validated: list[str] = []
        seen: set[str] = set()
        for raw_ip in resolved_ips:
            clean_ip = str(raw_ip).strip().strip("[]").split("%", 1)[0]
            try:
                ip_obj = ipaddress.ip_address(clean_ip)
            except ValueError as exc:
                raise UnsafeURLError(f"Resolver returned invalid IP '{raw_ip}'.") from exc
            check_ip(ip_obj, allow_private=self._allow_private)
            normalized = str(ip_obj)
            if normalized not in seen:
                seen.add(normalized)
                validated.append(normalized)

        return validated

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        validated_ips = await self._resolve_and_validate(host)
        last_exc: Exception | None = None
        for ip_str in validated_ips:
            try:
                return await self._backend.connect_tcp(
                    ip_str,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as exc:
                last_exc = exc
                continue
        assert last_exc is not None
        raise last_exc

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise UnsafeURLError("Unix domain socket connections are not allowed.")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class SafeTransport(httpx.AsyncBaseTransport):
    """``httpx.AsyncBaseTransport`` backed by ``httpcore.AsyncConnectionPool`` with DNS pinning.

    ``httpx.AsyncHTTPTransport`` (0.28.x) does not expose a ``network_backend``
    parameter, so this transport instantiates ``httpcore.AsyncConnectionPool``
    directly with :class:`PinnedNetworkBackend` and adapts requests, responses,
    and transport exceptions following ``httpx._transports.default``.
    """

    def __init__(
        self,
        *,
        allow_private: bool = False,
        resolver: DNSResolver | None = None,
        backend: httpcore.AsyncNetworkBackend | None = None,
        verify: ssl.SSLContext | str | bool = True,
        http1: bool = True,
        http2: bool = False,
        limits: httpx.Limits | None = None,
        retries: int = 0,
    ) -> None:
        effective_limits = limits if limits is not None else _DEFAULT_LIMITS
        ssl_context = httpx.create_ssl_context(verify=verify, trust_env=False)
        self._network_backend = PinnedNetworkBackend(
            allow_private=allow_private,
            resolver=resolver,
            backend=backend,
        )
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context,
            max_connections=effective_limits.max_connections,
            max_keepalive_connections=effective_limits.max_keepalive_connections,
            keepalive_expiry=effective_limits.keepalive_expiry,
            http1=http1,
            http2=http2,
            retries=retries,
            network_backend=self._network_backend,
        )

    @property
    def network_backend(self) -> PinnedNetworkBackend:
        return self._network_backend

    @property
    def pool(self) -> httpcore.AsyncConnectionPool:
        return self._pool

    async def __aenter__(self) -> SafeTransport:
        await self._pool.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        with _map_httpcore_exceptions():
            await self._pool.__aexit__(exc_type, exc_value, traceback)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        assert isinstance(request.stream, httpx.AsyncByteStream)
        req = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        with _map_httpcore_exceptions():
            resp = await self._pool.handle_async_request(req)

        return httpx.Response(
            status_code=resp.status,
            headers=resp.headers,
            stream=_AsyncResponseStream(resp.stream),
            extensions=resp.extensions,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()


def make_safe_client(
    *,
    allow_private: bool,
    resolver: DNSResolver | None = None,
    timeout: float = 20.0,
    headers: Mapping[str, str] | None = None,
    backend: httpcore.AsyncNetworkBackend | None = None,
    verify: ssl.SSLContext | str | bool = True,
    limits: httpx.Limits | None = None,
) -> httpx.AsyncClient:
    """Create an ``httpx.AsyncClient`` hardened against SSRF and DNS rebinding.

    - ``follow_redirects=False`` (callers use :func:`safe_request` for hops)
    - ``trust_env=False`` (never route through system proxies that resolve DNS)
    - ``transport=SafeTransport(...)`` (pins the validated IP per connection)
    """
    transport = SafeTransport(
        allow_private=allow_private,
        resolver=resolver,
        backend=backend,
        verify=verify,
        limits=limits,
    )
    return httpx.AsyncClient(
        transport=transport,
        follow_redirects=False,
        trust_env=False,
        timeout=httpx.Timeout(timeout),
        headers=dict(headers) if headers is not None else None,
    )
