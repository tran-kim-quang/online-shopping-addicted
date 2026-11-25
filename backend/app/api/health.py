"""Health check endpoint."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.cache.redis import RedisClient
from app.db.database import Database

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """Status payload for ``GET /health``."""

    status: str
    db: bool
    redis: bool


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Check Postgres and Redis connectivity."""
    db_ok = await Database().ping()
    redis_ok = await RedisClient().ping()
    return HealthResponse(
        status="ok" if (db_ok and redis_ok) else "degraded",
        db=db_ok,
        redis=redis_ok,
    )
