"""Hybrid full-text (unaccent tsvector) + pgvector cosine search fused with RRF."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from pgvector.sqlalchemy import Vector
from pydantic import BaseModel
from sqlalchemy import and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Chunk, Review
from app.ingest.embedding import Embedder, NullEmbedder

RRF_K = 60
DEFAULT_MIN_COSINE_SIMILARITY = 0.6


class SearchHit(BaseModel):
    """Single ranked chunk hit returned by :func:`hybrid_search`."""

    chunk_id: uuid.UUID
    product_id: uuid.UUID
    source_type: str
    source_ref: str
    text: str
    score: float
    fts_rank: int | None = None
    vector_rank: int | None = None
    similarity: float | None = None


def _resolve_min_similarity(min_similarity: float | None) -> float:
    if min_similarity is not None:
        return float(min_similarity)
    try:
        return float(get_settings().SEARCH_MIN_COSINE_SIMILARITY)
    except Exception:
        return DEFAULT_MIN_COSINE_SIMILARITY


def _build_scope_filters(
    *,
    product_ids: Sequence[uuid.UUID] | None,
    shop_id: uuid.UUID | None,
    source_types: Sequence[str] | None,
) -> list[Any]:
    filters: list[Any] = []
    if product_ids is not None:
        filters.append(Chunk.product_id.in_(list(product_ids)))
    if shop_id is not None:
        filters.append(Chunk.shop_id == shop_id)
    if source_types is not None:
        filters.append(Chunk.source_type.in_(list(source_types)))
    return filters


def _build_review_filters(
    *,
    review_sentiments: Sequence[str] | None,
    min_rating: int | None,
    max_rating: int | None,
) -> list[Any]:
    filters: list[Any] = []
    if review_sentiments is not None:
        filters.append(Review.sentiment.in_(list(review_sentiments)))
    if min_rating is not None:
        filters.append(Review.rating >= min_rating)
    if max_rating is not None:
        filters.append(Review.rating <= max_rating)
    return filters


async def hybrid_search(
    session: AsyncSession,
    query: str,
    *,
    product_ids: Sequence[uuid.UUID] | None = None,
    shop_id: uuid.UUID | None = None,
    source_types: Sequence[str] | None = None,
    review_sentiments: Sequence[str] | None = None,
    min_rating: int | None = None,
    max_rating: int | None = None,
    min_similarity: float | None = None,
    k: int = 8,
    embedder: Embedder | None = None,
) -> list[SearchHit]:
    """Run hybrid full-text + vector cosine search and merge ranks with RRF (``1/(60+rank)``).

    - Full-text uses ``websearch_to_tsquery('simple', f_unaccent(query))`` ranked by ``ts_rank_cd``.
    - Vector search casts ``embedding::vector(<dim>)`` to match partial HNSW expression indexes
      and applies ``distance <= 1 - min_similarity`` in SQL when ``min_similarity > 0``.
      Pass ``min_similarity=0`` to disable the cosine threshold; ``None`` uses
      ``Settings.SEARCH_MIN_COSINE_SIMILARITY`` (default 0.6).
    - Optional ``review_sentiments`` / ``min_rating`` / ``max_rating`` join ``reviews`` in SQL
      so review filtering happens before candidate truncation.
    - When ``embedder`` is ``None`` or :class:`NullEmbedder`, falls back cleanly to full-text only.
    - Returns ``[]`` for empty queries or empty scope filters.
    """
    cleaned = query.strip()
    if not cleaned or k <= 0:
        return []

    if product_ids is not None and len(product_ids) == 0:
        return []
    if source_types is not None and len(source_types) == 0:
        return []
    if review_sentiments is not None and len(review_sentiments) == 0:
        return []

    has_review_filters = (
        review_sentiments is not None or min_rating is not None or max_rating is not None
    )
    if has_review_filters and source_types is not None and "review" not in source_types:
        return []

    candidate_limit = k * 4
    base_filters = _build_scope_filters(
        product_ids=product_ids,
        shop_id=shop_id,
        source_types=source_types,
    )
    review_filters = _build_review_filters(
        review_sentiments=review_sentiments,
        min_rating=min_rating,
        max_rating=max_rating,
    )
    if has_review_filters:
        base_filters.append(Chunk.source_type == "review")

    def _apply_review_join(stmt: Any) -> Any:
        if not has_review_filters:
            return stmt
        return stmt.join(
            Review,
            and_(
                Review.product_id == Chunk.product_id,
                Review.external_id == Chunk.source_ref,
            ),
        ).where(*review_filters)

    # 1. Full-text search branch.
    tsq = func.websearch_to_tsquery("simple", func.f_unaccent(cleaned))
    fts_stmt = (
        select(
            Chunk.id,
            Chunk.product_id,
            Chunk.source_type,
            Chunk.source_ref,
            Chunk.text,
            func.ts_rank_cd(Chunk.tsv, tsq).label("fts_score"),
        )
        .where(*base_filters, Chunk.tsv.op("@@")(tsq))
        .order_by(func.ts_rank_cd(Chunk.tsv, tsq).desc(), Chunk.id.asc())
        .limit(candidate_limit)
    )
    fts_stmt = _apply_review_join(fts_stmt)
    fts_rows = list((await session.execute(fts_stmt)).all())

    # 2. Vector similarity branch (optional, requires active embedder).
    vec_rows: list[Any] = []
    if embedder is not None and not isinstance(embedder, NullEmbedder):
        vectors = await embedder.embed([cleaned])
        if vectors and vectors[0]:
            query_vec = vectors[0]
            dim = len(query_vec)
            distance_expr = cast(Chunk.embedding, Vector(dim)).cosine_distance(query_vec)
            eff_min_sim = _resolve_min_similarity(min_similarity)

            vec_where: list[Any] = [
                *base_filters,
                Chunk.embedding.is_not(None),
                Chunk.dim == dim,
                or_(
                    Chunk.embedding_model == embedder.name,
                    Chunk.embedding_model.is_(None),
                ),
            ]
            if eff_min_sim > 0.0:
                vec_where.append(distance_expr <= (1.0 - eff_min_sim))

            vec_stmt = (
                select(
                    Chunk.id,
                    Chunk.product_id,
                    Chunk.source_type,
                    Chunk.source_ref,
                    Chunk.text,
                    distance_expr.label("distance"),
                )
                .where(*vec_where)
                .order_by(distance_expr.asc(), Chunk.id.asc())
                .limit(candidate_limit)
            )
            vec_stmt = _apply_review_join(vec_stmt)
            vec_rows = list((await session.execute(vec_stmt)).all())

    if not fts_rows and not vec_rows:
        return []

    # 3. Reciprocal Rank Fusion (RRF).
    candidates: dict[uuid.UUID, dict[str, Any]] = {}
    for rank, row in enumerate(fts_rows, start=1):
        entry = candidates.setdefault(
            row.id,
            {
                "chunk_id": row.id,
                "product_id": row.product_id,
                "source_type": row.source_type,
                "source_ref": row.source_ref,
                "text": row.text,
                "fts_rank": None,
                "vector_rank": None,
                "similarity": None,
                "score": 0.0,
            },
        )
        entry["fts_rank"] = rank
        entry["score"] += 1.0 / (RRF_K + rank)

    for rank, row in enumerate(vec_rows, start=1):
        entry = candidates.setdefault(
            row.id,
            {
                "chunk_id": row.id,
                "product_id": row.product_id,
                "source_type": row.source_type,
                "source_ref": row.source_ref,
                "text": row.text,
                "fts_rank": None,
                "vector_rank": None,
                "similarity": None,
                "score": 0.0,
            },
        )
        entry["vector_rank"] = rank
        entry["similarity"] = round(1.0 - float(row.distance), 6)
        entry["score"] += 1.0 / (RRF_K + rank)

    ranked = sorted(
        candidates.values(),
        key=lambda item: (
            -item["score"],
            item["fts_rank"] if item["fts_rank"] is not None else 10**9,
            item["vector_rank"] if item["vector_rank"] is not None else 10**9,
            str(item["chunk_id"]),
        ),
    )

    return [
        SearchHit(
            chunk_id=item["chunk_id"],
            product_id=item["product_id"],
            source_type=item["source_type"],
            source_ref=item["source_ref"],
            text=item["text"],
            score=round(item["score"], 8),
            fts_rank=item["fts_rank"],
            vector_rank=item["vector_rank"],
            similarity=item["similarity"],
        )
        for item in ranked[:k]
    ]
