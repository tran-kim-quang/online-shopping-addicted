"""Platform crawler adapters and registry."""

from app.crawl.adapters.base import AdapterRegistry, PlatformAdapter
from app.crawl.adapters.tiki import TikiAdapter

__all__ = [
    "AdapterRegistry",
    "PlatformAdapter",
    "TikiAdapter",
]
