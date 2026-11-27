"""Fixed configuration constants for the TikTok brand trends pipeline."""

from __future__ import annotations

WINDOW_DAYS = 30
SEARCH_PAGES = 3
COMMENT_VIDEOS = 5
COMMENT_PAGES_PER_VIDEO = 1
RECENCY_HALF_LIFE_DAYS = 10
TOP_BRANDS = 3
# Seconds to wait before retrying an LLM batch that hit a provider rate limit (429).
LLM_RATE_LIMIT_BACKOFF = (5.0, 15.0, 30.0)
EVIDENCE_VIDEOS = 3
LLM_BATCH_SIZE = 20
CACHE_HOURS = 24
ESTIMATED_CREDITS = SEARCH_PAGES + COMMENT_VIDEOS * COMMENT_PAGES_PER_VIDEO

FEW_VIDEOS_THRESHOLD = 15

AD_HASHTAG_PREFIXES = (
    "hoptac",
    "taitro",
    "quangcao",
    "sponsored",
    "collab",
    "affiliate",
)
AD_HASHTAG_EXACT = frozenset({"ad", "ads"})
