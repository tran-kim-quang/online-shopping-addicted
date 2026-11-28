"""Public system-options endpoint (``GET /api/system-options``)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import client_ip, get_session, get_settings, get_user_id
from app.core.config import Settings
from app.core.system_usage import SystemUsage
from app.db.models import DataProviderProfile, LLMProfile
from app.security.cookies import SYSTEM_USER_ID

router = APIRouter(prefix="/api", tags=["system"])


class SystemLLMOption(BaseModel):
    """Availability and quota summary for the admin-shared LLM configuration."""

    available: bool = False
    profile_id: uuid.UUID | None = None
    name: str | None = None
    chat_model: str | None = None
    capabilities: dict[str, Any] | None = None
    remaining_today: float | None = None
    chat_daily_limit: int | None = None
    chat_remaining_today: int | None = None


class SystemApifyOption(BaseModel):
    """Availability and quota summary for the admin-shared Apify configuration."""

    available: bool = False
    profile_id: uuid.UUID | None = None
    max_cost_usd_per_link: float | None = None
    remaining_today: float | None = None


class SystemScrapeCreatorsOption(BaseModel):
    """Availability and quota summary for the admin-shared ScrapeCreators configuration."""

    available: bool = False
    profile_id: uuid.UUID | None = None
    max_credits_per_search: int | None = None
    remaining_today: float | None = None


class SystemOptionsResponse(BaseModel):
    """Response payload for ``GET /api/system-options``."""

    llm: SystemLLMOption
    apify: SystemApifyOption
    scrapecreators: SystemScrapeCreatorsOption


def _remaining(daily_limit: Any, used_today: float) -> float | None:
    if daily_limit is None:
        return None
    return round(max(0.0, float(daily_limit) - used_today), 4)


@router.get("/system-options", response_model=SystemOptionsResponse)
async def get_system_options(
    request: Request,
    user_id: str = Depends(get_user_id),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> SystemOptionsResponse:
    """Return whether admin-shared LLM, Apify, and ScrapeCreators configs are available."""
    usage = SystemUsage()

    llm_row = (
        await session.execute(
            select(LLMProfile).where(LLMProfile.user_id == SYSTEM_USER_ID).limit(1)
        )
    ).scalar_one_or_none()
    provider_rows = (
        (
            await session.execute(
                select(DataProviderProfile).where(DataProviderProfile.user_id == SYSTEM_USER_ID)
            )
        )
        .scalars()
        .all()
    )
    by_provider = {row.provider: row for row in provider_rows}

    if llm_row is not None and llm_row.shared_enabled:
        llm_used = await usage.used("llm")
        per_user = settings.CHAT_SYSTEM_DAILY_PER_USER
        per_ip = settings.CHAT_SYSTEM_DAILY_PER_IP
        used_user = await usage.used_key(usage.chat_user_key(user_id))
        used_ip = await usage.used_key(usage.chat_ip_key(client_ip(request, settings)))
        chat_remaining = max(0, int(min(per_user - used_user, per_ip - used_ip)))
        llm_opt = SystemLLMOption(
            available=True,
            profile_id=llm_row.id,
            name=llm_row.name,
            chat_model=llm_row.chat_model,
            capabilities=dict(llm_row.capabilities or {}),
            remaining_today=_remaining(llm_row.daily_limit, llm_used),
            chat_daily_limit=per_user,
            chat_remaining_today=chat_remaining,
        )
    else:
        llm_opt = SystemLLMOption(available=False)

    apify_row = by_provider.get("apify")
    if apify_row is not None and apify_row.shared_enabled:
        apify_used = await usage.used("apify")
        apify_opt = SystemApifyOption(
            available=True,
            profile_id=apify_row.id,
            max_cost_usd_per_link=round(float(apify_row.max_cost_usd_per_link), 2),
            remaining_today=_remaining(apify_row.daily_limit, apify_used),
        )
    else:
        apify_opt = SystemApifyOption(available=False)

    sc_row = by_provider.get("scrapecreators")
    if sc_row is not None and sc_row.shared_enabled:
        sc_used = await usage.used("scrapecreators")
        sc_opt = SystemScrapeCreatorsOption(
            available=True,
            profile_id=sc_row.id,
            max_credits_per_search=sc_row.max_credits_per_search,
            remaining_today=_remaining(sc_row.daily_limit, sc_used),
        )
    else:
        sc_opt = SystemScrapeCreatorsOption(available=False)

    return SystemOptionsResponse(
        llm=llm_opt,
        apify=apify_opt,
        scrapecreators=sc_opt,
    )
