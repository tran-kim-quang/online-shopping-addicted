"""Unit tests for ``app.ingest.embedding`` (no model download, no network)."""

from __future__ import annotations

import threading
from typing import Any

import pytest

from app.core.config import Settings
from app.ingest.embedding import (
    LocalEmbedder,
    NullEmbedder,
    ProviderEmbedder,
    get_embedder,
)
from tests.mock_openai import EMBED_MODEL

SECRET = "vN5y6rM0J9h2xL8qK3pW1zB4cF7dG0tH2sV5mN8wX4Q="


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "SECRET_KEY": SECRET,
        "EMBEDDING_BACKEND": "local",
        "EMBEDDING_MODEL_NAME": "fake/model",
        "EMBEDDING_BATCH_SIZE": 4,
    }
    values.update(overrides)
    return Settings(**values)


class FakeModel:
    """Stand-in for ``sentence_transformers.SentenceTransformer``."""

    def __init__(self, dim: int = 6) -> None:
        self.dim = dim
        self.encoded: list[list[str]] = []
        self.encode_kwargs: list[dict[str, Any]] = []
        self.threads: list[str] = []

    def get_sentence_embedding_dimension(self) -> int:
        return self.dim

    def encode(self, texts: list[str], **kwargs: Any) -> list[list[float]]:
        self.encoded.append(list(texts))
        self.encode_kwargs.append(kwargs)
        self.threads.append(threading.current_thread().name)
        return [[0.5] * self.dim for _ in texts]


def make_local_embedder(
    model: FakeModel,
    *,
    batch_size: int = 4,
    loads: list[str] | None = None,
) -> LocalEmbedder:
    recorded = loads if loads is not None else []

    def _loader(model_name: str) -> FakeModel:
        recorded.append(model_name)
        return model

    return LocalEmbedder(model_name="fake/model", batch_size=batch_size, loader=_loader)


# ---------------------------------------------------------------------------
# get_embedder factory
# ---------------------------------------------------------------------------


def test_no_real_embedding_stack_is_imported_during_tests() -> None:
    """Guard rail: the suite must never load torch/sentence-transformers."""
    import sys

    assert "sentence_transformers" not in sys.modules
    assert "torch" not in sys.modules


def test_get_embedder_none_backend_returns_null_embedder() -> None:
    embedder = get_embedder(make_settings(EMBEDDING_BACKEND="none"))

    assert isinstance(embedder, NullEmbedder)
    assert embedder.dim is None


async def test_null_embedder_never_produces_vectors() -> None:
    embedder = NullEmbedder()

    assert await embedder.embed(["a", "b"]) == []
    assert embedder.name == "none"


def test_get_embedder_local_returns_configured_singleton() -> None:
    settings = make_settings(EMBEDDING_MODEL_NAME="BAAI/bge-m3", EMBEDDING_BATCH_SIZE=8)

    first = get_embedder(settings)
    second = get_embedder(settings)

    assert isinstance(first, LocalEmbedder)
    assert first is second, "LocalEmbedder must be a process-wide singleton"
    assert first.name == "BAAI/bge-m3"
    assert first.batch_size == 8
    assert first.dim == 1024  # known model dimension, no download needed
    assert first.is_loaded is False


def test_get_embedder_rejects_unknown_backend() -> None:
    with pytest.raises(ValueError, match="Unsupported EMBEDDING_BACKEND"):
        get_embedder(make_settings(EMBEDDING_BACKEND="bogus"))


def test_get_embedder_provider_uses_llm_client_and_capability_dim(llm_client: Any) -> None:
    profile = SimpleProfile(embedding_model="mock-embed", capabilities={"embedding_dim": 8})

    embedder = get_embedder(
        make_settings(EMBEDDING_BACKEND="provider"),
        profile=profile,
        llm_client=llm_client,
    )

    assert isinstance(embedder, ProviderEmbedder)
    assert embedder.name == "mock-embed"
    assert embedder.dim == 8


def test_get_embedder_provider_without_embedding_model_falls_back_to_local() -> None:
    settings = make_settings(EMBEDDING_BACKEND="provider")

    embedder = get_embedder(settings, profile=SimpleProfile(embedding_model=None))

    assert isinstance(embedder, LocalEmbedder)
    assert embedder.name == "fake/model"


class SimpleProfile:
    """Attribute-only stand-in for ``LLMProfile`` rows."""

    def __init__(
        self,
        *,
        embedding_model: str | None,
        capabilities: dict[str, Any] | None = None,
        base_url: str = "http://mock/v1",
        chat_model: str = "mock-chat",
        small_model: str | None = None,
        api_key_enc: str | None = None,
        name: str = "test",
        id: Any = None,
    ) -> None:
        self.embedding_model = embedding_model
        self.capabilities = capabilities or {}
        self.base_url = base_url
        self.chat_model = chat_model
        self.small_model = small_model
        self.api_key_enc = api_key_enc
        self.name = name
        self.id = id


