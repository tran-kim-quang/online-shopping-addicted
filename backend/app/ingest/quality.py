"""Pure deterministic review quality scoring used during crawl/ingest across all platforms."""

from __future__ import annotations

import math
import re
import unicodedata
from typing import Any

_WHITESPACE_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)

_GENERIC_FILLER_PHRASES = frozenset(
    {
        "ok",
        "oke",
        "okela",
        "tot",
        "rat tot",
        "qua tot",
        "tam on",
        "on",
        "good",
        "nice",
        "dep",
        "rat dep",
        "cam on",
        "cam on shop",
        "san pham tot",
        "hang dep",
        "giao nhanh",
        "giao hang nhanh",
        "dung nhu mo ta",
        "chat luong tot",
    }
)


def _normalize_for_filler_check(text: str) -> str:
    replaced = text.replace("đ", "d").replace("Đ", "D")
    decomposed = unicodedata.normalize("NFKD", replaced)
    ascii_text = decomposed.encode("ascii", "ignore").decode("ascii").lower()
    no_punct = _PUNCT_RE.sub(" ", ascii_text)
    return _WHITESPACE_RE.sub(" ", no_punct).strip()


def compute_review_quality_score(
    *,
    title: str | None = None,
    content: str | None = None,
    media_count: int = 0,
    variant: str | None = None,
    helpful_count: int | None = None,
) -> float:
    """Compute a normalized ``[0.0, 1.0]`` informativeness score for a review.

    Formula (additive components clamped to ``[0.0, 1.0]``):
      - **Text length** (up to ``0.50``): ``min(1.0, len(text) / 300) * 0.50``.
      - **Media** (up to ``0.20``): ``0.20`` when ``media_count > 0``, else ``0.0``.
      - **Variant** (up to ``0.10``): ``0.10`` when non-empty ``variant`` is set, else ``0.0``.
      - **Helpful votes** (up to ``0.20``): ``min(0.20, log1p(helpful) / log1p(20) * 0.20)``.
      - **Low-information penalty** (``-0.35``): applied when text is missing/very short
        (``< 12`` chars) or matches common generic/repetitive filler phrases (e.g. ``"ok"``,
        ``"tốt"``) or collapses to <= 2 unique tokens.
    """
    parts = [p.strip() for p in (title or "", content or "") if p and p.strip()]
    combined = " ".join(parts)
    char_len = len(combined)

    length_score = min(1.0, char_len / 300.0) * 0.50
    media_score = 0.20 if (media_count or 0) > 0 else 0.0
    variant_score = 0.10 if (variant and variant.strip()) else 0.0
    helpful = max(0, int(helpful_count or 0))
    helpful_score = min(0.20, (math.log1p(helpful) / math.log1p(20)) * 0.20)

    penalty = 0.0
    normalized = _normalize_for_filler_check(combined)
    tokens = normalized.split() if normalized else []
    is_repetitive = bool(tokens and len(set(tokens)) <= 2 and len(tokens) >= 3)
    if char_len < 12 or normalized in _GENERIC_FILLER_PHRASES or is_repetitive:
        penalty = 0.35

    raw_score = length_score + media_score + variant_score + helpful_score - penalty
    return round(max(0.0, min(1.0, raw_score)), 4)


def score_review_object(review: Any) -> float:
    """Compute quality score from a :class:`Review` ORM row or :class:`ReviewData` schema."""
    images = getattr(review, "images", None) or []
    raw_media = getattr(review, "media_count", None)
    media_count = int(raw_media) if raw_media is not None else len(images)
    return compute_review_quality_score(
        title=getattr(review, "title", None),
        content=getattr(review, "content", None),
        media_count=media_count,
        variant=getattr(review, "variant", None),
        helpful_count=getattr(review, "helpful_count", None),
    )
