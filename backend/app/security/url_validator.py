"""Outbound URL validator protecting against SSRF."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Sequence
from urllib.parse import urlsplit

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
DNSResolver = Callable[[str], Awaitable[Sequence[str]]]

_ALLOWED_SCHEMES = frozenset({"http", "https"})

# Cloud instance metadata endpoints — always blocked even when allow_private=True.
_ALWAYS_BLOCKED_IPS: frozenset[IPAddress] = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("fd00:ec2::254"),
    }
)


class UnsafeURLError(ValueError):
    """Raised when an outbound URL fails scheme, authority, or IP safety checks."""


async def default_resolver(host: str) -> list[str]:
    """Resolve ``host`` asynchronously via the event loop's ``getaddrinfo``."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise UnsafeURLError(f"Failed to resolve host '{host}': {exc}") from exc

    ips: list[str] = []
    seen: set[str] = set()
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        ip_str = str(sockaddr[0])
        # Strip IPv6 zone IDs (e.g. fe80::1%eth0) if present.
        if "%" in ip_str:
            ip_str = ip_str.split("%", 1)[0]
        if ip_str not in seen:
            seen.add(ip_str)
            ips.append(ip_str)
    return ips


def _is_non_public(ip: IPAddress) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _check_ip(ip: IPAddress, *, allow_private: bool) -> None:
    candidates: list[IPAddress] = [ip]
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        candidates.append(ip.ipv4_mapped)

    for candidate in candidates:
        if candidate in _ALWAYS_BLOCKED_IPS:
            raise UnsafeURLError(f"Access to metadata IP '{candidate}' is forbidden.")
        if not allow_private and _is_non_public(candidate):
            raise UnsafeURLError(f"Access to non-public IP '{candidate}' is forbidden.")


async def validate_outbound_url(
    url: str,
    *,
    allow_private: bool,
    resolver: DNSResolver | None = None,
) -> str:
    """Validate an outbound HTTP(S) URL against SSRF rules.

    Args:
        url: The candidate URL.
        allow_private: Whether private/loopback/link-local IPs are allowed
            (metadata IPs are always blocked regardless of this flag).
        resolver: Optional async callable ``(host) -> Sequence[str]`` returning
            resolved IP strings, injectable for offline unit tests.

    Returns:
        The stripped URL string if valid and safe.

    Raises:
        UnsafeURLError: If the URL has an unsupported scheme, userinfo, missing
            host, unresolvable host, or resolves to a disallowed IP.
    """
    cleaned = url.strip()
    if not cleaned:
        raise UnsafeURLError("URL must not be empty.")

    try:
        parsed = urlsplit(cleaned)
    except ValueError as exc:
        raise UnsafeURLError(f"Malformed URL: {exc}") from exc

    scheme = parsed.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise UnsafeURLError(f"Unsupported URL scheme '{parsed.scheme}'. Only http/https allowed.")

    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise UnsafeURLError("URLs containing userinfo credentials are not allowed.")

    hostname = parsed.hostname
    if not hostname:
        raise UnsafeURLError("URL must include a hostname.")

    try:
        _ = parsed.port
    except ValueError as exc:
        raise UnsafeURLError(f"Invalid port in URL: {exc}") from exc

    # Direct IP literal check (urlsplit already strips brackets from IPv6 literals).
    ip_host = hostname.split("%", 1)[0]
    try:
        literal_ip = ipaddress.ip_address(ip_host)
    except ValueError:
        literal_ip = None

    if literal_ip is not None:
        _check_ip(literal_ip, allow_private=allow_private)
        return cleaned

    resolve_fn = resolver if resolver is not None else default_resolver
    try:
        resolved_ips = await resolve_fn(hostname)
    except UnsafeURLError:
        raise
    except Exception as exc:
        raise UnsafeURLError(f"Failed to resolve host '{hostname}': {exc}") from exc

    if not resolved_ips:
        raise UnsafeURLError(f"Host '{hostname}' did not resolve to any IP address.")

    for raw_ip in resolved_ips:
        clean_ip = raw_ip.split("%", 1)[0]
        try:
            ip_obj = ipaddress.ip_address(clean_ip)
        except ValueError as exc:
            raise UnsafeURLError(f"Resolver returned invalid IP '{raw_ip}'.") from exc
        _check_ip(ip_obj, allow_private=allow_private)

    return cleaned