# ---------------------------------------------------------------------------
# LocalEmbedder — lazy loading, thread offload, batching
# ---------------------------------------------------------------------------


async def test_local_embedder_is_lazy_and_loads_the_model_once() -> None:
    model = FakeModel()
    loads: list[str] = []
    embedder = make_local_embedder(model, loads=loads)

    assert loads == []
    assert embedder.is_loaded is False

    await embedder.embed(["một"])
    await embedder.embed(["hai"])

    assert loads == ["fake/model"]
    assert embedder.is_loaded is True


async def test_local_embedder_detects_dim_from_the_model() -> None:
    model = FakeModel(dim=12)
    embedder = LocalEmbedder(model_name="unknown/model", loader=lambda _name: model)

    assert embedder.dim is None
    vectors = await embedder.embed(["x"])

    assert embedder.dim == 12
    assert len(vectors[0]) == 12


async def test_local_embedder_runs_encode_in_a_worker_thread() -> None:
    model = FakeModel()
    embedder = make_local_embedder(model)

    await embedder.embed(["x"])

    assert model.threads and model.threads[0] != threading.main_thread().name


async def test_local_embedder_batches_and_normalizes() -> None:
    model = FakeModel()
    embedder = make_local_embedder(model, batch_size=2)

    vectors = await embedder.embed(["a", "b", "c", "d", "e"])

    assert len(vectors) == 5
    assert model.encoded == [["a", "b"], ["c", "d"], ["e"]]
    assert all(kwargs["normalize_embeddings"] is True for kwargs in model.encode_kwargs)
    assert all(kwargs["batch_size"] == 2 for kwargs in model.encode_kwargs)


async def test_local_embedder_empty_input_skips_the_model() -> None:
    model = FakeModel()
    loads: list[str] = []
    embedder = make_local_embedder(model, loads=loads)

    assert await embedder.embed([]) == []
    assert loads == []


async def test_local_embedder_reconfigure_with_new_model_resets_state() -> None:
    first = FakeModel(dim=4)
    second = FakeModel(dim=10)
    models = {"fake/model": first, "other/model": second}
    embedder = LocalEmbedder(model_name="fake/model", loader=lambda name: models[name])
    await embedder.embed(["x"])
    assert embedder.is_loaded is True

    embedder.configure(model_name="other/model", batch_size=3)

    assert embedder.is_loaded is False
    assert embedder.batch_size == 3
    await embedder.embed(["y"])
    assert embedder.dim == 10


async def test_local_embedder_accepts_to_list_style_vectors() -> None:
    class ArrayLike:
        def __init__(self, rows: list[list[float]]) -> None:
            self._rows = rows

        def tolist(self) -> list[list[float]]:
            return self._rows

    class ArrayModel(FakeModel):
        def encode(self, texts: list[str], **kwargs: Any) -> Any:
            return ArrayLike([[1, 0, 0] for _ in texts])

    embedder = LocalEmbedder(model_name="array/model", loader=lambda _name: ArrayModel())

    assert await embedder.embed(["x"]) == [[1.0, 0.0, 0.0]]


# ---------------------------------------------------------------------------
# ProviderEmbedder
# ---------------------------------------------------------------------------


@pytest.fixture
def provider_client(client_factory: Any, behavior: Any) -> Any:
    behavior.embeddings = True
    behavior.embedding_dim = 8
    return client_factory(embedding_model=EMBED_MODEL)


async def test_provider_embedder_calls_the_provider_and_infers_dim(
    provider_client: Any,
    behavior: Any,
) -> None:
    embedder = ProviderEmbedder(provider_client, model=EMBED_MODEL)

    vectors = await embedder.embed(["xin chào", "tạm biệt"])

    assert embedder.name == EMBED_MODEL
    assert embedder.dim == 8
    assert len(vectors) == 2
    assert behavior.embedding_requests == [["xin chào", "tạm biệt"]]


async def test_provider_embedder_empty_input_skips_the_provider(provider_client: Any) -> None:
    embedder = ProviderEmbedder(provider_client, model=EMBED_MODEL)

    assert await embedder.embed([]) == []


async def test_provider_embedder_requires_a_client_or_config() -> None:
    with pytest.raises(ValueError, match="requires either 'client' or 'config'"):
        ProviderEmbedder(model=EMBED_MODEL)


async def test_provider_embedder_propagates_provider_errors(
    provider_client: Any,
    behavior: Any,
) -> None:
    from app.core.errors import LLMError

    behavior.embeddings = False  # provider answers 404
    embedder = ProviderEmbedder(provider_client, model=EMBED_MODEL)

    with pytest.raises(LLMError):
        await embedder.embed(["x"])
