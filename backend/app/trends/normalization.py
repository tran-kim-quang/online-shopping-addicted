"""Text normalization helpers for brand names, keywords, and ad hashtags."""

from __future__ import annotations

import re
import unicodedata

from app.trends.config import AD_HASHTAG_EXACT, AD_HASHTAG_PREFIXES


def strip_accents(text: str) -> str:
    """Remove diacritics and map Vietnamese ``đ``/``Đ`` to ``d``/``D``."""
    replaced = text.replace("đ", "d").replace("Đ", "D")
    normalized = unicodedata.normalize("NFD", replaced)
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


def normalize_brand_key(name: str) -> str:
    """Casefold, strip accents, and remove all non-alphanumeric characters."""
    ascii_text = strip_accents(name).casefold()
    return re.sub(r"[^a-z0-9]", "", ascii_text)


def normalize_keyword(keyword: str) -> str:
    """Casefold, strip accents, and collapse whitespace to single spaces."""
    ascii_text = strip_accents(keyword).casefold()
    cleaned = re.sub(r"[^a-z0-9]+", " ", ascii_text)
    return cleaned.strip()


def is_ad_hashtag(hashtag: str) -> bool:
    """Return True if a hashtag indicates advertising or paid partnership."""
    key = normalize_brand_key(hashtag)
    if not key:
        return False
    if key in AD_HASHTAG_EXACT:
        return True
    return any(key.startswith(prefix) for prefix in AD_HASHTAG_PREFIXES)
