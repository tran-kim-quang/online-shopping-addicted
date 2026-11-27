"""Platform plugin system with auto-discovery for e-commerce marketplaces."""

from app.platforms.base import PlatformFetcher, PlatformPlugin
from app.platforms.known import KNOWN_UNSUPPORTED_PLATFORMS, KnownUnsupportedPlatform
from app.platforms.registry import PlatformRegistry

__all__ = [
    "KNOWN_UNSUPPORTED_PLATFORMS",
    "KnownUnsupportedPlatform",
    "PlatformFetcher",
    "PlatformPlugin",
    "PlatformRegistry",
]
