"""Platform plugin core (A1): auto-discovery, registry lookups, /api/platforms, "no Tiki in core".

The auto-discovery test writes a brand-new plugin module into the discovery path and proves the
core classifies it without a single change to classifier/registry/API/frontend code.
"""

from __future__ import annotations

import importlib
import re
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.crawl.classifier import classify_url
from app.crawl.errors import UnsupportedPlatformError, UnsupportedURLError
from app.main import create_app
from app.platforms import plugins as plugins_pkg
from app.platforms.base import PlatformFetcher, PlatformPlugin
from app.platforms.known import KNOWN_UNSUPPORTED_PLATFORMS, match_known_unsupported
from app.platforms.registry import PlatformRegistry

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent

FAKE_PLUGIN_SOURCE = '''
"""Test-only platform plugin injected into the discovery path."""

from __future__ import annotations

from urllib.parse import SplitResult

from app.crawl.errors import UnsupportedURLError
from app.crawl.types import ParsedURL
from app.platforms.base import PlatformFetcher, PlatformPlugin


class FakeFetcher(PlatformFetcher):
    async def fetch_product(self, ref):
        raise NotImplementedError

    async def fetch_reviews(self, ref, *, limit=200):
        return []

    async def fetch_shop(self, ref):
        raise NotImplementedError

    async def list_shop_products(self, ref, *, limit=100):
        return []

    async def aclose(self) -> None:
        return None


class FakeMarketPlugin(PlatformPlugin):
    name = "fake_market"
    display_name = "Fake Market"
    hosts = frozenset({"fake-market.test"})
    short_hosts = frozenset({"fk.test"})
    currency = "USD"
    requires_provider = None
    client_patterns = [r"^/p/\\\\d+$"]

    def parse_url(self, url: SplitResult) -> ParsedURL:
        path = url.path or "/"
        if not path.startswith("/p/"):
            raise UnsupportedURLError("Đường dẫn Fake Market chưa được hỗ trợ.")
        pid = path.removeprefix("/p/").strip("/")
        return ParsedURL(
            platform=self.name,
            kind="product",
            product_id=pid,
            canonical_url=f"https://fake-market.test/p/{pid}",
        )

    def create_fetcher(self, provider_profile=None, **kwargs):
        return FakeFetcher()


PLUGIN = FakeMarketPlugin()
'''


@pytest.fixture
def fake_plugin_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Path] | Any:
    """Add ``tmp_path`` to the plugin discovery path and import the fake plugin."""
    (tmp_path / "fake_market.py").write_text(FAKE_PLUGIN_SOURCE, encoding="utf-8")
    monkeypatch.setattr(plugins_pkg, "__path__", [*plugins_pkg.__path__, str(tmp_path)])
    sys.modules.pop("app.platforms.plugins.fake_market", None)
    yield tmp_path
    sys.modules.pop("app.platforms.plugins.fake_market", None)


# ---------------------------------------------------------------------------
# Auto-discovery
# ---------------------------------------------------------------------------


def test_registry_discovers_builtin_plugins_without_a_hardcoded_list() -> None:
    names = [p.name for p in PlatformRegistry().all()]

    assert names == ["shopee", "tiki"]  # sorted by name


def test_a_new_plugin_is_classified_without_touching_core_code(fake_plugin_dir: Path) -> None:
    registry = PlatformRegistry()

    assert [p.name for p in registry.all()] == ["fake_market", "shopee", "tiki"]
    assert registry.for_host("fake-market.test").name == "fake_market"
    assert registry.for_short_host("fk.test").name == "fake_market"

    parsed = classify_url("https://fake-market.test/p/42")
    assert parsed.platform == "fake_market"
    assert parsed.product_id == "42"
    assert parsed.canonical_url == "https://fake-market.test/p/42"


def test_fake_plugin_exposes_a_fetcher(fake_plugin_dir: Path) -> None:
    fetcher = PlatformRegistry().get("fake_market").create_fetcher()

    assert isinstance(fetcher, PlatformFetcher)


