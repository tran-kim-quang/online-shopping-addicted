"""FastAPI dependency providers (all overridable via ``app.dependency_overrides``)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.config import get_settings as _get_settings
from app.db.database import Database
from app.ingest.embedding import Embedder
from app.jobs.events import JobEvents
from app.jobs.queue import JobQueue
from app.llm.registry import LLMClientRegistry
from app.providers.apify import ApifyClient
from app.providers.scrapecreators import ScrapeCreatorsClient
from app.security.secrets import SecretBox
from app.security.url_validator import DNSResolver

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield an async SQLAlchemy session from the singleton ``Database``."""
    async with Database().session() as session:
        yield session


def get_session_factory() -> SessionFactory:
    """Return the shared async session factory from the singleton ``Database``."""
    return Database().session_factory


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


def get_job_events() -> JobEvents:
    """Return a ``JobEvents`` helper backed by the singleton ``RedisClient``."""
    return JobEvents()


def get_job_queue() -> JobQueue:
    """Return the singleton ``JobQueue`` for enqueueing Arq tasks."""
    return JobQueue()


def get_embedder_override() -> Embedder | None:
    """Optional embedder override for ``POST /api/search`` (used in tests)."""
    return None


ApifyClientFactory = Callable[[str], ApifyClient]
ScrapeCreatorsClientFactory = Callable[[str], ScrapeCreatorsClient]


def get_apify_client_factory() -> ApifyClientFactory:
    """Return factory creating :class:`ApifyClient` instances for token verification."""
    return lambda token: ApifyClient(token)


def get_scrapecreators_client_factory() -> ScrapeCreatorsClientFactory:
    """Return factory creating :class:`ScrapeCreatorsClient` instances."""
    return lambda api_key: ScrapeCreatorsClient(api_key)
