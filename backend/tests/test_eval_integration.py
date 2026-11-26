"""Integration test: run ``scripts/eval_agent.py`` end-to-end against the mock provider.

Uses the dedicated Phase-4 test database (``shopqa_phase4_test``) and a scripted in-process
mock OpenAI app injected through ``run_eval(http_client=...)``: no real LLM, no network, and
nothing is written to ``shopqa`` or the eval database that holds real results.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from scripts.eval_agent import main, run_eval
from tests.mock_openai import MockBehavior, build_mock_app
from tests.test_phase4_integration import (  # noqa: F401 - fixtures re-exported for pytest
    TEST_DATABASE_URL,
    TEST_DB_NAME,
    phase4_database_url,
)
from tests.test_phase4_integration import (
    session_factory as phase4_session_factory,  # noqa: F401 - used as a fixture below
)


@pytest.fixture
def session_factory(phase4_session_factory: Any) -> Any:  # noqa: F811
    """Expose the Phase-4 isolated-DB session factory under its pytest name."""
    return phase4_session_factory


pytestmark = pytest.mark.integration

BACKEND_DIR = Path(__file__).resolve().parents[1]
DATASET_PATH = BACKEND_DIR / "eval" / "dataset.yaml"
API_KEY = "sk-eval-mock-key"
ONLY_IDS = {"shop-top-sold-3", "prod-review-summary"}


@pytest.fixture
def mock_behavior() -> MockBehavior:
    behavior = MockBehavior()
    behavior.require_api_key = True
    behavior.expected_api_key = API_KEY
    behavior.supports_tools = True
    behavior.supports_json_mode = True
    # One tool call, then a grounded-ish answer; the point is exercising the pipeline,
    # not scoring quality.
    behavior.tool_call_script = [
        [{"name": "query_products", "args": {"sort_by": "sold_count", "limit": 3}}],
        [{"name": "summarize_reviews", "args": {"product_id": "P1"}}],
        [],
    ]
    # The probe reports no native tool calling for this mock, so the agent runs the
    # ReAct-JSON strategy: every reply must be one JSON object.
    behavior.scripted_texts = [
        '{"tool": "query_products", "args": {"sort_by": "sold_count", "limit": 3}}',
        '{"tool": "summarize_reviews", "args": {"product_id": "P1"}}',
        '{"final": "Top 3 sản phẩm bán chạy nhất là các sản phẩm trong shop [P1][P2][P3]."}',
    ]
    return behavior


@pytest.fixture
def patched_probe(monkeypatch: Any) -> None:
    """Skip the (separately tested) capability probe so mock replies are not consumed by it."""
    import scripts.eval_agent as eval_module
    from app.llm.types import Capabilities

    async def _probe(*_args: Any, **_kwargs: Any) -> Capabilities:
        return Capabilities(chat=True, tool_calling=False, streaming=False, json_mode=True)

    monkeypatch.setattr(eval_module, "probe", _probe)


async def test_eval_pipeline_runs_end_to_end_on_the_mock(
    tmp_path: Path,
    mock_behavior: MockBehavior,
    session_factory: Any,
    patched_probe: None,
) -> None:
    mock_app = build_mock_app(mock_behavior)
    http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_app), base_url="http://mock"
    )
    try:
        result = await run_eval(
            base_url="http://mock/v1",
            api_key=API_KEY,
            model="mock-chat",
            small_model=None,
            judge_model=None,
            database_url=TEST_DATABASE_URL,
            dataset_path=DATASET_PATH,
            only_ids=ONLY_IDS,
            embedder_choice="fake",
            skip_seed=False,
            allow_main_db=False,
            results_dir=tmp_path,
            http_client=http_client,
        )
    finally:
        await http_client.aclose()

    summary = result["summary"]
    assert summary["cases_total"] == len(ONLY_IDS)
    assert summary["model"] == "mock-chat"
    assert summary["tool_accuracy"] >= 0.0
    assert 0.0 <= summary["check_pass_rate"] <= 1.0
    assert "avg_latency_ms" in summary

    cases = {case["id"]: case for case in result["cases"]}
    assert set(cases) == ONLY_IDS
    for case in cases.values():
        assert case["answer"], "every case must produce an answer"
        assert case["error"] is None
        assert case["checks"], "every case is graded"

    # The first case consumes the scripted tool calls; its trace and raw tool outputs must be
    # saved so the report can later be regraded offline.
    first = cases["shop-top-sold-3"]
    assert first["tool_calls"], "the scripted tool call must be traced"
    assert first["tool_calls"][0]["name"] == "query_products"
    assert first["tool_outputs"], "tool outputs are persisted for regrading"
    assert first["tool_ok"] is True
    assert first["invalid_citations"] == 0

    json_files = list(tmp_path.glob("*.json"))
    md_files = list(tmp_path.glob("*.md"))
    assert len(json_files) == 1 and len(md_files) == 1
    saved = json.loads(json_files[0].read_text(encoding="utf-8"))
    assert saved["summary"]["cases_total"] == len(ONLY_IDS)
    assert API_KEY not in json_files[0].read_text(encoding="utf-8")


async def test_eval_seeds_the_dedicated_database_only(
    mock_behavior: MockBehavior, session_factory: Any, tmp_path: Path, patched_probe: None
) -> None:
    async with session_factory() as session:
        current = (await session.execute(text("SELECT current_database()"))).scalar_one()
        assert current == TEST_DB_NAME != "shopqa"

    mock_app = build_mock_app(mock_behavior)
    http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_app), base_url="http://mock"
    )
    try:
        await run_eval(
            base_url="http://mock/v1",
            api_key=API_KEY,
            model="mock-chat",
            small_model=None,
            judge_model=None,
            database_url=TEST_DATABASE_URL,
            dataset_path=DATASET_PATH,
            only_ids={"shop-top-sold-3"},
            embedder_choice="fake",
            skip_seed=False,
            allow_main_db=False,
            results_dir=tmp_path,
            http_client=http_client,
        )
    finally:
        await http_client.aclose()

    async with session_factory() as session:
        sources = list((await session.execute(text("SELECT id, url_type FROM sources"))).all())
        products = (await session.execute(text("SELECT count(*) FROM products"))).scalar_one()
        chunks = (await session.execute(text("SELECT count(*) FROM chunks"))).scalar_one()

    # ``eval/seed.py`` builds both eval sources (shop + single product).
    assert {row[1] for row in sources} == {"shop", "product"}
    assert products >= 6
    assert chunks >= 6


def test_eval_cli_refuses_the_main_database(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(
        [
            "--base-url",
            "http://127.0.0.1:9/v1",
            "--model",
            "mock-chat",
            "--database-url",
            "postgresql+asyncpg://shopqa:shopqa@localhost:5433/shopqa",
        ]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "Refusing to seed main database" in captured.err
    assert "--allow-main-db" in captured.err
