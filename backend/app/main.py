"""FastAPI application factory, lifespan wiring, and error handling."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.api.chat import router as chat_router
from app.api.health import router as health_router
from app.api.ingest import router as ingest_router
from app.api.jobs import router as jobs_router
from app.api.llm_profiles import router as llm_profiles_router
from app.api.reports import router as reports_router
from app.api.search import router as search_router
from app.api.sources import router as sources_router
from app.cache.redis import RedisClient
from app.core.config import get_settings
from app.core.errors import (
    LLMAuthError,
    LLMBadRequestError,
    LLMError,
    LLMInvalidResponseError,
    LLMNotFoundError,
    LLMRateLimitError,
    LLMUnavailableError,
    sanitize_error_message,
)
from app.db.database import Database
from app.jobs.queue import JobQueue
from app.llm.registry import LLMClientRegistry
from app.security.secrets import SecretBox, SecretBoxError
from app.security.url_validator import UnsafeURLError


class SecretMaskingFilter(logging.Filter):
    """Logging filter that redacts API-key-like tokens from log messages."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = sanitize_error_message(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                sanitize_error_message(arg) if isinstance(arg, str) else arg for arg in record.args
            )
        elif isinstance(record.args, dict):
            record.args = {
                k: sanitize_error_message(v) if isinstance(v, str) else v
                for k, v in record.args.items()
            }
        return True


def _configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(level=level)
    root = logging.getLogger()
    root.setLevel(level)
    secret_filter = SecretMaskingFilter()
    for handler in root.handlers:
        if not any(isinstance(f, SecretMaskingFilter) for f in handler.filters):
            handler.addFilter(secret_filter)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Initialize process-wide singletons on startup and clean up on shutdown."""
    settings = get_settings()
    _configure_logging(settings.LOG_LEVEL)

    Database().init(settings.DATABASE_URL)
    RedisClient().init(settings.REDIS_URL)
    JobQueue().init(settings.REDIS_URL)
    SecretBox().init(settings.SECRET_KEY)
    LLMClientRegistry().configure(max_clients=settings.LLM_REGISTRY_MAX_CLIENTS)

    try:
        yield
    finally:
        await LLMClientRegistry().aclose_all()
        await JobQueue().close()
        await RedisClient().close()
        await Database().dispose()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application instance."""
    application = FastAPI(
        title="Shop QA API",
        version="0.1.0",
        lifespan=lifespan,
    )

    @application.exception_handler(UnsafeURLError)
    async def _handle_unsafe_url(_request: Request, exc: UnsafeURLError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": "unsafe_url", "detail": str(exc)},
        )

    @application.exception_handler(SecretBoxError)
    async def _handle_secret_box_error(_request: Request, _exc: SecretBoxError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": "secret_key_mismatch",
                "detail": (
                    "Stored API key cannot be decrypted with the current SECRET_KEY. "
                    "Re-enter the API key."
                ),
            },
        )

    @application.exception_handler(LLMAuthError)
    async def _handle_llm_auth(_request: Request, exc: LLMAuthError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": "llm_auth", "detail": exc.message},
        )

    @application.exception_handler(LLMNotFoundError)
    async def _handle_llm_not_found(_request: Request, exc: LLMNotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"error": "llm_not_found", "detail": exc.message},
        )

    @application.exception_handler(LLMRateLimitError)
    async def _handle_llm_rate_limit(_request: Request, exc: LLMRateLimitError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"error": "llm_rate_limit", "detail": exc.message},
        )

    @application.exception_handler(LLMUnavailableError)
    async def _handle_llm_unavailable(_request: Request, exc: LLMUnavailableError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"error": "llm_unavailable", "detail": exc.message},
        )

    @application.exception_handler(LLMBadRequestError)
    async def _handle_llm_bad_request(_request: Request, exc: LLMBadRequestError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": "llm_bad_request", "detail": exc.message},
        )

    @application.exception_handler(LLMInvalidResponseError)
    async def _handle_llm_invalid_response(
        _request: Request,
        exc: LLMInvalidResponseError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"error": "llm_invalid_response", "detail": exc.message},
        )

    @application.exception_handler(LLMError)
    async def _handle_llm_error(_request: Request, exc: LLMError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"error": "llm_error", "detail": exc.message},
        )

    application.include_router(health_router)
    application.include_router(llm_profiles_router)
    application.include_router(ingest_router)
    application.include_router(jobs_router)
    application.include_router(sources_router)
    application.include_router(reports_router)
    application.include_router(search_router)
    application.include_router(chat_router)
    return application


app = create_app()
