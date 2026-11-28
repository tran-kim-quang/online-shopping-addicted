"""Shared helper for constructing ``LLMConfig`` from an ``LLMProfile``."""

from __future__ import annotations

from pydantic import SecretStr

from app.core.config import Settings
from app.db.models import LLMProfile
from app.llm.types import LLMConfig
from app.security.cookies import SYSTEM_USER_ID
from app.security.secrets import SecretBox


def build_llm_config(
    profile: LLMProfile,
    settings: Settings,
    *,
    secret_box: SecretBox | None = None,
) -> LLMConfig:
    """Build an ``LLMConfig`` from an ``LLMProfile``, attaching quota metadata when shared."""
    raw_key: str | None = None
    if profile.api_key_enc:
        box = secret_box if secret_box is not None else SecretBox()
        raw_key = box.decrypt(profile.api_key_enc)

    is_system = profile.user_id == SYSTEM_USER_ID
    daily_limit: float | None = None
    if is_system and profile.daily_limit is not None:
        daily_limit = float(profile.daily_limit)

    return LLMConfig(
        base_url=profile.base_url,
        api_key=SecretStr(raw_key) if raw_key else None,
        chat_model=profile.chat_model,
        small_model=profile.small_model,
        embedding_model=profile.embedding_model,
        timeout=settings.LLM_TIMEOUT_SECONDS,
        max_retries=settings.LLM_MAX_RETRIES,
        usage_kind="llm" if is_system else None,
        daily_limit=daily_limit,
    )
