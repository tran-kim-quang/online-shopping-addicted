"""Source overview report and streaming CSV export endpoints."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BeforeValidator, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session, get_settings
from app.core.config import Settings
from app.db.models import Product, Shop, Source
from app.ingest.pipeline import resolve_source_scope
from app.reports.csv_export import (
    build_csv_filename,
    iter_products_csv,
    iter_reviews_csv,
)
from app.reports.source_report import SourceReport, build_source_report

router = APIRouter(prefix="/api/sources", tags=["sources"])

_IN_PROGRESS_STATUSES = frozenset({"pending", "queued", "running"})


def _empty_to_none(value: Any) -> Any:
    if isinstance(value, str) and not value.strip():
        return None
    return value


OptionalSentiment = Annotated[
    Literal["positive", "neutral", "negative"] | None,
    BeforeValidator(_empty_to_none),
]
OptionalRating = Annotated[
    Annotated[int, Field(ge=1, le=5)] | None,
    BeforeValidator(_empty_to_none),
]


def _source_not_ready_response(source: Source) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={
            "error": "source_not_ready",
            "detail": (
                f"Source {source.id} is still in progress "
                f"(status='{source.status}') and has no crawled products yet."
            ),
        },
    )


async def _load_source_or_404(session: AsyncSession, source_id: uuid.UUID) -> Source:
    source = await session.get(Source, source_id)
    if source is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Source not found.",
        )
    return source


async def _load_ready_scope(
    session: AsyncSession,
    source: Source,
) -> tuple[Shop | None, list[Product]] | JSONResponse:
    shop, products = await resolve_source_scope(session, source)
    if source.status in _IN_PROGRESS_STATUSES and not products:
        return _source_not_ready_response(source)
    return shop, products


@router.get(
    "/{source_id}/report",
    response_model=SourceReport,
    responses={
        404: {"description": "Source not found."},
        409: {"description": "Source is still queued/running with no products yet."},
    },
)
async def get_source_report(
    source_id: uuid.UUID,
    top_n: int = Query(default=10, ge=1, le=50),
    session: AsyncSession = Depends(get_session),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> SourceReport | Response:
    """Return a deterministic SQL-aggregated data overview report for ``source_id``."""
    source = await _load_source_or_404(session, source_id)
    if source.status in _IN_PROGRESS_STATUSES:
        _shop, products = await resolve_source_scope(session, source)
        if not products:
            return _source_not_ready_response(source)

    return await build_source_report(
        session,
        source,
        top_n=top_n,
        settings=settings,
    )


@router.get(
    "/{source_id}/export/products.csv",
    responses={
        200: {
            "description": "UTF-8 BOM CSV export of all products in source scope.",
            "content": {"text/csv": {"schema": {"type": "string", "format": "binary"}}},
        },
        404: {"description": "Source not found."},
        409: {"description": "Source is still queued/running with no products yet."},
    },
)
async def export_products_csv(
    source_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    """Stream all products in ``source_id`` scope as a UTF-8 BOM CSV attachment."""
    source = await _load_source_or_404(session, source_id)
    scope_or_err = await _load_ready_scope(session, source)
    if isinstance(scope_or_err, JSONResponse):
        return scope_or_err

    shop, products = scope_or_err
    product_ids = [p.id for p in products]
    filename = build_csv_filename(source, shop, products, "products")

    return StreamingResponse(
        iter_products_csv(
            session,
            product_ids,
            default_shop_name=shop.name if shop is not None else None,
        ),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get(
    "/{source_id}/export/reviews.csv",
    responses={
        200: {
            "description": "UTF-8 BOM CSV export of reviews in source scope.",
            "content": {"text/csv": {"schema": {"type": "string", "format": "binary"}}},
        },
        404: {"description": "Source not found."},
        409: {"description": "Source is still queued/running with no products yet."},
    },
)
async def export_reviews_csv(
    source_id: uuid.UUID,
    include_author: bool = Query(default=False),
    sentiment: Annotated[OptionalSentiment, Query()] = None,
    min_rating: Annotated[OptionalRating, Query()] = None,
    max_rating: Annotated[OptionalRating, Query()] = None,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    """Stream reviews in ``source_id`` scope as a UTF-8 BOM CSV attachment."""
    if min_rating is not None and max_rating is not None and min_rating > max_rating:
        raise HTTPException(
            status_code=422,
            detail="min_rating must be less than or equal to max_rating.",
        )

    source = await _load_source_or_404(session, source_id)
    scope_or_err = await _load_ready_scope(session, source)
    if isinstance(scope_or_err, JSONResponse):
        return scope_or_err

    shop, products = scope_or_err
    product_ids = [p.id for p in products]
    filename = build_csv_filename(source, shop, products, "reviews")

    return StreamingResponse(
        iter_reviews_csv(
            session,
            product_ids,
            include_author=include_author,
            sentiment=sentiment,
            min_rating=min_rating,
            max_rating=max_rating,
        ),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
