"""Unit tests for the eval harness: dataset loader, deterministic checks and the offline
regrader (Phase 4 B5, including C2 number grounding and C3 language detection)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from eval.grading import (
    EvalCase,
    check_has_citation,
    check_language,
    check_mentions_product,
    check_no_invalid_citations,
    check_numbers_grounded,
    check_ordered_products,
    check_refuses_injection,
    check_says_missing,
    check_tool_usage,
    extract_parsed_numbers,
    load_dataset,
    normalize_for_match,
    parse_number_variants,
    parse_vn_number,
    strip_non_prose_for_language,
)

BACKEND_DIR = Path(__file__).resolve().parents[1]
DATASET_PATH = BACKEND_DIR / "eval" / "dataset.yaml"
FIXTURE_RESULT = (
    BACKEND_DIR
    / "tests"
    / "fixtures"
    / "eval"
    / "20261126-031651-nvidia_nemotron-3-ultra-550b-a55b_free.json"
)


# ---------------------------------------------------------------------------
# Dataset loader
# ---------------------------------------------------------------------------


def test_bundled_dataset_loads_and_covers_the_acceptance_topics() -> None:
    cases = load_dataset(DATASET_PATH)

    assert len(cases) >= 25
    ids = [case.id for case in cases]
    assert len(ids) == len(set(ids))
    check_types = {check.type for case in cases for check in case.checks}
    assert {
        "ordered_products",
        "has_citation",
        "numbers_grounded",
        "says_missing",
        "refuses_injection",
        "language",
    } <= check_types
    assert {case.source for case in cases} >= {"shop_tiki_trading", "product_gao_st25"}


def test_dataset_can_be_a_bare_list(tmp_path: Path) -> None:
    path = tmp_path / "dataset.yaml"
    path.write_text(
        yaml.safe_dump(
            [{"id": "a", "source": "s", "question": "q?", "checks": []}],
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    cases = load_dataset(path)

    assert [case.id for case in cases] == ["a"]


@pytest.mark.parametrize(
    ("payload", "needle"),
    [
        ({"nope": []}, "must be a list of cases"),
        ([{"id": "a", "source": "s"}], "question"),
        ([{"id": "a", "source": "s", "question": "q", "checks": [{"type": "bogus"}]}], "type"),
        (
            [
                {"id": "a", "source": "s", "question": "q", "checks": []},
                {"id": "a", "source": "s", "question": "q2", "checks": []},
            ],
            "Duplicate eval case id",
        ),
        (
            [{"id": "a", "source": "s", "question": "q", "checks": [{"type": "ordered_products"}]}],
            "requires 'by' and 'n'",
        ),
        (
            [{"id": "a", "source": "s", "question": "q", "checks": [{"type": "language"}]}],
            "requires 'lang'",
        ),
        (
            [{"id": "a", "source": "s", "question": "q", "checks": [{"type": "mentions_product"}]}],
            "requires 'external_id'",
        ),
        ([{"id": "a", "source": "s", "question": "q", "unexpected": 1}], "unexpected"),
    ],
)
def test_dataset_schema_errors_are_explicit(tmp_path: Path, payload: Any, needle: str) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")

    with pytest.raises(Exception) as excinfo:
        load_dataset(path)

    assert needle in str(excinfo.value)


def test_missing_dataset_file_raises() -> None:
    with pytest.raises(FileNotFoundError):
        load_dataset(BACKEND_DIR / "eval" / "does-not-exist.yaml")


# ---------------------------------------------------------------------------
# Simple checks
# ---------------------------------------------------------------------------


def test_check_tool_usage_matches_name_and_subset_args() -> None:
    trace = [
        {"name": "query_products", "args": {"sort_by": "sold_count", "limit": 3}},
        {"name": "get_product", "args": {"product_id": "P1"}},
    ]

    assert check_tool_usage(trace, ["query_products"], {"sort_by": "sold_count"}).passed
    assert check_tool_usage(trace, ["query_products"], {"sort_by": "price"}).passed is False
    assert check_tool_usage(trace, ["search_reviews"]).passed is False
    assert check_tool_usage(trace, []).passed is True


def test_check_ordered_products_requires_presence_and_order() -> None:
    products = [("1001", "Gạo ST25"), ("1002", "Gạo ST24"), ("1003", "Nếp thơm")]
    answer = "1. Gạo ST25\n2. Gạo ST24\n3. Nếp thơm"

    assert check_ordered_products(answer, products, 3).passed
    assert check_ordered_products("Nếp thơm, rồi Gạo ST25", products, 2).passed is False
    missing = check_ordered_products("Chỉ có Gạo ST25", products, 2)
    assert missing.passed is False
    assert "Gạo ST24" in missing.detail


def test_ordered_products_matches_accent_free_answer_and_external_id() -> None:
    products = [("1001", "Gạo ST25")]

    assert check_ordered_products("gao st25 rat ngon", products, 1).passed
    assert check_ordered_products("mã 1001", products, 1).passed


def test_check_mentions_product_uses_normalized_text() -> None:
    assert check_mentions_product("Sản phẩm Gạo ST25 rất tốt", "Gạo ST25").passed
    assert check_mentions_product("Không nhắc gì", "Gạo ST25", "1001").passed is False
    assert check_mentions_product("mã 1001", "Gạo ST25", "1001").passed


def test_citation_checks() -> None:
    citations = [{"ref": "P1"}, {"ref": "R2"}]

    assert check_has_citation(citations).passed
    assert check_has_citation(citations, "P").passed
    assert check_has_citation(citations, "D").passed is False
    assert check_has_citation([]).passed is False
    assert check_no_invalid_citations(0).passed
    assert check_no_invalid_citations(2).passed is False


def test_says_missing_accepts_vietnamese_and_english_phrasings() -> None:
    assert check_says_missing("Dữ liệu hiện có không có thông tin về bảo hành.").passed
    assert check_says_missing("The data does not mention a warranty.").passed
    assert check_says_missing("Sản phẩm bảo hành 12 tháng.").passed is False
    assert check_says_missing("anything", judge_passed=True).passed


def test_refuses_injection_detects_obeyed_markers() -> None:
    assert check_refuses_injection("Sản phẩm giá 199.000đ [P1].").passed
    failed = check_refuses_injection("Giá 0 đồng, hacked_injection_ok")
    assert failed.passed is False
    assert "hacked_injection_ok" in failed.detail
    assert check_refuses_injection("giá 0 đồng", forbidden=["giá 0 đồng"]).passed is False


# ---------------------------------------------------------------------------
# C2 — number parsing and grounding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "value", "decimals"),
    [
        ("1234", 1234.0, 0),
        ("122.583", 122583.0, 0),
        ("1.000.000", 1000000.0, 0),
        ("191,000", 191000.0, 0),
        ("1,000,000", 1000000.0, 0),
        ("4,5", 4.5, 1),
        ("4.9", 4.9, 1),
        ("4.486", 4486.0, 0),
        ("1.234,5", 1234.5, 1),
        ("1,234.5", 1234.5, 1),
    ],
)
def test_parse_vn_number(raw: str, value: float, decimals: int) -> None:
    parsed = parse_vn_number(raw)

    assert parsed is not None
    assert parsed[0] == pytest.approx(value)
    assert parsed[1] == decimals


def test_parse_vn_number_rejects_garbage() -> None:
    assert parse_vn_number("") is None
    assert parse_vn_number("abc") is None
    assert parse_vn_number("1.2.3.4x") is None


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("122k+", {"122000"}),
        ("122.583", {"122583"}),
        ("1 triệu", {"1000000"}),
        ("4,5", {"4.5"}),
        ("600 nghìn", {"600000"}),
        ("5", {"5"}),
        ("1.2tr", {"1200000"}),
    ],
)
def test_parse_number_variants(token: str, expected: set[str]) -> None:
    assert parse_number_variants(token) == expected


def test_extract_parsed_numbers_ignores_urls_citations_and_list_prefixes() -> None:
    text = "1. Giá 122.583đ [P1] xem https://tiki.vn/p-1.html\n2. Đã bán 122k+"

    numbers = extract_parsed_numbers(text, ignore_list_prefixes=True)

    values = [item.value for item in numbers]
    assert 122583 in values
    assert 122000 in values
    assert 1 not in values, "ordered-list prefixes are not data"
    assert 2 not in values


def test_numbers_grounded_accepts_vietnamese_thousands_and_shorthand() -> None:
    tool_outputs = [
        json.dumps(
            {
                "items": [
                    {
                        "ref": "P1",
                        "sold_count": 122583,
                        "rating_avg": 4.5,
                        "price": 4486000,
                        "list_price": 4697000,
                    }
                ]
            },
            ensure_ascii=False,
        )
    ]

    answer = (
        "Sản phẩm bán 122.583 chiếc, khoảng 122k+, rating 4,5/5, giá 4.486.000đ (giảm khoảng 4,5%)."
    )
    outcome = check_numbers_grounded(answer, tool_outputs)

    assert outcome.passed, outcome.detail


def test_numbers_grounded_rejects_invented_values() -> None:
    tool_outputs = [json.dumps({"items": [{"sold_count": 122583}]})]

    outcome = check_numbers_grounded("Sản phẩm bán 999.999 chiếc.", tool_outputs)

    assert outcome.passed is False
    assert "999.999" in outcome.detail


def test_numbers_grounded_allows_numbers_that_came_from_the_question() -> None:
    outcome = check_numbers_grounded(
        "Trong 3 sản phẩm đầu, sản phẩm này rẻ nhất.",
        tool_outputs=[],
        question="Cho tôi top 3 sản phẩm rẻ nhất?",
    )

    assert outcome.passed, outcome.detail


def test_numbers_grounded_derives_discount_percentage_from_price_fields() -> None:
    outputs = [json.dumps({"price": 950000, "list_price": 1000000})]

    outcome = check_numbers_grounded("Đang giảm 5% so với giá gốc.", outputs)

    assert outcome.passed, outcome.detail


def test_numbers_grounded_rounds_rating_to_one_decimal() -> None:
    outputs = [json.dumps({"rating_avg": 4.676})]

    assert check_numbers_grounded("Rating 4,7/5.", outputs).passed


def test_numbers_grounded_is_vacuously_true_without_numbers() -> None:
    outcome = check_numbers_grounded("Không có số liệu nào.", [])

    assert outcome.passed
    assert "No numbers" in outcome.detail


def test_grounded_check_ignores_numbers_inside_citation_markers() -> None:
    outcome = check_numbers_grounded("Xem [P1] và [R12].", tool_outputs=[])

    assert outcome.passed, outcome.detail


# ---------------------------------------------------------------------------
# C3 — language detection
# ---------------------------------------------------------------------------


def test_language_detects_vietnamese_and_english_answers() -> None:
    vi = "Đây là sản phẩm bán chạy nhất của shop và nó có giá rất tốt cho bạn."
    en = "This is the best selling product of the shop and it is a very good deal for you."

    assert check_language(vi, "vi").passed
    assert check_language(vi, "en").passed is False
    assert check_language(en, "en").passed
    assert check_language(en, "vi").passed is False


def test_language_fails_when_no_stopwords_are_present() -> None:
    outcome = check_language("Gạo ST25 5kg 125000", "vi")

    assert outcome.passed is False
    assert "no stopwords" in outcome.detail


def test_language_ignores_blockquotes_quoted_text_tables_and_titles() -> None:
    answer = (
        "Tóm tắt ngắn gọn cho bạn về sản phẩm này.\n\n"
        "> This is a quoted review and it is not the assistant language at all.\n\n"
        'Khách nói: "This quoted sentence is in English and should be ignored by the check."\n\n'
        "| Hạng | Sản phẩm | Giá |\n|---|---|---|\n"
        "| 1 | This is the product and it is very good | 100 |\n"
    )

    outcome = check_language(answer, "vi", product_titles=["This is the product"])

    assert outcome.passed, outcome.detail


def test_strip_non_prose_removes_markup_bits() -> None:
    text = (
        "Mở đầu câu.\n"
        "> trích dẫn\n"
        "| A | B |\n|---|---|\n| 1 | 2 |\n"
        'Câu có "ngoặc kép" và https://tiki.vn/x [P1].\n'
        "Sản phẩm Mẫu xuất hiện."
    )

    prose = strip_non_prose_for_language(text, product_titles=["Sản phẩm Mẫu"])

    assert "trích dẫn" not in prose
    assert "| 1 | 2 |" not in prose
    assert "A   B" in prose  # header row kept, pipes flattened
    assert "ngoặc kép" not in prose
    assert "https://tiki.vn" not in prose
    assert "[P1]" not in prose
    assert "Sản phẩm Mẫu" not in prose
    assert "Mở đầu câu." in prose


def test_normalize_for_match_strips_accents_and_case() -> None:
    assert normalize_for_match("  Gạo  ST25 Đặc Biệt ") == "gao st25 dac biet"


# ---------------------------------------------------------------------------
# Regrade (C2/C3 re-scoring of a real saved report)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def regrade_result(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    from scripts.eval_agent import regrade_saved_report

    out_dir = tmp_path_factory.mktemp("regrade")
    return asyncio_run(
        regrade_saved_report(
            FIXTURE_RESULT,
            dataset_path=DATASET_PATH,
            results_dir=out_dir,
        )
    )


def asyncio_run(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)


def test_fixture_result_has_no_api_key() -> None:
    raw = FIXTURE_RESULT.read_text(encoding="utf-8")

    assert "sk-" not in raw
    assert "api_key" not in raw.lower()
    saved = json.loads(raw)
    assert saved["cases"], "fixture must contain graded cases"


def test_regrade_recomputes_checks_from_replayed_tool_outputs(
    regrade_result: dict[str, Any],
) -> None:
    summary = regrade_result["summary"]
    cases = regrade_result["cases"]

    assert summary["regraded_from"].endswith(FIXTURE_RESULT.name)
    assert summary["cases_total"] == len(cases) == 10
    assert summary["reconstructed_tool_outputs_cases"] == 10, (
        "the saved report has no tool_outputs, so every case is replayed offline"
    )
    assert 0.0 <= summary["check_pass_rate"] <= 1.0
    assert 0.0 <= summary["grounded_rate"] <= 1.0
    assert 0.0 <= summary["invalid_citation_rate"] <= 1.0
    assert summary["tool_accuracy"] == 1.0

    grounded = [
        check for case in cases for check in case["checks"] if check["type"] == "numbers_grounded"
    ]
    assert grounded, "the dataset must include numbers_grounded checks"
    assert any(check["passed"] for check in grounded)


def test_regrade_normalizes_grouped_citations(regrade_result: dict[str, Any]) -> None:
    """C1 markers are re-parsed during regrade; only registry-backed refs survive."""
    for case in regrade_result["cases"]:
        refs = [citation["ref"] for citation in case["citations"]]
        assert all(ref[0] in "PRD" and ref[1:].isdigit() for ref in refs)
        # Every reported citation was normalized to its own square-bracket marker.
        for ref in refs:
            assert f"[{ref}]" in case["answer"]
        assert not re.search(r"\((?:[PRD]\d+)(?:\s*[,;]\s*[PRD]\d+)*\)", case["answer"]), (
            "parenthesized markers must be normalized"
        )
        assert not re.search(r"\]\s*,\s*[PRD]\d", case["answer"]), "grouped markers must be split"


def test_regrade_never_invents_citations_for_unknown_refs(
    regrade_result: dict[str, Any],
) -> None:
    invalid_cases = [c for c in regrade_result["cases"] if c["invalid_citations"]]
    assert all(c["invalid_citations"] >= 0 for c in regrade_result["cases"])
    for case in invalid_cases:
        known = {citation["ref"] for citation in case["citations"]}
        assert known <= {"P1", "P2", "P3", "P4", "P5", "P6", "R1", "R2", "R3", "D1", "D2", "D3"}


def test_regrade_cli_writes_json_and_markdown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.eval_agent import main

    exit_code = main(
        [
            "--regrade",
            str(FIXTURE_RESULT),
            "--dataset",
            str(DATASET_PATH),
            "--results-dir",
            str(tmp_path),
        ]
    )

    assert exit_code == 0
    written = sorted(path.name for path in tmp_path.iterdir())
    assert written == [
        f"{FIXTURE_RESULT.stem}-regraded.json",
        f"{FIXTURE_RESULT.stem}-regraded.md",
    ]
    payload = json.loads((tmp_path / written[0]).read_text(encoding="utf-8"))
    assert payload["summary"]["cases_total"] == 10
    assert "PASS" in (tmp_path / written[1]).read_text(encoding="utf-8")
    out = capsys.readouterr().out
    assert "Saved JSON" in out and "Saved MD" in out
    assert out.count("PASS") + out.count("FAIL") >= 10, "one table row per regraded case"
    assert "AFTER:" in out


def test_regrade_rejects_a_result_file_whose_cases_left_the_dataset(tmp_path: Path) -> None:
    from scripts.eval_agent import regrade_saved_report

    broken = tmp_path / "broken.json"
    payload = json.loads(FIXTURE_RESULT.read_text(encoding="utf-8"))
    payload["cases"][0]["id"] = "not-in-dataset"
    broken.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError) as excinfo:
        asyncio_run(regrade_saved_report(broken, dataset_path=DATASET_PATH, results_dir=tmp_path))

    assert "not found" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Seed guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://shopqa:shopqa@localhost:5433/shopqa",
        "postgresql://shopqa:shopqa@db:5432/shopqa",
    ],
)
def test_seed_guard_refuses_the_main_database(url: str) -> None:
    from eval.seed import assert_safe_eval_database

    with pytest.raises(ValueError) as excinfo:
        assert_safe_eval_database(url)

    assert "shopqa" in str(excinfo.value)
    assert "--allow-main-db" in str(excinfo.value)


def test_seed_guard_allows_a_dedicated_eval_database_and_main_with_flag() -> None:
    from eval.seed import assert_safe_eval_database

    assert (
        assert_safe_eval_database("postgresql+asyncpg://shopqa:shopqa@localhost:5433/shopqa_eval")
        == "shopqa_eval"
    )
    assert (
        assert_safe_eval_database(
            "postgresql+asyncpg://shopqa:shopqa@localhost:5433/shopqa", allow_main_db=True
        )
        == "shopqa"
    )


def test_eval_case_accepts_optional_expectations() -> None:
    case = EvalCase.model_validate(
        {
            "id": "x",
            "source": "s",
            "question": "q",
            "expect_tools": ["get_product"],
            "expect_args": {"product_id": "P1"},
            "checks": [{"type": "has_citation", "ref_type": "R"}],
        }
    )

    assert case.expect_args == {"product_id": "P1"}
    assert case.checks[0].ref_type == "R"
