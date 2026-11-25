"""FastAPI dependency providers (all overridable via ``app.dependency_overrides``)."""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.config import get_settings as _get_settings
from app.db.database import Database
from app.llm.registry import LLMClientRegistry
from app.security.secrets import SecretBox
from app.security.url_validator import DNSResolver


async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield an async SQLAlchemy session from the singleton ``Database``."""
    async with Database().session() as session:
        yield session


def get_registry() -> LLMClientRegistry:
    """Return the singleton ``LLMClientRegistry``."""
    return LLMClientRegistry()


def get_secret_box() -> SecretBox:
    """Return the singleton ``SecretBox``."""
    return SecretBox()


def get_settings() -> Settings:
    """Return the cached ``Settings`` singleton."""
    return _get_settings()


def get_url_resolver() -> DNSResolver | None:
    """Return an optional DNS resolver override for outbound URL validation."""
    return None
