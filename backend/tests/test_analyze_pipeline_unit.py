"""Unit tests for ``app.ingest.pipeline.analyze_source`` orchestration (no Postgres).

The real SQL (scope resolution, counter aggregation, ``review_summaries`` deletion) is
exercised in ``test_analyze_integration.py``; here the per-product analysis, the scope and
the review counter are stubbed so every status branch (done / partial / failed) and the
progress contract can be asserted offline.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import Delete

import app.ingest.pipeline as pipeline_module
from app.core.errors import JobCancelledError
from app.crawl.types import ProgressEvent
from app.db.models import LLMProfile, Product, Source
from app.ingest.pipeline import analyze_source
from app.ingest.sentiment import SentimentSummary
from tests.agent_fakes import make_settings
from tests.ingest_fakes import Rows, SessionFactoryDouble, make_product_row, make_source_row

PROFILE_ID = uuid.uuid4()


class PipelineSession:
    """Session double: ``get`` resolves the ORM rows, ``execute`` only records deletes."""

    def __init__(
        self,
        *,
        source: Source | None,
        profile: LLMProfile | None,
        products: list[Product] | None = None,
    ) -> None:
        self.source = source
        self.profile = profile
        self.products = {product.id: product for product in (products or [])}
        self.deletes: list[Any] = []
        self.commits = 0

    async def get(self, model: Any, pk: Any) -> Any:
        if model is Source:
            return self.source if self.source is not None and self.source.id == pk else None
        if model is LLMProfile:
            return self.profile if self.profile is not None and self.profile.id == pk else None
        if model is Product:
            return self.products.get(pk)
        return None

    async def execute(self, statement: Any) -> Rows:
        self.deletes.append(statement)
        return Rows()

    async def commit(self) -> None:
        self.commits += 1

    async def flush(self) -> None:
        return None


def make_profile() -> LLMProfile:
    return LLMProfile(
        id=PROFILE_ID,
        name="Scripted",
        base_url="https://llm.test/v1",
        chat_model="scripted-chat",
        capabilities={"chat": True, "json_mode": True},
    )


def patch_analysis(
    monkeypatch: Any,
    *,
    products: list[Product],
    reviews_to_analyze: int,
    summaries: dict[Any, Any],
) -> list[dict[str, Any]]:
    """Patch scope, review counter and the per-product analyzer."""
    calls: list[dict[str, Any]] = []

    async def _scope(_session: Any, _source: Any) -> tuple[None, list[Product]]:
        return None, products

    async def _count(*_args: Any, **_kwargs: Any) -> int:
        return reviews_to_analyze

    async def _analyze(_session: Any, product_id: Any, **kwargs: Any) -> Any:
        calls.append({"product_id": product_id, **kwargs})
        outcome = summaries[product_id]
        if isinstance(outcome, Exception):
            raise outcome
        on_batch_done = kwargs.get("on_batch_done")
        if on_batch_done is not None and outcome.changed > 0:
            await on_batch_done(outcome.analyzed)
        return outcome

    monkeypatch.setattr(pipeline_module, "resolve_source_scope", _scope)
    monkeypatch.setattr(pipeline_module, "count_reviews_needing_analysis", _count)
    monkeypatch.setattr(pipeline_module, "analyze_product_reviews", _analyze)
    return calls


def summary(**overrides: Any) -> SentimentSummary:
    values: dict[str, Any] = {
        "analyzed": 2,
        "llm_count": 2,
        "rating_fallback_count": 0,
        "skipped": 0,
        "changed": 2,
        "batches_total": 1,
        "batches_failed": 0,
    }
    values.update(overrides)
    return SentimentSummary(**values)


async def collect_events() -> tuple[list[ProgressEvent], Any]:
    events: list[ProgressEvent] = []

    async def _on_progress(event: ProgressEvent) -> None:
        events.append(event)

    return events, _on_progress


async def test_analyze_marks_a_product_without_changes_as_done_and_keeps_its_summary(
    monkeypatch: Any,
) -> None:
    source = make_source_row()
    product = make_product_row()
    session = PipelineSession(source=source, profile=make_profile(), products=[product])
    calls = patch_analysis(
        monkeypatch,
        products=[product],
        reviews_to_analyze=2,
        summaries={product.id: summary(changed=0)},
    )
    events, on_progress = await collect_events()

    result = await analyze_source(
        source.id,
        llm_profile_id=PROFILE_ID,
        on_progress=on_progress,
        session_factory=SessionFactoryDouble(session),
        llm_client=object(),  # type: ignore[arg-type]
        settings=None,
    )

    assert result.status == "done"
    assert result.stats.products_processed == 1
    assert result.stats.reviews_to_analyze == 2
    assert result.stats.summaries_invalidated == 0
    assert result.stats.reviews_llm == 2
    assert session.deletes == [], "an unchanged product must keep its cached summary"
    assert session.commits == 1
    assert calls[0]["llm_client"] is not None
    assert events[0].stage == "sentiment" and events[0].total == 2


async def test_analyze_deletes_the_summary_only_for_changed_products(
    monkeypatch: Any,
) -> None:
    source = make_source_row()
    changed_product = make_product_row(external_id="1")
    stable_product = make_product_row(external_id="2")
    session = PipelineSession(
        source=source,
        profile=make_profile(),
        products=[changed_product, stable_product],
    )
    patch_analysis(
        monkeypatch,
        products=[changed_product, stable_product],
        reviews_to_analyze=3,
        summaries={
            changed_product.id: summary(analyzed=2, changed=2),
            stable_product.id: summary(analyzed=1, changed=0),
        },
    )

    result = await analyze_source(
        source.id,
        llm_profile_id=PROFILE_ID,
        session_factory=SessionFactoryDouble(session),
        llm_client=object(),  # type: ignore[arg-type]
        settings=None,
    )

    assert result.status == "done"
    assert result.stats.summaries_invalidated == 1
    assert len(session.deletes) == 1
    statement = session.deletes[0]
    assert isinstance(statement, Delete)
    compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
    assert "review_summaries" in compiled


async def test_analyze_reports_progress_monotonically(monkeypatch: Any) -> None:
    source = make_source_row()
    first = make_product_row(external_id="1")
    second = make_product_row(external_id="2")
    session = PipelineSession(source=source, profile=make_profile(), products=[first, second])
    patch_analysis(
        monkeypatch,
        products=[first, second],
        reviews_to_analyze=4,
        summaries={
            first.id: summary(analyzed=2, changed=2),
            second.id: summary(analyzed=2, changed=2),
        },
    )
    events, on_progress = await collect_events()

    await analyze_source(
        source.id,
        llm_profile_id=PROFILE_ID,
        on_progress=on_progress,
        session_factory=SessionFactoryDouble(session),
        llm_client=object(),  # type: ignore[arg-type]
        settings=None,
    )

    done_values = [event.done for event in events]
    assert done_values == [0, 2, 4], "the bar must not jump backwards or overshoot"
    assert all(event.total == 4 for event in events)


async def test_analyze_isolates_a_failing_product_and_ends_partial(monkeypatch: Any) -> None:
    source = make_source_row()
    broken = make_product_row(external_id="1")
    healthy = make_product_row(external_id="2")
    session = PipelineSession(source=source, profile=make_profile(), products=[broken, healthy])
    patch_analysis(
        monkeypatch,
        products=[broken, healthy],
        reviews_to_analyze=2,
        summaries={broken.id: RuntimeError("boom"), healthy.id: summary(analyzed=1, changed=1)},
    )

    result = await analyze_source(
        source.id,
        llm_profile_id=PROFILE_ID,
        session_factory=SessionFactoryDouble(session),
        llm_client=object(),  # type: ignore[arg-type]
        settings=None,
    )

    assert result.status == "partial"
    assert result.stats.products_failed == 1
    assert result.stats.products_processed == 1
    assert result.stats.errors == [{"product_id": str(broken.id), "error": "boom"}]
    assert result.error is not None and "1/2 product(s) failed" in result.error


async def test_analyze_ends_failed_when_every_product_fails(monkeypatch: Any) -> None:
    source = make_source_row()
    product = make_product_row()
    session = PipelineSession(source=source, profile=make_profile(), products=[product])
    patch_analysis(
        monkeypatch,
        products=[product],
        reviews_to_analyze=2,
        summaries={product.id: RuntimeError("all bad")},
    )

    result = await analyze_source(
        source.id,
        llm_profile_id=PROFILE_ID,
        session_factory=SessionFactoryDouble(session),
        llm_client=object(),  # type: ignore[arg-type]
        settings=None,
    )

    assert result.status == "failed"
    assert result.error == "All 1 product(s) failed during LLM analysis."


async def test_analyze_reports_partial_when_only_some_llm_batches_failed(
    monkeypatch: Any,
) -> None:
    source = make_source_row()
    product = make_product_row()
    session = PipelineSession(source=source, profile=make_profile(), products=[product])
    patch_analysis(
        monkeypatch,
        products=[product],
        reviews_to_analyze=4,
        summaries={
            product.id: summary(
                analyzed=4,
                llm_count=2,
                rating_fallback_count=2,
                changed=4,
                batches_total=2,
                batches_failed=1,
            )
        },
    )

    result = await analyze_source(
        source.id,
        llm_profile_id=PROFILE_ID,
        session_factory=SessionFactoryDouble(session),
        llm_client=object(),  # type: ignore[arg-type]
        settings=None,
    )

    assert result.status == "partial"
    assert result.error == ("1/2 LLM batch(es) failed; kept star-rating fallback.")
    assert result.stats.reviews_rating_fallback == 2


async def test_analyze_returns_immediately_when_nothing_needs_analysis(
    monkeypatch: Any,
) -> None:
    source = make_source_row()
    product = make_product_row()
    session = PipelineSession(source=source, profile=make_profile(), products=[product])
    calls = patch_analysis(
        monkeypatch,
        products=[product],
        reviews_to_analyze=0,
        summaries={product.id: summary()},
    )
    events, on_progress = await collect_events()

    result = await analyze_source(
        source.id,
        llm_profile_id=PROFILE_ID,
        on_progress=on_progress,
        session_factory=SessionFactoryDouble(session),
        llm_client=object(),  # type: ignore[arg-type]
        settings=None,
    )

    assert result.status == "done"
    assert result.stats.reviews_to_analyze == 0
    assert result.stats.products_processed == 0
    assert calls == [], "nothing to analyze must not call the per-product pipeline"
    assert events == []
    assert session.commits == 0


async def test_analyze_raises_for_an_unknown_source() -> None:
    session = PipelineSession(source=None, profile=make_profile())

    with pytest.raises(ValueError, match="not found"):
        await analyze_source(
            uuid.uuid4(),
            llm_profile_id=PROFILE_ID,
            session_factory=SessionFactoryDouble(session),
            llm_client=object(),  # type: ignore[arg-type]
            settings=None,
        )


async def test_analyze_counts_a_vanished_product_as_failed(monkeypatch: Any) -> None:
    """A product deleted between scope resolution and analysis must not abort the source."""
    source = make_source_row()
    product = make_product_row()
    session = PipelineSession(source=source, profile=make_profile(), products=[])
    patch_analysis(
        monkeypatch,
        products=[product],
        reviews_to_analyze=2,
        summaries={product.id: summary()},
    )

    result = await analyze_source(
        source.id,
        llm_profile_id=PROFILE_ID,
        session_factory=SessionFactoryDouble(session),
        llm_client=object(),  # type: ignore[arg-type]
        settings=None,
    )

    assert result.status == "failed"
    assert result.stats.products_failed == 1
    assert "disappeared before analysis" in result.stats.errors[0]["error"]


async def test_analyze_raises_when_the_profile_is_missing_and_no_client_is_injected() -> None:
    source = make_source_row()
    session = PipelineSession(source=source, profile=None, products=[])

    with pytest.raises(ValueError, match="profile"):
        await analyze_source(
            source.id,
            llm_profile_id=PROFILE_ID,
            session_factory=SessionFactoryDouble(session),
            settings=None,
        )


async def test_analyze_builds_the_client_from_the_stored_profile(monkeypatch: Any) -> None:
    """The worker injects no client: ``analyze_source`` must build it from the profile row."""
    source = make_source_row()
    product = make_product_row()
    session = PipelineSession(source=source, profile=make_profile(), products=[product])
    calls = patch_analysis(
        monkeypatch,
        products=[product],
        reviews_to_analyze=1,
        summaries={product.id: summary()},
    )
    sentinel = object()
    configs: list[Any] = []

    class _Registry:
        async def get(self, config: Any) -> Any:
            configs.append(config)
            return sentinel

    monkeypatch.setattr(pipeline_module, "LLMClientRegistry", _Registry)

    result = await analyze_source(
        source.id,
        llm_profile_id=PROFILE_ID,
        session_factory=SessionFactoryDouble(session),
        settings=make_settings(),
    )

    assert result.status == "done"
    assert len(configs) == 1
    assert configs[0].base_url == "https://llm.test/v1"
    assert configs[0].chat_model == "scripted-chat"
    assert calls[0]["llm_client"] is sentinel


async def test_analyze_fails_when_the_client_cannot_be_initialized(monkeypatch: Any) -> None:
    """A profile that cannot produce a client must fail the job, not silently star-rate it."""
    source = make_source_row()
    product = make_product_row()
    session = PipelineSession(source=source, profile=make_profile(), products=[product])
    calls = patch_analysis(
        monkeypatch,
        products=[product],
        reviews_to_analyze=1,
        summaries={product.id: summary()},
    )

    async def _no_client(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(pipeline_module, "_resolve_llm_client", _no_client)

    with pytest.raises(ValueError, match="Could not initialize LLM client"):
        await analyze_source(
            source.id,
            llm_profile_id=PROFILE_ID,
            session_factory=SessionFactoryDouble(session),
            settings=make_settings(),
        )

    assert calls == [], "no product may be analysed with a missing client"


async def test_analyze_propagates_cancellation_instead_of_marking_the_product_failed(
    monkeypatch: Any,
) -> None:
    source = make_source_row()
    product = make_product_row()
    session = PipelineSession(source=source, profile=make_profile(), products=[product])
    patch_analysis(
        monkeypatch,
        products=[product],
        reviews_to_analyze=2,
        summaries={product.id: JobCancelledError("job-1", status="failed")},
    )

    with pytest.raises(JobCancelledError):
        await analyze_source(
            source.id,
            llm_profile_id=PROFILE_ID,
            session_factory=SessionFactoryDouble(session),
            llm_client=object(),  # type: ignore[arg-type]
            settings=None,
        )

    assert session.commits == 0, "cancellation must not commit a partial result"
