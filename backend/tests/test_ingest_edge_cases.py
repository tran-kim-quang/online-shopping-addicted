"""Edge-case unit tests for the ingestion layer (branch coverage, no I/O)."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

import app.ingest.chunking as chunking_module
import app.ingest.embedding as embedding_module
import app.ingest.pipeline as pipeline_module
import app.ingest.sentiment as sentiment_module
from app.core.config import Settings
from app.ingest.chunking import (
    _split_oversized_unit,
    _tail_overlap,
    build_product_chunks,
    chunk_description,
    compute_content_hash,
)
from app.ingest.embedding import LocalEmbedder, ProviderEmbedder, get_embedder
from app.ingest.pipeline import _parse_uuid, _resolve_llm_client
from app.ingest.sentiment import _resolve_batch_size, analyze_product_reviews
from app.jobs.events import JobEvents
from tests.ingest_fakes import (
    FakeEmbedder,
    FakeRedis,
    Rows,
    ScriptedSession,
    make_product_row,
    make_review_row,
)

SECRET = "vN5y6rM0J9h2xL8qK3pW1zB4cF7dG0tH2sV5mN8wX4Q="


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"SECRET_KEY": SECRET}
    values.update(overrides)
    return Settings(**values)


# ---------------------------------------------------------------------------
# Chunking branch edges
# ---------------------------------------------------------------------------


def test_tail_overlap_returns_empty_for_non_positive_overlap() -> None:
    assert _tail_overlap("một đoạn văn", 0) == ""
    assert _tail_overlap("một đoạn văn", -5) == ""
    assert _tail_overlap("", 10) == ""


def test_tail_overlap_returns_the_whole_short_tail() -> None:
    assert _tail_overlap("ngắn", 50) == "ngắn"


def test_tail_overlap_trims_partial_first_word() -> None:
    overlap = _tail_overlap("alpha beta gamma delta", 12)

    assert overlap
    assert "alpha beta gamma delta".endswith(overlap)
    assert not overlap.startswith("a")


def test_split_oversized_unit_keeps_short_units_intact() -> None:
    assert _split_oversized_unit("ngắn", 100, 10) == ["ngắn"]


def test_split_oversized_unit_handles_a_single_unbreakable_token() -> None:
    token = "x" * 500

    parts = _split_oversized_unit(token, 100, 20)

    assert len(parts) > 1
    assert all(len(part) <= 100 for part in parts)
    assert "".join(parts).count("x") >= 500


def test_description_with_only_blank_paragraphs_yields_no_chunk() -> None:
    assert chunk_description("Tiêu đề", "\n\n   \n\n\n\t\n") == []


def test_meta_chunk_formats_fractional_prices() -> None:
    product = make_product_row(price="125000.5")

    text = chunking_module.chunk_meta(product).text

    assert "Giá: 125,000.50 VND" in text


def test_chunk_limits_fall_back_when_settings_are_unavailable(monkeypatch: Any) -> None:
    def _boom() -> Any:
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(chunking_module, "get_settings", _boom)

    drafts = build_product_chunks(
        make_product_row(description_text="Mô tả đủ dài để tạo một chunk duy nhất."),
        [],
    )

    assert drafts[0].source_type == "meta"
    assert any(draft.source_type == "description" for draft in drafts)


def test_build_product_chunks_without_description_or_reviews_only_meta() -> None:
    drafts = build_product_chunks(make_product_row(description_text=None, specs={}), [])

    assert [draft.source_type for draft in drafts] == ["meta"]
    assert drafts[0].content_hash == compute_content_hash(drafts[0].text)


# ---------------------------------------------------------------------------
# Embedding branch edges
# ---------------------------------------------------------------------------


def test_local_embedder_falls_back_to_defaults_without_settings(monkeypatch: Any) -> None:
    def _boom() -> Any:
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(embedding_module, "get_settings", _boom)

    embedder = LocalEmbedder()

    assert embedder.name == "BAAI/bge-m3"
    assert embedder.batch_size == 16
    assert embedder.dim == 1024


async def test_local_embedder_detects_dim_from_vectors_without_a_model_hook() -> None:
    class BareModel:
        def encode(self, texts: list[str], **_kwargs: Any) -> list[list[float]]:
            return [[0.1, 0.2, 0.3] for _ in texts]

    embedder = LocalEmbedder(model_name="bare/model", loader=lambda _name: BareModel())

    await embedder.embed(["x"])

    assert embedder.dim == 3


async def test_local_embedder_configure_accepts_a_new_loader() -> None:
    model = object()
    embedder = LocalEmbedder(model_name="fake/model", loader=lambda _name: pytest.fail("not used"))

    def _loader(_name: str) -> Any:
        return _RecordingModel(model)

    embedder.configure(loader=_loader)

    assert await embedder.embed(["x"]) == [[0.0]]


class _RecordingModel:
    def __init__(self, _model: Any) -> None:
        self.encoded = 0

    def get_sentence_embedding_dimension(self) -> int:
        return 1

    def encode(self, texts: list[str], **_kwargs: Any) -> list[list[float]]:
        self.encoded += len(texts)
        return [[0.0] for _ in texts]


async def test_provider_embedder_resolves_a_client_from_a_registry(
    llm_client: Any, behavior: Any
) -> None:
    from pydantic import SecretStr

    from app.llm.types import LLMConfig

    behavior.embeddings = True
    config = LLMConfig(
        base_url="http://mock/v1",
        api_key=SecretStr("«redacted:sk-…»"),
        chat_model="mock-chat",
        embedding_model="mock-embed",
    )

    class Registry:
        calls = 0

        async def get(self, _config: Any) -> Any:
            Registry.calls += 1
            return llm_client

    embedder = ProviderEmbedder(model="mock-embed", config=config, registry=Registry())
    vectors = await embedder.embed(["a"])

    assert Registry.calls == 1
    assert len(vectors) == 1
    # The resolved client is cached: no second registry lookup.
    await embedder.embed(["b"])
    assert Registry.calls == 1


def test_get_embedder_provider_builds_a_lazy_provider_embedder() -> None:
    class Profile:
        embedding_model = "mock-embed"
        capabilities = {"embedding_dim": 16}
        base_url = "http://mock/v1"
        chat_model = "mock-chat"
        small_model = None
        api_key_enc = None

    embedder = get_embedder(make_settings(EMBEDDING_BACKEND="provider"), profile=Profile())  # type: ignore[arg-type]

    assert isinstance(embedder, ProviderEmbedder)
    assert embedder.name == "mock-embed"
    assert embedder.dim == 16


# ---------------------------------------------------------------------------
# Sentiment branches
# ---------------------------------------------------------------------------


def test_sentiment_batch_size_falls_back_when_settings_are_unavailable(monkeypatch: Any) -> None:
    def _boom() -> Any:
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(sentiment_module, "get_settings", _boom)

    assert _resolve_batch_size(None) == 20
    assert _resolve_batch_size(3) == 3
    assert _resolve_batch_size(0) == 1


async def test_no_pending_reviews_still_recomputes_counts() -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from app.ingest.sentiment import review_content_hash

    review = make_review_row("r1")
    review.analyzed_at = datetime.now(UTC)
    review.content_hash = review_content_hash(review)
    session = ScriptedSession(
        [Rows([review]), Rows(one=SimpleNamespace(pos=1, neg=0, neu=0)), Rows()]
    )

    summary = await analyze_product_reviews(session, uuid.uuid4())  # type: ignore[arg-type]

    assert summary.analyzed == 0
    assert summary.skipped == 1
    assert summary.positive == 1


# ---------------------------------------------------------------------------
# Pipeline helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            uuid.UUID("11111111-1111-1111-1111-111111111111"),
            uuid.UUID("11111111-1111-1111-1111-111111111111"),
        ),
        ("11111111-1111-1111-1111-111111111111", uuid.UUID("11111111-1111-1111-1111-111111111111")),
        (
            " 11111111-1111-1111-1111-111111111111 ",
            uuid.UUID("11111111-1111-1111-1111-111111111111"),
        ),
        ("not-a-uuid", None),
        ("   ", None),
        (None, None),
        (123, None),
        ({"a": 1}, None),
    ],
)
def test_parse_uuid(raw: Any, expected: Any) -> None:
    assert _parse_uuid(raw) == expected


async def test_resolve_llm_client_decrypts_the_profile_key(llm_client: Any) -> None:
    from pydantic import SecretStr

    from app.security.secrets import SecretBox

    box = SecretBox(SECRET)
    profile = _Profile(
        base_url="http://mock/v1",
        chat_model="mock-chat",
        api_key_enc=box.encrypt("«redacted:sk-…»"),
    )

    client = await _resolve_llm_client(profile, make_settings())  # type: ignore[arg-type]

    assert client is not None
    assert client.config.base_url == "http://mock/v1"
    assert isinstance(client.config.api_key, SecretStr)
    assert client.config.embedding_model is None


async def test_resolve_llm_client_returns_none_without_a_profile() -> None:
    assert await _resolve_llm_client(None, make_settings()) is None


class _Profile:
    def __init__(
        self,
        *,
        base_url: str,
        chat_model: str,
        api_key_enc: str | None = None,
        small_model: str | None = None,
        embedding_model: str | None = None,
        capabilities: dict[str, Any] | None = None,
    ) -> None:
        self.id = uuid.uuid4()
        self.base_url = base_url
        self.chat_model = chat_model
        self.api_key_enc = api_key_enc
        self.small_model = small_model
        self.embedding_model = embedding_model
        self.capabilities = capabilities or {}


def test_pipeline_module_exposes_ingest_stats_helpers() -> None:
    assert pipeline_module.IngestStats().timings.total_seconds == 0.0


# ---------------------------------------------------------------------------
# JobEvents branch edges
# ---------------------------------------------------------------------------


def test_job_events_defaults_to_the_redis_singleton() -> None:
    events = JobEvents()

    with pytest.raises(RuntimeError, match="RedisClient is not initialized"):
        _ = events.redis


async def test_last_event_ignores_corrupt_payloads(redis_fake: FakeRedis) -> None:
    events = JobEvents(redis_fake)  # type: ignore[arg-type]
    await events.emit("job-x", stage="ok")
    redis_fake.lists["job:job-x:events"].append("{broken")
    redis_fake.lists["job:job-x:events"].append("[1, 2]")

    last = await events.last_event("job-x")

    assert last is None or isinstance(last, dict)


async def test_stream_ignores_non_message_and_malformed_pubsub_payloads() -> None:
    import asyncio

    redis_fake = FakeRedis()
    events = JobEvents(redis_fake)  # type: ignore[arg-type]
    job_id = "job-live"
    await events.create_job(job_id)
    await events.set_status(job_id, "running")
    agen = events.stream(job_id, keepalive_seconds=0.5)

    async def _inject() -> None:
        channel = JobEvents.channel_name(job_id)
        await asyncio.sleep(0.02)
        redis_fake.deliver_raw(channel, {"type": "subscribe", "data": 1})
        redis_fake.deliver_raw(channel, {"type": "message", "data": "{not json"})
        redis_fake.deliver_raw(channel, {"type": "message", "data": "[1, 2]"})
        await events.set_status(job_id, "failed", error="boom")
        redis_fake.deliver_raw(
            channel,
            {
                "type": "message",
                "data": json.dumps(
                    {"seq": 99, "stage": "failed", "done": 0, "total": None, "message": "boom"}
                ),
            },
        )

    injector = asyncio.create_task(_inject())
    try:
        frames: list[tuple[str, Any]] = []
        while True:
            item = await asyncio.wait_for(agen.__anext__(), timeout=2)
            if item[0] == "keepalive":
                continue
            frames.append(item)
            if item[0] in ("done", "error"):
                break
    finally:
        await agen.aclose()
        await injector

    assert [name for name, _ in frames] == ["progress", "error"]
    assert frames[-1][1]["status"] == "failed"
    assert frames[-1][1]["error"] == "boom"


@pytest.fixture
def redis_fake() -> FakeRedis:
    return FakeRedis()


def test_fake_embedder_is_deterministic() -> None:
    embedder = FakeEmbedder(dim=4)

    assert embedder.vector("abc") == embedder.vector("abc")
    assert embedder.vector("abc") != embedder.vector("abd")


# ---------------------------------------------------------------------------
# Pipeline: product row deleted between scope resolution and per-product work
# ---------------------------------------------------------------------------


class _VanishingProductSession:
    """Session double where ``Source`` exists but the ``Product`` row is gone."""

    def __init__(self, source: Any) -> None:
        self._source = source
        self.commits = 0

    async def get(self, model: Any, _pk: Any) -> Any:
        from app.db.models import Source

        return self._source if model is Source else None

    async def commit(self) -> None:
        self.commits += 1

    async def execute(self, _stmt: Any) -> Any:
        return Rows([])


class _VanishingFactory:
    def __init__(self, session: Any) -> None:
        self._session = session
        self.opens = 0

    def __call__(self) -> _VanishingFactory:
        return self

    async def __aenter__(self) -> Any:
        self.opens += 1
        return self._session

    async def __aexit__(self, *_exc: Any) -> None:
        return None


async def test_pipeline_reports_a_product_that_disappeared(monkeypatch: Any) -> None:
    import uuid as uuid_module
    from types import SimpleNamespace

    from app.db.models import Source

    source = Source(
        id=uuid_module.uuid4(), url="https://tiki.vn/x-p1.html", status="done", stats={}
    )
    factory = _VanishingFactory(_VanishingProductSession(source))
    vanished = SimpleNamespace(id=uuid_module.uuid4())

    async def _fake_scope(_session: Any, _source: Any) -> Any:
        return None, [vanished]

    monkeypatch.setattr(pipeline_module, "resolve_source_scope", _fake_scope)

    result = await pipeline_module.ingest_source(
        source.id,
        session_factory=factory,
        embedder=FakeEmbedder(dim=4),
        settings=make_settings(),
    )

    assert result.status == "failed"
    assert result.stats.products_failed == 1
    assert "disappeared" in result.stats.errors[0]["error"]
    assert source.status == "failed"
    assert source.stats["ingest"]["products_failed"] == 1
