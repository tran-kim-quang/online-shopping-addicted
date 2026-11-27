"""Helpers for per-user source and trend search history links."""

from __future__ import annotations

import uuid

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import UserSource, UserTrendSearch


async def upsert_user_source(
    session: AsyncSession,
    user_id: str,
    source_id: uuid.UUID,
) -> None:
    """Insert or touch ``user_sources(user_id, source_id)``."""
    stmt = (
        pg_insert(UserSource)
        .values(user_id=user_id, source_id=source_id)
        .on_conflict_do_update(
            index_elements=[UserSource.user_id, UserSource.source_id],
            set_={"last_seen": func.now()},
        )
    )
    await session.execute(stmt)


async def upsert_user_trend_search(
    session: AsyncSession,
    user_id: str,
    trend_search_id: uuid.UUID,
) -> None:
    """Insert or touch ``user_trend_searches(user_id, trend_search_id)``."""
    stmt = (
        pg_insert(UserTrendSearch)
        .values(user_id=user_id, trend_search_id=trend_search_id)
        .on_conflict_do_update(
            index_elements=[UserTrendSearch.user_id, UserTrendSearch.trend_search_id],
            set_={"last_seen": func.now()},
        )
    )
    await session.execute(stmt)
