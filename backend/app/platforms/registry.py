"""Auto-discovering singleton registry for platform plugins."""

from __future__ import annotations

import importlib
import pkgutil

import app.platforms.plugins as plugins_pkg
from app.core.singleton import SingletonMeta
from app.crawl.errors import UnsupportedPlatformError
from app.platforms.base import PlatformPlugin
from app.platforms.known import KNOWN_UNSUPPORTED_PLATFORMS, KnownUnsupportedPlatform


class PlatformRegistry(metaclass=SingletonMeta):
    """Singleton registry discovering :class:`PlatformPlugin` modules in ``app.platforms.plugins``."""  # noqa: E501

    def __init__(self) -> None:
        self._plugins: dict[str, PlatformPlugin] = {}
        self._discover()

    def _discover(self) -> None:
        for module_info in pkgutil.iter_modules(
            plugins_pkg.__path__,
            f"{plugins_pkg.__name__}.",
        ):
            module = importlib.import_module(module_info.name)
            plugin = getattr(module, "PLUGIN", None)
            if isinstance(plugin, PlatformPlugin):
                self.register(plugin)

    def register(self, plugin: PlatformPlugin) -> None:
        """Register or replace a platform plugin under ``plugin.name``."""
        key = plugin.name.strip().lower()
        if not key:
            raise ValueError("Platform plugin name must not be empty.")
        self._plugins[key] = plugin

    def get(self, name: str) -> PlatformPlugin:
        """Return the plugin for ``name`` or raise :class:`UnsupportedPlatformError`."""
        key = (name or "").strip().lower()
        plugin = self._plugins.get(key)
        if plugin is None:
            raise UnsupportedPlatformError(
                platform=key,
                message=f"Nền tảng '{name}' chưa được hỗ trợ.",
            )
        return plugin

    def for_host(self, host: str) -> PlatformPlugin | None:
        """Return the plugin handling ``host`` (primary or short host), or ``None``."""
        lowered = (host or "").strip().lower()
        if not lowered:
            return None
        for plugin in self._plugins.values():
            if lowered in plugin.hosts or lowered in plugin.short_hosts:
                return plugin
        return None

    def for_short_host(self, host: str) -> PlatformPlugin | None:
        """Return the plugin that declares ``host`` in ``short_hosts``, or ``None``."""
        lowered = (host or "").strip().lower()
        if not lowered:
            return None
        for plugin in self._plugins.values():
            if lowered in plugin.short_hosts:
                return plugin
        return None

    def unsupported_host_reason(self, host: str) -> tuple[str, str] | None:
        """Return ``(platform_name, reason)`` if ``host`` matches ``unsupported_hosts``."""
        lowered = (host or "").strip().lower()
        if not lowered:
            return None
        for plugin in self._plugins.values():
            if lowered in plugin.unsupported_hosts:
                reason = plugin.unsupported_host_reason(lowered)
                if reason:
                    return plugin.name, reason
        return None

    def all(self) -> list[PlatformPlugin]:
        """Return all registered supported platform plugins sorted by name."""
        return [self._plugins[k] for k in sorted(self._plugins.keys())]

    def known_unsupported(self) -> tuple[KnownUnsupportedPlatform, ...]:
        """Return recognized platforms without support yet."""
        return KNOWN_UNSUPPORTED_PLATFORMS