def test_registry_is_a_singleton_and_resets_between_tests() -> None:
    assert PlatformRegistry() is PlatformRegistry()


# ---------------------------------------------------------------------------
# Registry lookups
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["tiki", "TIKI", "  Tiki  "])
def test_get_is_case_and_space_insensitive(name: str) -> None:
    assert PlatformRegistry().get(name).name == "tiki"


def test_get_unknown_platform_raises_without_docs_hints() -> None:
    with pytest.raises(UnsupportedPlatformError) as excinfo:
        PlatformRegistry().get("lazada")

    assert excinfo.value.platform == "lazada"
    assert "chưa được hỗ trợ" in str(excinfo.value)
    assert "docs/" not in str(excinfo.value)


def test_for_host_handles_primary_short_and_unknown_hosts() -> None:
    registry = PlatformRegistry()

    assert registry.for_host("tiki.vn").name == "tiki"
    assert registry.for_host("www.tiki.vn").name == "tiki"
    assert registry.for_host("ti.ki").name == "tiki"
    assert registry.for_host("shopee.vn").name == "shopee"
    assert registry.for_host("s.shopee.vn").name == "shopee"
    assert registry.for_host("example.com") is None
    assert registry.for_host("") is None


def test_register_replaces_a_plugin() -> None:
    registry = PlatformRegistry()
    original = registry.get("tiki")

    class Replacement(PlatformPlugin):
        name = "tiki"
        display_name = "Tiki (test)"
        hosts = frozenset({"tiki.vn"})

        def parse_url(self, url):  # pragma: no cover - never called
            raise NotImplementedError

        def create_fetcher(self, provider_profile=None, **kwargs):  # pragma: no cover
            raise NotImplementedError

    registry.register(Replacement())
    assert registry.get("tiki").display_name == "Tiki (test)"
    registry.register(original)


def test_empty_plugin_name_is_rejected() -> None:
    class Nameless(PlatformPlugin):
        name = "  "
        display_name = "x"
        hosts = frozenset()

        def parse_url(self, url):  # pragma: no cover
            raise NotImplementedError

        def create_fetcher(self, provider_profile=None, **kwargs):  # pragma: no cover
            raise NotImplementedError

    with pytest.raises(ValueError, match="must not be empty"):
        PlatformRegistry().register(Nameless())


# ---------------------------------------------------------------------------
# known.py — recognized but unsupported
# ---------------------------------------------------------------------------


def test_known_unsupported_platforms_keep_their_reasons_and_exclude_shopee() -> None:
    names = [entry.name for entry in PlatformRegistry().known_unsupported()]

    assert names == ["lazada", "amazon", "ebay", "tiktok_shop"]
    assert "shopee" not in names
    assert all(entry.reason_vi and entry.hosts for entry in KNOWN_UNSUPPORTED_PLATFORMS)


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("lazada.vn", "lazada"),
        ("www.lazada.vn", "lazada"),
        ("amzn.to", "amazon"),
        ("de.amazon.com", "amazon"),
        ("ebay.com", "ebay"),
        ("vt.tiktok.com", "tiktok_shop"),
        ("shopee.vn", None),
        ("example.com", None),
    ],
)
def test_match_known_unsupported_uses_domain_boundaries(host: str, expected: str | None) -> None:
    match = match_known_unsupported(host)

    assert (match.name if match else None) == expected


def test_known_platform_still_raises_unsupported_platform_error() -> None:
    with pytest.raises(UnsupportedPlatformError) as excinfo:
        classify_url("https://www.lazada.vn/products/x-i1.html")

    assert "Lazada chưa được hỗ trợ" in str(excinfo.value)


def test_unknown_domain_raises_unsupported_url() -> None:
    with pytest.raises(UnsupportedURLError, match="chưa được hỗ trợ"):
        classify_url("https://example.com/p/1")


# ---------------------------------------------------------------------------
# GET /api/platforms
# ---------------------------------------------------------------------------


@pytest.fixture
async def api_client() -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://testserver")
    try:
        yield client
    finally:
        await client.aclose()


