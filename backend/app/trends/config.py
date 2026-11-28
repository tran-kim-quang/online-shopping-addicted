"""Fixed configuration constants for the TikTok brand trends pipeline."""

from __future__ import annotations

WINDOW_DAYS = 30
SEARCH_PAGES = 3
COMMENT_VIDEOS = 5
COMMENT_PAGES_PER_VIDEO = 1
RECENCY_HALF_LIFE_DAYS = 10
TOP_BRANDS = 3
ROLE_WEIGHTS: dict[str, float] = {"primary": 1.0, "mentioned": 0.3}
SEARCH_REGION = "VN"
# Extra attempts for a batch the provider rejected with 429 (the wait comes from the
# provider's retry-after hint; the LLM client also paces itself on its headers).
LLM_RATE_LIMIT_RETRIES = 2
LLM_RATE_LIMIT_MAX_WAIT = 60.0
EVIDENCE_VIDEOS = 3
LLM_BATCH_SIZE = 40
# Token diet for the extraction prompt (free LLM tiers allow only a few thousand
# tokens per minute): trimmed texts, no near-empty comments, duplicates sent once.
VIDEO_TEXT_MAX_CHARS = 600
COMMENT_TEXT_MAX_CHARS = 300
COMMENT_MIN_LETTERS = 4
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
