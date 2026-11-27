"""Agent behaviour for Phase 6: neutral prompts, platform display names, paid sources (A1/A6/A7)."""

from __future__ import annotations

from typing import Any

import pytest

from app.agent.prompts import build_system_prompt
from app.agent.tools import CrawlMoreArgs, _run_crawl_more, build_agent_tools
from tests.agent_fakes import NoopSessionFactory, make_ctx
from tests.ingest_fakes import make_source_row
from tests.shopee_fakes import ScriptedApify  # noqa: F401  (keeps import surface explicit)


def _ctx_with_source(source: Any, **kwargs: Any) -> Any:
    ctx = make_ctx(source=source, **kwargs)
    ctx.session_factory = NoopSessionFactory()  # type: ignore[assignment]
    return ctx


def _source(*, platform: str, sampling: dict[str, Any] | None = None) -> Any:
    row = make_source_row()
    row.platform = platform
    stats = dict(row.stats or {})
    if sampling is not None:
        stats["sampling"] = sampling
    row.stats = stats
    return row


# ---------------------------------------------------------------------------
# Prompt: platform display name, stratified rules, no extra-crawl suggestion
# ---------------------------------------------------------------------------


def test_prompt_uses_the_platform_display_name_instead_of_hardcoded_tiki() -> None:
    tiki_prompt = build_system_prompt(_ctx_with_source(_source(platform="tiki")))
    shopee_prompt = build_system_prompt(_ctx_with_source(_source(platform="shopee")))

    assert "Tiki" in tiki_prompt
    assert "Shopee" in shopee_prompt
    assert "Tiki" not in shopee_prompt


def test_unknown_platform_falls_back_to_its_own_name_without_docs_hint() -> None:
    prompt = build_system_prompt(_ctx_with_source(_source(platform="lazada")))

    assert "lazada" in prompt
    assert "docs/" not in prompt


def test_stratified_prompt_forbids_percentages_and_extra_collection() -> None:
    prompt = build_system_prompt(
        _ctx_with_source(
            _source(
                platform="shopee",
                sampling={"strategy": "stratified", "window_months": 12, "groups": []},
            )
        )
    )

    assert "QUY TẮC MẪU ĐÁNH GIÁ CHỌN LỌC" in prompt
    assert "CHỈ lấy từ `rating_counts`" in prompt
    assert "TUYỆT ĐỐI KHÔNG dùng số lượng review trong DB để suy ra tỷ lệ khen/chê" in prompt
    assert "CHỈ lấy từ số sàn báo" in prompt
    assert "KHÔNG gợi ý người dùng hoặc tự thu thập thêm đánh giá" in prompt
    assert "`crawl_more(url)`" not in prompt


def test_natural_sample_keeps_the_crawl_more_hint() -> None:
    prompt = build_system_prompt(
        _ctx_with_source(
            _source(platform="tiki", sampling={"strategy": "natural", "provider": None})
        )
    )

    assert "`crawl_more(url)`" in prompt
    assert "QUY TẮC MẪU ĐÁNH GIÁ CHỌN LỌC" not in prompt


def test_paid_platform_never_offers_crawl_more_even_without_sampling() -> None:
    prompt = build_system_prompt(_ctx_with_source(_source(platform="shopee", sampling=None)))

    assert "`crawl_more(url)`" not in prompt


def test_no_tool_description_mentions_a_specific_marketplace() -> None:
    for tool in build_agent_tools().values():
        spec = tool.to_spec()
        blob = f"{spec.name} {spec.description} {spec.parameters}".lower()
        assert "tiki" not in blob, spec.name
        assert "shopee" not in blob, spec.name
        assert "ti.ki" not in blob, spec.name


# ---------------------------------------------------------------------------
# crawl_more: paid platforms are never enqueued
# ---------------------------------------------------------------------------


class _SubmitForbidden:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        raise AssertionError("crawl_more must not enqueue a paid source")


@pytest.fixture
def submit_spy(monkeypatch: pytest.MonkeyPatch) -> _SubmitForbidden:
    import app.agent.tools as tools_module

    spy = _SubmitForbidden()
    monkeypatch.setattr(tools_module, "submit_parsed_ingest", spy)
    return spy


async def test_crawl_more_refuses_shopee_and_does_not_enqueue(submit_spy: Any) -> None:
    ctx = _ctx_with_source(_source(platform="shopee"))

    result = await _run_crawl_more(
        ctx, CrawlMoreArgs(url="https://shopee.vn/product/1937969974/48571216193")
    )

    assert result.ok is False
    assert "paid_source_requires_user" in (result.error or "")
    assert "Shopee" in (result.error or "")
    assert "tự dán" in (result.error or "")
    assert submit_spy.calls == 0
    assert ctx.crawl_turn_count == 0
    assert ctx.crawl_session_count == 0


async def test_crawl_more_refuses_shopee_short_links_too(submit_spy: Any) -> None:
    ctx = _ctx_with_source(_source(platform="shopee"))

    result = await _run_crawl_more(ctx, CrawlMoreArgs(url="https://shp.ee/abc123"))

    assert result.ok is False
    assert submit_spy.calls == 0


async def test_crawl_more_still_enqueues_a_free_tiki_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.agent.tools as tools_module
    from app.jobs.submit import IngestSubmitResult

    calls: list[Any] = []

    async def _submit(parsed: Any, **kwargs: Any) -> IngestSubmitResult:
        calls.append(parsed)
        return IngestSubmitResult(cached=False, canonical_url="u", job_id="job-1")

    monkeypatch.setattr(tools_module, "submit_parsed_ingest", _submit)
    ctx = _ctx_with_source(_source(platform="tiki"))

    result = await _run_crawl_more(ctx, CrawlMoreArgs(url="https://tiki.vn/a-p1001.html"))

    assert result.ok is True
    assert result.data["status"] == "queued"
    assert len(calls) == 1
    assert ctx.crawl_turn_count == 1
