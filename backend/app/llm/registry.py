"""LRU registry of ``LLMClient`` instances (Singleton)."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.singleton import SingletonMeta
from app.llm.client import LLMClient
from app.llm.types import LLMConfig

logger = logging.getLogger(__name__)

HttpClientFactory = Callable[[], httpx.AsyncClient]


def _default_max_clients() -> int:
    try:
        return get_settings().LLM_REGISTRY_MAX_CLIENTS
    except Exception:
        return 64


class LLMClientRegistry(metaclass=SingletonMeta):
    """Singleton LRU cache managing ``LLMClient`` lifecycles across profiles.

    Cache keys are SHA-256 digests of
    ``(base_url, api_key, chat_model, small_model, embedding_model, timeout, max_retries)``
    so plaintext API keys are never stored as dictionary keys.
    """

    def __init__(self, max_clients: int | None = None) -> None:
        self._clients: OrderedDict[str, LLMClient] = OrderedDict()
        self._factory_http_clients: dict[int, httpx.AsyncClient] = {}
        self._lock: asyncio.Lock = asyncio.Lock()
        self._max_clients: int = max_clients if max_clients is not None else _default_max_clients()
        self._http_client_factory: HttpClientFactory | None = None
        self._closing_tasks: set[asyncio.Task[Any]] = set()

    def configure(self, *, max_clients: int | None = None) -> None:
        """Update registry limits (useful at startup or in tests)."""
        if max_clients is not None:
            if max_clients < 1:
                raise ValueError("max_clients must be >= 1.")
            self._max_clients = max_clients

    @property
    def max_clients(self) -> int:
        return self._max_clients

    @property
    def http_client_factory(self) -> HttpClientFactory | None:
        return self._http_client_factory

    @http_client_factory.setter
    def http_client_factory(self, factory: HttpClientFactory | None) -> None:
        self._http_client_factory = factory

    def set_http_client_factory(self, factory: HttpClientFactory | None) -> None:
        """Set an optional factory producing ``httpx.AsyncClient`` (for testing)."""
        self._http_client_factory = factory

    @staticmethod
    def _make_key(config: LLMConfig) -> str:
        raw_key = config.api_key.get_secret_value() if config.api_key is not None else ""
        small_model = config.small_model or ""
        embedding_model = config.embedding_model or ""
        material = (
            f"{config.base_url}\x00{raw_key}\x00"
            f"{config.chat_model}\x00{small_model}\x00{embedding_model}\x00"
            f"{config.timeout}\x00{config.max_retries}\x00"
            f"{config.usage_kind or ''}\x00{config.daily_limit}"
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    async def _close_client(self, client: LLMClient) -> None:
        await client.aclose()
        http_client = self._factory_http_clients.pop(id(client), None)
        if http_client is not None and not http_client.is_closed:
            await http_client.aclose()

    def _schedule_close(self, client: LLMClient) -> None:
        async def _close_safe() -> None:
            try:
                await self._close_client(client)
            except Exception:
                logger.debug("Ignoring error while closing evicted LLMClient", exc_info=True)

        task = asyncio.create_task(_close_safe())
        self._closing_tasks.add(task)
        task.add_done_callback(self._closing_tasks.discard)

    async def get(self, config: LLMConfig) -> LLMClient:
        """Return a cached ``LLMClient`` or create one, evicting the LRU entry if full."""
        key = self._make_key(config)
        async with self._lock:
            existing = self._clients.get(key)
            if existing is not None:
                self._clients.move_to_end(key)
                return existing

            http_client = (
                self._http_client_factory() if self._http_client_factory is not None else None
            )
            client = LLMClient(config, http_client=http_client)
            if http_client is not None:
                self._factory_http_clients[id(client)] = http_client
            self._clients[key] = client

            while len(self._clients) > self._max_clients:
                _, evicted = self._clients.popitem(last=False)
                self._schedule_close(evicted)

            return client

    async def invalidate(self, config: LLMConfig) -> None:
        """Remove and close the cached client for ``config``, if present."""
        key = self._make_key(config)
        async with self._lock:
            client = self._clients.pop(key, None)

        if client is not None:
            await self._close_client(client)

    async def aclose_all(self) -> None:
        """Close all cached clients and await any pending eviction closures."""
        async with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
            pending = list(self._closing_tasks)

        if clients:
            await asyncio.gather(*(self._close_client(c) for c in clients), return_exceptions=True)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
