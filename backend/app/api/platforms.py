"""Supported and recognized e-commerce platforms catalog endpoint."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from app.platforms.registry import PlatformRegistry

router = APIRouter(prefix="/api", tags=["platforms"])


class SupportedPlatformInfo(BaseModel):
    """Metadata and JS-compatible URL patterns for a supported platform plugin."""

    name: str
    display_name: str
    hosts: list[str]
    short_hosts: list[str]
    kinds: list[Literal["product", "shop"]]
    currency: str
    requires_provider: str | None = None
    client_patterns: list[str]
    supported: Literal[True] = True


class UnsupportedPlatformInfo(BaseModel):
    """Metadata and explanation for a recognized but unsupported platform."""

    name: str
    display_name: str
    hosts: list[str]
    reason: str
    supported: Literal[False] = False


PlatformCatalogItem = SupportedPlatformInfo | UnsupportedPlatformInfo


@router.get("/platforms", response_model=list[PlatformCatalogItem])
async def list_platforms() -> list[PlatformCatalogItem]:
    """Return all supported platform plugins followed by recognized unsupported platforms."""
    registry = PlatformRegistry()
    items: list[PlatformCatalogItem] = []

    for plugin in registry.all():
        items.append(
            SupportedPlatformInfo(
                name=plugin.name,
                display_name=plugin.display_name,
                hosts=sorted(plugin.hosts),
                short_hosts=sorted(plugin.short_hosts),
                kinds=sorted(plugin.kinds),  # type: ignore[arg-type]
                currency=plugin.currency,
                requires_provider=plugin.requires_provider,
                client_patterns=list(plugin.client_patterns),
                supported=True,
            )
        )

    for entry in registry.known_unsupported():
        items.append(
            UnsupportedPlatformInfo(
                name=entry.name,
                display_name=entry.display_name,
                hosts=list(entry.hosts),
                reason=entry.reason_vi,
                supported=False,
            )
        )

    return items
