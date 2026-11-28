"""FastAPI dependency providers (all overridable via ``app.dependency_overrides``)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from fastapi import Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.redis import RedisClient
from app.core.config import Settings
from app.core.config import get_settings as _get_settings
from app.db.database import Database
from app.ingest.embedding import Embedder
from app.jobs.events import JobEvents
from app.jobs.queue import JobQueue
from app.llm.registry import LLMClientRegistry
from app.providers.apify import ApifyClient
from app.providers.scrapecreators import ScrapeCreatorsClient
from app.security.cookies import (
    USER_COOKIE_MAX_AGE,
    USER_COOKIE_NAME,
    sign_user_id,
    verify_user_cookie,
)
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


def get_redis_client() -> Any:
    """Return the active async Redis client from the singleton ``RedisClient``."""
    return RedisClient().client


def get_user_id(
    request: Request,
    response: Response,
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> str:
    """Return the verified anonymous ``user_id`` from ``sqa_uid`` cookie, or mint a new one."""
    secret = settings.SECRET_KEY.get_secret_value()
    raw_cookie = request.cookies.get(USER_COOKIE_NAME)
    existing = verify_user_cookie(secret, raw_cookie)
    if existing is not None:
        return existing

    new_uid = uuid.uuid4().hex
    signed = sign_user_id(secret, new_uid)
    cookie_kwargs: dict[str, Any] = {
        "key": USER_COOKIE_NAME,
        "value": signed,
        "httponly": True,
        "samesite": "lax",
        "path": "/",
        "max_age": USER_COOKIE_MAX_AGE,
        "secure": settings.COOKIE_SECURE,
    }
    response.set_cookie(**cookie_kwargs)
    # Persist on request.state so middleware also attaches Set-Cookie to JSONResponse /
    # StreamingResponse endpoints that bypass FastAPI's injected Response object.
    request.state.pending_uid_cookie = cookie_kwargs
    return new_uid
