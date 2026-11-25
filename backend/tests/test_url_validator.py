"""Unit tests for the SSRF URL validator (DNS is always faked)."""

from __future__ import annotations

import pytest

from app.security.url_validator import UnsafeURLError, validate_outbound_url


def _resolver(*ips: str):
    async def _resolve(_host: str) -> list[str]:
        return list(ips)

    return _resolve


async def _exploding_resolver(_host: str) -> list[str]:
    raise OSError("dns exploded")


# ---------------------------------------------------------------------------
# Scheme / authority
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/file",
        "file:///etc/passwd",
        "gopher://example.com",
        "javascript:alert(1)",
        "example.com/v1",
    ],
)
async def test_rejects_unsupported_schemes(url: str) -> None:
    with pytest.raises(UnsafeURLError):
        await validate_outbound_url(url, allow_private=False, resolver=_resolver("93.184.216.34"))


@pytest.mark.parametrize(
    "url",
    [
        "http://user:pass@example.com/v1",
        "http://user@example.com/v1",
        "https://token@api.openai.com/v1",
    ],
)
async def test_rejects_userinfo(url: str) -> None:
    with pytest.raises(UnsafeURLError, match="userinfo"):
        await validate_outbound_url(url, allow_private=False, resolver=_resolver("93.184.216.34"))


@pytest.mark.parametrize("url", ["http:///v1", "https:///v1", ""])
async def test_rejects_missing_host(url: str) -> None:
    with pytest.raises(UnsafeURLError):
        await validate_outbound_url(url, allow_private=False, resolver=_resolver("93.184.216.34"))


async def test_rejects_invalid_port() -> None:
    with pytest.raises(UnsafeURLError, match="port"):
        await validate_outbound_url(
            "http://example.com:99999/v1",
            allow_private=False,
            resolver=_resolver("93.184.216.34"),
        )


# ---------------------------------------------------------------------------
# IP literal checks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.5/v1",
        "http://192.168.1.10/v1",
        "http://172.16.0.1/v1",
        "http://127.0.0.1/v1",
        "http://127.1.2.3/v1",
        "http://169.254.1.1/v1",
        "http://0.0.0.0/v1",
        "http://[::1]/v1",
        "http://[fe80::1]/v1",
        "http://[fc00::1]/v1",
        "http://[::ffff:10.0.0.1]/v1",
    ],
)
async def test_rejects_non_public_ip_literals(url: str) -> None:
    with pytest.raises(UnsafeURLError, match="non-public"):
        await validate_outbound_url(url, allow_private=False, resolver=_resolver("93.184.216.34"))


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data",
        "http://[fd00:ec2::254]/latest/meta-data",
        "http://[::ffff:169.254.169.254]/latest/meta-data",
    ],
)
async def test_metadata_ips_always_blocked(url: str) -> None:
    with pytest.raises(UnsafeURLError, match="metadata"):
        await validate_outbound_url(url, allow_private=True, resolver=_resolver("93.184.216.34"))


async def test_allows_public_ip_literal() -> None:
    assert (
        await validate_outbound_url("http://93.184.216.34/v1", allow_private=False)
        == "http://93.184.216.34/v1"
    )


# ---------------------------------------------------------------------------
# DNS resolution checks
# ---------------------------------------------------------------------------


async def test_rejects_private_host_resolution() -> None:
    with pytest.raises(UnsafeURLError, match="non-public"):
        await validate_outbound_url(
            "https://internal.example.com/v1",
            allow_private=False,
            resolver=_resolver("10.1.2.3"),
        )


async def test_rejects_if_any_resolved_ip_is_private() -> None:
    with pytest.raises(UnsafeURLError, match="non-public"):
        await validate_outbound_url(
            "https://mixed.example.com/v1",
            allow_private=False,
            resolver=_resolver("93.184.216.34", "192.168.0.9", "2606:4700::1111"),
        )


async def test_allows_private_resolution_when_enabled() -> None:
    url = await validate_outbound_url(
        "http://localhost:11434/v1",
        allow_private=True,
        resolver=_resolver("127.0.0.1"),
    )

    assert url == "http://localhost:11434/v1"


async def test_allows_public_resolution() -> None:
    url = await validate_outbound_url(
        "https://api.openai.com/v1",
        allow_private=False,
        resolver=_resolver("93.184.216.34", "2606:4700::1111"),
    )

    assert url == "https://api.openai.com/v1"


async def test_rejects_unresolvable_host() -> None:
    with pytest.raises(UnsafeURLError, match="resolve"):
        await validate_outbound_url(
            "https://nope.example.com/v1",
            allow_private=False,
            resolver=_exploding_resolver,
        )


async def test_rejects_empty_resolution() -> None:
    with pytest.raises(UnsafeURLError, match="did not resolve"):
        await validate_outbound_url(
            "https://empty.example.com/v1",
            allow_private=False,
            resolver=_resolver(),
        )


async def test_rejects_garbage_resolver_output() -> None:
    with pytest.raises(UnsafeURLError, match="invalid IP"):
        await validate_outbound_url(
            "https://garbage.example.com/v1",
            allow_private=False,
            resolver=_resolver("not-an-ip"),
        )


async def test_strips_ipv6_zone_id_from_resolution() -> None:
    with pytest.raises(UnsafeURLError, match="non-public"):
        await validate_outbound_url(
            "https://zoned.example.com/v1",
            allow_private=False,
            resolver=_resolver("fe80::1%eth0"),
        )


async def test_rejects_malformed_ipv6_url() -> None:
    with pytest.raises(UnsafeURLError, match="Malformed URL"):
        await validate_outbound_url(
            "http://[::1/v1", allow_private=False, resolver=_resolver("93.184.216.34")
        )


async def test_resolver_unsafe_error_propagates() -> None:
    async def _resolver_raises(_host: str) -> list[str]:
        raise UnsafeURLError("blocked upstream")

    with pytest.raises(UnsafeURLError, match="blocked upstream"):
        await validate_outbound_url(
            "https://example.com/v1", allow_private=False, resolver=_resolver_raises
        )


# ---------------------------------------------------------------------------
# default_resolver (socket layer faked: still no network access)
# ---------------------------------------------------------------------------


async def test_default_resolver_parses_and_deduplicates(monkeypatch) -> None:
    import socket

    from app.security.url_validator import default_resolver

    def _fake_getaddrinfo(host, port, *args, **kwargs):
        assert host == "example.com"
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700::1111", 0, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)

    assert await default_resolver("example.com") == ["93.184.216.34", "2606:4700::1111"]


async def test_default_resolver_strips_zone_id(monkeypatch) -> None:
    import socket

    from app.security.url_validator import default_resolver

    def _fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fe80::1%eth0", 0, 0, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)

    assert await default_resolver("zoned.example.com") == ["fe80::1"]


async def test_default_resolver_wraps_oserror(monkeypatch) -> None:
    import socket

    from app.security.url_validator import default_resolver

    def _fake_getaddrinfo(host, port, *args, **kwargs):
        raise socket.gaierror("name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)

    with pytest.raises(UnsafeURLError, match="Failed to resolve"):
        await default_resolver("nope.example.com")


async def test_returns_trimmed_url() -> None:
    assert (
        await validate_outbound_url(
            "  https://api.openai.com/v1  ",
            allow_private=False,
            resolver=_resolver("93.184.216.34"),
        )
        == "https://api.openai.com/v1"
    )
