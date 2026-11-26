"""Prompt-injection sanitization and untrusted-data delimiter helpers."""

from __future__ import annotations

import re

UNTRUSTED_START_DELIMITER = "<<<UNTRUSTED_START>>>"
UNTRUSTED_END_DELIMITER = "<<<UNTRUSTED_END>>>"

REVIEW_START_DELIMITER = "<<<REVIEW_START>>>"
REVIEW_END_DELIMITER = "<<<REVIEW_END>>>"


def _default_replacement(delimiter: str) -> str:
    core = delimiter.strip("<>").strip()
    return f"[{core}]" if core else "[DELIMITER]"


def sanitize_delimiters(
    text: str,
    *,
    start_delimiter: str = UNTRUSTED_START_DELIMITER,
    end_delimiter: str = UNTRUSTED_END_DELIMITER,
    start_replacement: str | None = None,
    end_replacement: str | None = None,
) -> str:
    """Neutralize literal (and case-variant) delimiter tokens inside untrusted text."""
    if not text:
        return ""
    start_rep = (
        start_replacement
        if start_replacement is not None
        else _default_replacement(start_delimiter)
    )
    end_rep = (
        end_replacement if end_replacement is not None else _default_replacement(end_delimiter)
    )

    sanitized = re.sub(re.escape(start_delimiter), start_rep, text, flags=re.IGNORECASE)
    sanitized = re.sub(re.escape(end_delimiter), end_rep, sanitized, flags=re.IGNORECASE)
    return sanitized.strip()


def wrap_untrusted(
    text: str | None,
    *,
    start_delimiter: str = UNTRUSTED_START_DELIMITER,
    end_delimiter: str = UNTRUSTED_END_DELIMITER,
    max_chars: int | None = None,
) -> str:
    """Sanitize, optionally truncate, and wrap untrusted text inside explicit delimiters."""
    cleaned = sanitize_delimiters(
        text or "",
        start_delimiter=start_delimiter,
        end_delimiter=end_delimiter,
    )
    if max_chars is not None and max_chars > 0 and len(cleaned) > max_chars:
        cleaned = cleaned[: max(1, max_chars - 1)].rstrip() + "…"
    return f"{start_delimiter}\n{cleaned}\n{end_delimiter}"
