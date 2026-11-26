"""Unit tests for count parsing and HTML-to-text helpers (``app.crawl.parsing``)."""

from __future__ import annotations

import pytest

from app.crawl.parsing import html_to_text, parse_vn_count


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1,2k", 1200),
        ("1.2k", 1200),
        ("10k+", 10000),
        ("4k", 4000),
        ("Đã bán 1.234", 1234),
        ("Đã bán 122583", 122583),
        ("1tr", 1000000),
        ("5.6tr+", 5600000),
        ("2,5 triệu", 2500000),
        ("3 triệu", 3000000),
        ("15 nghìn", 15000),
        ("2 ngàn", 2000),
        ("1 tỷ", 1000000000),
        ("1b", 1000000000),
        ("1.234.567", 1234567),
        ("10000+", 10000),
        ("0", 0),
    ],
)
def test_parse_vn_count_text_forms(raw: str, expected: int) -> None:
    assert parse_vn_count(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "abc", "Đã bán", "k", "+"])
def test_parse_vn_count_unparsable_returns_none(raw: str | None) -> None:
    assert parse_vn_count(raw) is None


def test_parse_vn_count_passthrough_numbers() -> None:
    assert parse_vn_count(4486) == 4486
    assert parse_vn_count(0) == 0
    assert parse_vn_count(12.7) == 13
    assert parse_vn_count(4.4) == 4


def test_parse_vn_count_rejects_bools_and_negatives() -> None:
    assert parse_vn_count(True) is None
    assert parse_vn_count(False) is None
    assert parse_vn_count(-5) is None
    assert parse_vn_count(-5.5) is None


def test_parse_vn_count_strips_surrounding_whitespace() -> None:
    assert parse_vn_count("  7,5k  ") == 7500


def test_html_to_text_strips_script_style_and_noscript() -> None:
    html = (
        "<script>var leak = 'secret';</script>"
        "<style>.a { color: red }</style>"
        "<noscript>enable js</noscript>"
        "<p>Nội dung</p>"
    )

    assert html_to_text(html) == "Nội dung"


def test_html_to_text_keeps_logical_line_breaks_between_blocks() -> None:
    text = html_to_text("<div><p>Dòng một</p><p>Dòng hai</p></div>")

    assert [line for line in text.splitlines() if line] == ["Dòng một", "Dòng hai"]


def test_html_to_text_br_becomes_newline() -> None:
    assert html_to_text("A<br/>B<br>C") == "A\nB\nC"


def test_html_to_text_preserves_explicit_blank_line() -> None:
    assert html_to_text("<div>x<br><br>y</div>") == "x\n\ny"


def test_html_to_text_keeps_inline_markup_on_one_line() -> None:
    assert html_to_text("<p>Hello <b>bold</b> <i>world</i></p>") == "Hello bold world"


def test_html_to_text_decodes_entities_and_nbsp() -> None:
    assert html_to_text("  &nbsp; Gạo &amp; nếp  ") == "Gạo & nếp"


def test_html_to_text_lists_become_separate_lines() -> None:
    text = html_to_text("<ul><li>một</li><li>hai</li></ul>")

    assert [line for line in text.splitlines() if line] == ["một", "hai"]


def test_html_to_text_collapses_runs_of_blank_lines() -> None:
    assert html_to_text("<p>a</p>\n\n\n\n<p>b</p>") == "a\n\nb"


def test_html_to_text_cleans_html_inside_spec_values() -> None:
    assert html_to_text("\n<p><span>100% gạo trắng</span></p>") == "100% gạo trắng"


@pytest.mark.parametrize("raw", [None, "", "   ", "\n\t "])
def test_html_to_text_blank_inputs(raw: str | None) -> None:
    assert html_to_text(raw) == ""


def test_html_to_text_handles_plain_text() -> None:
    assert html_to_text("một\ndòng") == "một\ndòng"


def test_html_to_text_tolerates_unclosed_tags() -> None:
    assert html_to_text("<p>a<p>b") == "a\nb"


def test_html_to_text_self_closing_block_tag_yields_nothing() -> None:
    assert html_to_text("<br/>") == ""
    assert html_to_text("<div><br/></div>") == ""


def test_html_to_text_ignores_self_closing_tags_inside_ignored_blocks() -> None:
    # ``<template>`` (unlike ``<script>``) is parsed as markup, so nested tags
    # exercise the ignore-depth guards for start/end/self-closing tags.
    assert html_to_text("<template><p>x</p><br/></template>after") == "after"


def test_parse_vn_count_handles_grouped_thousands_with_unit() -> None:
    assert parse_vn_count("1.000.000k") == 1000000000
    assert parse_vn_count("2.500.000") == 2500000
