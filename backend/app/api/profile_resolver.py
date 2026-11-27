"""Shared profile resolution with ownership and admin-shared checks."""

from __future__ import annotations

import uuid

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import SystemProfileDisabledError
from app.db.models import DataProviderProfile, LLMProfile
from app.security.cookies import SYSTEM_USER_ID


async def resolve_llm_profile(
    session: AsyncSession,
    profile_id: uuid.UUID,
    user_id: str,
) -> LLMProfile:
    """Resolve an ``LLMProfile`` owned by ``user_id`` or an enabled ``system`` profile."""
    profile = await session.get(LLMProfile, profile_id)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="LLM profile not found.",
        )
    if profile.user_id == user_id:
        return profile
    if profile.user_id == SYSTEM_USER_ID:
        if profile.shared_enabled:
            return profile
        raise SystemProfileDisabledError(kind="llm")
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="LLM profile not found.",
    )


async def resolve_data_provider_profile(
    session: AsyncSession,
    profile_id: uuid.UUID,
    user_id: str,
) -> DataProviderProfile:
    """Resolve a ``DataProviderProfile`` owned by ``user_id`` or an enabled ``system`` profile."""
    profile = await session.get(DataProviderProfile, profile_id)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Data provider profile not found.",
        )
    if profile.user_id == user_id:
        return profile
    if profile.user_id == SYSTEM_USER_ID:
        if profile.shared_enabled:
            return profile
        raise SystemProfileDisabledError(kind=profile.provider)
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Data provider profile not found.",
    )
