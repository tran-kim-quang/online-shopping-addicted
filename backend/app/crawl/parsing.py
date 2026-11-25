"""HTML-to-text cleaning and Vietnamese e-commerce count parsing helpers."""

from __future__ import annotations

import re
from html.parser import HTMLParser

_IGNORED_TAGS = frozenset({"script", "style", "noscript", "template"})
_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }
)

_COUNT_RE = re.compile(
    r"(?P<sign>[-−])?(?P<num>\d+(?:[.,]\d+)*)\s*"
    r"(?P<unit>triệu|trieu|nghìn|nghin|ngàn|ngan|tỷ|ty|tr|k|m|b)?\+?",
    re.IGNORECASE,
)

_UNIT_MULTIPLIERS: dict[str, int] = {
    "k": 1_000,
    "nghìn": 1_000,
    "nghin": 1_000,
    "ngàn": 1_000,
    "ngan": 1_000,
    "tr": 1_000_000,
    "triệu": 1_000_000,
    "trieu": 1_000_000,
    "m": 1_000_000,
    "b": 1_000_000_000,
    "tỷ": 1_000_000_000,
    "ty": 1_000_000_000,
}

_THOUSANDS_GROUP_RE = re.compile(r"^\d{1,3}(?:([.,])\d{3})+$")


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._ignore_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lower = tag.lower()
        if lower in _IGNORED_TAGS:
            self._ignore_depth += 1
            return
        if self._ignore_depth > 0:
            return
        if lower in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        lower = tag.lower()
        if lower in _IGNORED_TAGS:
            if self._ignore_depth > 0:
                self._ignore_depth -= 1
            return
        if self._ignore_depth > 0:
            return
        if lower in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lower = tag.lower()
        if self._ignore_depth > 0:
            return
        if lower in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignore_depth > 0:
            return
        self._parts.append(data)

    def get_text(self) -> str:
        raw = "".join(self._parts)
        raw = raw.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in raw.split("\n")]
        cleaned: list[str] = []
        blank_run = 0
        for line in lines:
            if not line:
                blank_run += 1
                if blank_run == 1 and cleaned:
                    cleaned.append("")
            else:
                blank_run = 0
                cleaned.append(line)
        return "\n".join(cleaned).strip()


def html_to_text(html: str | None) -> str:
    """Convert HTML markup into clean plain text while preserving logical line breaks.

    Strips ``<script>``, ``<style>``, ``<noscript>``, and ``<template>`` content.
    Returns an empty string for ``None`` or blank inputs.
    """
    if not html or not html.strip():
        return ""
    parser = _HTMLTextExtractor()
    parser.feed(html)
    parser.close()
    return parser.get_text()


def parse_vn_count(value: str | int | float | None) -> int | None:
    """Parse Vietnamese/English e-commerce counts into an integer.

    Examples:
        - ``"1,2k"`` / ``"1.2k"`` → ``1200``
        - ``"10k+"`` → ``10000``
        - ``"Đã bán 1.234"`` → ``1234``
        - ``"1tr"`` / ``"5.6tr+"`` / ``"2,5 triệu"`` → ``1000000``, ``5600000``, ``2500000``
        - ``""`` / ``None`` → ``None``
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(round(value)) if value >= 0 else None

    cleaned = value.strip()
    if not cleaned:
        return None

    match = _COUNT_RE.search(cleaned)
    if match is None:
        return None

    if match.group("sign"):
        return None

    num_str = match.group("num")
    unit_raw = match.group("unit")
    unit = unit_raw.lower() if unit_raw else None

    if unit is not None:
        multiplier = _UNIT_MULTIPLIERS[unit]
        sep_count = num_str.count(".") + num_str.count(",")
        if sep_count > 1:
            # Multiple separators are only unambiguous when matching thousands groups
            # (e.g. "1.000.000k").
            if _THOUSANDS_GROUP_RE.match(num_str):
                normalized = num_str.replace(".", "").replace(",", "")
                return int(normalized) * multiplier
            return None
        if sep_count == 1:
            sep = "." if "." in num_str else ","
            _left, right = num_str.split(sep, 1)
            # Forms like "1.000k" or "1,000k" are ambiguous between 1000*1000 and 1.0*1000.
            if len(right) == 3:
                return None
        normalized = num_str.replace(",", ".")
        return int(round(float(normalized) * multiplier))

    # No multiplier unit: treat `.` / `,` thousands groups as integer separators.
    if _THOUSANDS_GROUP_RE.match(num_str):
        return int(num_str.replace(".", "").replace(",", ""))

    if "." in num_str or "," in num_str:
        # E.g. "1.234.567" or fallback decimal like "4.5"
        if num_str.count(".") + num_str.count(",") > 1:
            return int(num_str.replace(".", "").replace(",", ""))
        sep = "." if "." in num_str else ","
        left, right = num_str.split(sep, 1)
        if len(right) == 3:
            return int(left + right)
        return int(round(float(f"{left}.{right}")))

    return int(num_str)
