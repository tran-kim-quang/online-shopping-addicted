"""Embedding abstractions: local sentence-transformers (Singleton), provider API, and null."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any, Protocol, runtime_checkable

from app.core.config import Settings, get_settings
from app.core.singleton import SingletonMeta
from app.db.models import LLMProfile
from app.llm.client import LLMClient
from app.llm.profile_config import build_llm_config
from app.llm.registry import LLMClientRegistry
from app.llm.types import LLMConfig

logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_MODEL = "BAAI/bge-m3"
DEFAULT_EMBEDDING_BATCH_SIZE = 16

_KNOWN_MODEL_DIMS: dict[str, int] = {
    "BAAI/bge-m3": 1024,
}


@runtime_checkable
class Embedder(Protocol):
    """Protocol implemented by all embedding backends."""

    @property
    def name(self) -> str: ...

    @property
    def dim(self) -> int | None: ...

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def _load_sentence_transformer(model_name: str) -> Any:
    """Lazily import and load a CPU ``SentenceTransformer`` model."""
    started = time.monotonic()
    logger.info("Loading local embedding model '%s' on CPU...", model_name)
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(model_name, device="cpu")
    elapsed = round(time.monotonic() - started, 2)
    logger.info("Loaded local embedding model '%s' in %.2fs.", model_name, elapsed)
    return model


def _to_float_rows(raw_vectors: Any) -> list[list[float]]:
    if hasattr(raw_vectors, "tolist"):
        raw_vectors = raw_vectors.tolist()
    return [[float(x) for x in row] for row in raw_vectors]


class LocalEmbedder(metaclass=SingletonMeta):
    """Process-wide singleton embedding model powered by ``sentence-transformers`` (CPU).

    The underlying model is loaded lazily on the first ``embed()`` call and executed
    inside ``asyncio.to_thread`` so inference never blocks the async event loop.
    """

    def __init__(
        self,
        model_name: str | None = None,
        batch_size: int | None = None,
        *,
        loader: Callable[[str], Any] | None = None,
    ) -> None:
        try:
            settings = get_settings()
        except Exception:
            settings = None

        default_model = (
            settings.EMBEDDING_MODEL_NAME if settings is not None else DEFAULT_EMBEDDING_MODEL
        )
        self._model_name: str = model_name if model_name is not None else default_model
        eff_batch = (
            batch_size
            if batch_size is not None
            else (
                settings.EMBEDDING_BATCH_SIZE
                if settings is not None
                else DEFAULT_EMBEDDING_BATCH_SIZE
            )
        )
        self._batch_size: int = max(1, eff_batch)
        self._loader: Callable[[str], Any] | None = loader
        self._model: Any = None
        self._dim: int | None = _KNOWN_MODEL_DIMS.get(self._model_name)
        self._load_lock = threading.Lock()

    def configure(
        self,
        *,
        model_name: str | None = None,
        batch_size: int | None = None,
        loader: Callable[[str], Any] | None = None,
    ) -> None:
        """Update model name / batch size before loading (or reset loaded model if name changed)."""
        with self._load_lock:
            if model_name is not None and model_name != self._model_name:
                self._model_name = model_name
                self._model = None
                self._dim = _KNOWN_MODEL_DIMS.get(model_name)
            if batch_size is not None:
                self._batch_size = max(1, batch_size)
            if loader is not None:
                self._loader = loader

    @property
    def name(self) -> str:
        return self._model_name

    @property
    def dim(self) -> int | None:
        return self._dim

    @property
    def batch_size(self) -> int:
        return self._batch_size

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is None:
                load_fn = self._loader if self._loader is not None else _load_sentence_transformer
                model = load_fn(self._model_name)
                self._model = model
                get_dim = getattr(model, "get_embedding_dimension", None)
                if not callable(get_dim):
                    get_dim = getattr(model, "get_sentence_embedding_dimension", None)
                if callable(get_dim):
                    detected = get_dim()
                    if isinstance(detected, int) and detected > 0:
                        self._dim = detected
        return self._model

    def _encode_sync(self, texts: list[str]) -> list[list[float]]:
        model = self._ensure_model()
        results: list[list[float]] = []
        for offset in range(0, len(texts), self._batch_size):
            batch = texts[offset : offset + self._batch_size]
            encoded = model.encode(
                batch,
                batch_size=self._batch_size,
                normalize_embeddings=True,
            )
            rows = _to_float_rows(encoded)
            results.extend(rows)

        if self._dim is None and results:
            self._dim = len(results[0])
        return results

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Encode ``texts`` into normalized dense vectors in a background thread."""
        if not texts:
            return []
        return await asyncio.to_thread(self._encode_sync, list(texts))