async def test_platforms_endpoint_lists_supported_then_recognized(
    api_client: httpx.AsyncClient,
) -> None:
    response = await api_client.get("/api/platforms")

    assert response.status_code == 200
    items = response.json()
    names = [item["name"] for item in items]
    assert names == ["shopee", "tiki", "lazada", "amazon", "ebay", "tiktok_shop"]

    shopee = items[0]
    assert shopee["supported"] is True
    assert shopee["display_name"] == "Shopee"
    assert shopee["hosts"] == ["shopee.vn", "www.shopee.vn"]
    assert shopee["short_hosts"] == ["s.shopee.vn", "shp.ee", "vn.shp.ee"]
    assert shopee["kinds"] == ["product", "shop"]
    assert shopee["currency"] == "VND"
    assert shopee["requires_provider"] == "apify"
    assert shopee["client_patterns"] == [
        r"^/.+-i\.\d+\.\d+/?$",
        r"^/product/\d+/\d+/?$",
        r"^/shop/\d+/?$",
    ]

    tiki = items[1]
    assert tiki["requires_provider"] is None
    assert tiki["short_hosts"] == ["ti.ki", "www.ti.ki"]

    lazada = items[2]
    assert lazada == {
        "name": "lazada",
        "display_name": "Lazada",
        "hosts": list(KNOWN_UNSUPPORTED_PLATFORMS[0].hosts),
        "reason": KNOWN_UNSUPPORTED_PLATFORMS[0].reason_vi,
        "supported": False,
    }


async def test_platforms_openapi_schema_is_discriminated(api_client: httpx.AsyncClient) -> None:
    schema = (await api_client.get("/openapi.json")).json()

    operation = schema["paths"]["/api/platforms"]["get"]
    response_schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert response_schema["type"] == "array"
    assert response_schema["items"]["anyOf"] == [
        {"$ref": "#/components/schemas/SupportedPlatformInfo"},
        {"$ref": "#/components/schemas/UnsupportedPlatformInfo"},
    ]

    supported = schema["components"]["schemas"]["SupportedPlatformInfo"]
    assert supported["required"] == [
        "name",
        "display_name",
        "hosts",
        "short_hosts",
        "kinds",
        "currency",
        "client_patterns",
    ]
    assert supported["properties"]["supported"]["const"] is True

    unsupported = schema["components"]["schemas"]["UnsupportedPlatformInfo"]
    assert unsupported["properties"]["supported"]["const"] is False
    assert set(unsupported["required"]) == {"name", "display_name", "hosts", "reason"}


# ---------------------------------------------------------------------------
# No hardcoded marketplace names outside the plugins
# ---------------------------------------------------------------------------


def _read(relpath: str) -> str:
    return (REPO_ROOT / relpath).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "relpath",
    [
        "backend/app/crawl/classifier.py",
        "backend/app/jobs/submit.py",
        "backend/app/jobs/tasks.py",
        "backend/app/crawl/service.py",
        "backend/app/api/ingest.py",
        "backend/app/api/platforms.py",
        "backend/app/agent/prompts.py",
        "backend/app/agent/tools.py",
        "frontend/lib/url-validation.ts",
        "frontend/lib/stages.ts",
    ],
)
def test_core_files_do_not_hardcode_tiki_or_shopee(relpath: str) -> None:
    source = _read(relpath)
    lowered = source.lower()

    assert "ti.ki" not in lowered, f"hardcoded Tiki short host in {relpath}"
    for pattern in (r"\btiki\b", r"\bshopee\b"):
        match = re.search(pattern, lowered)
        if match is not None:
            line = source[: match.start()].count("\n") + 1
            context = source.splitlines()[line - 1].strip()
            # Comments may name a platform; code/messages must not.
            assert context.startswith(("#", "//", "*", '"""', "'''")), (
                f"hardcoded platform reference in {relpath}:{line}: {context}"
            )


def test_plugin_packages_are_where_platform_knowledge_lives() -> None:
    for name in ("tiki", "shopee"):
        module = importlib.import_module(f"app.platforms.plugins.{name}")
        assert isinstance(module.PLUGIN, PlatformPlugin)
