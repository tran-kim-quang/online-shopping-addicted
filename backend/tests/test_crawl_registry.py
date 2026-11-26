"""Unit tests for ``PlatformAdapter`` lifecycle and ``AdapterRegistry``."""

from __future__ import annotations

import pytest

from app.core.singleton import SingletonMeta
from app.crawl.adapters.base import AdapterRegistry, PlatformAdapter
from app.crawl.adapters.tiki import TikiAdapter
from app.crawl.errors import UnsupportedPlatformError
from app.crawl.types import ParsedURL, ProductData
from tests.crawl_fakes import FakeAdapter


class IncompleteAdapter(PlatformAdapter):
    """Adapter implementing only part of the abstract interface."""

    platform = "incomplete"

    async def fetch_product(self, ref: ParsedURL) -> ProductData:  # pragma: no cover
        raise NotImplementedError


def test_platform_adapter_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        PlatformAdapter()  # type: ignore[abstract]


def test_incomplete_adapter_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        IncompleteAdapter()  # type: ignore[abstract]


async def test_adapter_lazy_http_is_owned_and_released() -> None:
    adapter = FakeAdapter()
    adapter._http = None
    adapter._owns_http = False

    http = adapter.http
    assert adapter._owns_http is True
    assert http.client.is_closed is False

    await adapter.aclose()
    assert http.client.is_closed is True
    assert adapter._http is None

    # Calling aclose twice is a no-op.
    await adapter.aclose()


def test_registry_is_a_singleton_with_tiki_registered() -> None:
    registry = AdapterRegistry()
    again = AdapterRegistry()

    assert registry is again
    assert isinstance(registry.get("tiki"), TikiAdapter)
    assert registry.has("tiki") is True
    assert registry.has("shopee") is False
    assert registry.platforms() == ["tiki"]


@pytest.mark.parametrize("name", ["tiki", "TIKI", "  Tiki  "])
def test_registry_lookup_is_case_and_space_insensitive(name: str) -> None:
    assert AdapterRegistry().get(name).platform == "tiki"


def test_registry_unknown_platform_raises_with_hint() -> None:
    with pytest.raises(UnsupportedPlatformError) as excinfo:
        AdapterRegistry().get("shopee")

    assert excinfo.value.platform == "shopee"
    assert "docs/research/hard-ecommerce-sites.md" in str(excinfo.value)


def test_registry_register_adds_and_replaces() -> None:
    registry = AdapterRegistry()
    first = FakeAdapter()
    first.platform = "example"
    registry.register(first)

    assert registry.get("example") is first
    assert "example" in registry.platforms()

    second = FakeAdapter()
    second.platform = "example"
    registry.register(second)

    assert registry.get("example") is second


@pytest.mark.parametrize("name", ["", "   "])
def test_registry_rejects_empty_platform(name: str) -> None:
    adapter = FakeAdapter()
    adapter.platform = name

    with pytest.raises(ValueError, match="must not be empty"):
        AdapterRegistry().register(adapter)


async def test_registry_aclose_all_closes_owned_clients() -> None:
    SingletonMeta.reset(AdapterRegistry)
    registry = AdapterRegistry()
    adapter = TikiAdapter()
    owned = adapter.http  # force creation of an owned client
    registry.register(adapter)

    await registry.aclose_all()

    assert owned.client.is_closed is True
    assert adapter._http is None


async def test_registry_aclose_all_skips_adapters_without_owned_http() -> None:
    registry = AdapterRegistry()
    fake = FakeAdapter()
    fake.platform = "fake"
    registry.register(fake)

    await registry.aclose_all()

    assert fake._http is None
    assert fake._owns_http is False