class ProviderEmbedder:
    """Embedder backed by a user's OpenAI-compatible ``/v1/embeddings`` provider."""

    def __init__(
        self,
        client: LLMClient | None = None,
        *,
        model: str,
        dim: int | None = None,
        config: LLMConfig | None = None,
        registry: LLMClientRegistry | None = None,
    ) -> None:
        if client is None and config is None:
            raise ValueError("ProviderEmbedder requires either 'client' or 'config'.")
        self._client = client
        self._config = config
        self._registry = registry
        self._model = model
        self._dim = dim if (isinstance(dim, int) and dim > 0) else None

    @property
    def name(self) -> str:
        return self._model

    @property
    def dim(self) -> int | None:
        return self._dim

    async def _resolve_client(self) -> LLMClient:
        if self._client is not None:
            return self._client
        assert self._config is not None
        registry = self._registry if self._registry is not None else LLMClientRegistry()
        self._client = await registry.get(self._config)
        return self._client

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        client = await self._resolve_client()
        vectors = await client.embed(list(texts), model=self._model)
        if self._dim is None and vectors:
            self._dim = len(vectors[0])
        return vectors


class NullEmbedder:
    """No-op embedder used when ``EMBEDDING_BACKEND=none`` (full-text search only)."""

    @property
    def name(self) -> str:
        return "none"

    @property
    def dim(self) -> int | None:
        return None

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return []


def _config_from_profile(profile: LLMProfile, settings: Settings) -> LLMConfig:
    return build_llm_config(profile, settings)


def get_embedder(
    settings: Settings | None = None,
    profile: LLMProfile | None = None,
    *,
    llm_client: LLMClient | None = None,
) -> Embedder:
    """Return the configured :class:`Embedder` according to ``EMBEDDING_BACKEND``."""
    active_settings = settings if settings is not None else get_settings()
    backend = active_settings.EMBEDDING_BACKEND.strip().lower()

    if backend == "none":
        return NullEmbedder()

    if backend == "local":
        embedder = LocalEmbedder()
        embedder.configure(
            model_name=active_settings.EMBEDDING_MODEL_NAME,
            batch_size=active_settings.EMBEDDING_BATCH_SIZE,
        )
        return embedder

    if backend == "provider":
        caps = (profile.capabilities or {}) if profile is not None else {}
        cap_dim = caps.get("embedding_dim")
        dim = cap_dim if isinstance(cap_dim, int) and cap_dim > 0 else None

        if llm_client is not None:
            model = (
                profile.embedding_model
                if (profile is not None and profile.embedding_model)
                else llm_client.config.embedding_model
            )
            if model:
                return ProviderEmbedder(llm_client, model=model, dim=dim)

        if profile is not None and profile.embedding_model:
            config = _config_from_profile(profile, active_settings)
            return ProviderEmbedder(model=profile.embedding_model, dim=dim, config=config)

        logger.warning(
            "EMBEDDING_BACKEND='provider' requested without an embedding_model on profile; "
            "falling back to LocalEmbedder."
        )
        fallback = LocalEmbedder()
        fallback.configure(
            model_name=active_settings.EMBEDDING_MODEL_NAME,
            batch_size=active_settings.EMBEDDING_BATCH_SIZE,
        )
        return fallback

    raise ValueError(
        f"Unsupported EMBEDDING_BACKEND '{active_settings.EMBEDDING_BACKEND}'. "
        "Expected one of: local, provider, none."
    )
