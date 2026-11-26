"""Product/review chunking and idempotent hash-based chunk synchronization."""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Chunk, Product, Review, Shop
from app.ingest.embedding import Embedder, NullEmbedder

ChunkSourceType = Literal["description", "specs", "review", "meta"]

_PARAGRAPH_SPLIT_RE = re.compile(r"\n\s*\n+")


class ChunkDraft(BaseModel):
    """In-memory chunk candidate before syncing with the ``chunks`` table."""

    source_type: ChunkSourceType
    source_ref: str
    text: str
    content_hash: str


class ChunkSyncStats(BaseModel):
    """Counters returned by :func:`sync_product_chunks`."""

    total: int = 0
    created: int = 0
    kept: int = 0
    deleted: int = 0
    embedded: int = 0
    embed_seconds: float = 0.0


def compute_content_hash(text: str) -> str:
    """Return the 64-char hex SHA-256 digest of ``text``."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _make_draft(source_type: ChunkSourceType, source_ref: str, text: str) -> ChunkDraft:
    return ChunkDraft(
        source_type=source_type,
        source_ref=source_ref,
        text=text,
        content_hash=compute_content_hash(text),
    )


def _tail_overlap(text: str, overlap_chars: int) -> str:
    if overlap_chars <= 0 or not text:
        return ""
    if len(text) <= overlap_chars:
        return text.strip()
    snippet = text[-overlap_chars:]
    # Trim to next whitespace boundary when available to avoid cutting mid-word.
    first_space = snippet.find(" ")
    if 0 < first_space < len(snippet) - 1:
        snippet = snippet[first_space + 1 :]
    return snippet.strip()


def _split_oversized_unit(unit: str, limit: int, overlap: int) -> list[str]:
    """Split a single paragraph/line exceeding ``limit`` into overlapping segments."""
    if len(unit) <= limit:
        return [unit]

    parts: list[str] = []
    start = 0
    step_floor = max(1, limit - overlap)
    length = len(unit)
    while start < length:
        end = min(length, start + limit)
        if end < length:
            ws = unit.rfind(" ", start + max(1, limit // 2), end)
            if ws > start:
                end = ws
        piece = unit[start:end].strip()
        if piece:
            parts.append(piece)
        if end >= length:
            break
        next_start = max(start + 1, end - overlap)
        # Ensure forward progress even on pathological long tokens.
        if next_start <= start:
            next_start = start + step_floor
        start = next_start
    return parts


def chunk_description(
    title: str,
    description_text: str | None,
    *,
    max_chars: int = 1200,
    overlap_chars: int = 150,
) -> list[ChunkDraft]:
    """Split product description into overlapping chunks prefixed with ``[<title>]``."""
    if not description_text or not description_text.strip():
        return []

    clean_title = title.strip()
    prefix = f"[{clean_title}] " if clean_title else ""
    body_limit = max(40, max_chars - len(prefix))
    eff_overlap = min(max(0, overlap_chars), body_limit // 2)

    raw_paragraphs = [
        p.strip() for p in _PARAGRAPH_SPLIT_RE.split(description_text.strip()) if p.strip()
    ]
    # Break paragraphs that are individually larger than body_limit.
    units: list[str] = []
    for para in raw_paragraphs:
        if len(para) <= body_limit:
            units.append(para)
        else:
            lines = [ln.strip() for ln in para.splitlines() if ln.strip()]
            for line in lines:
                units.extend(_split_oversized_unit(line, body_limit, eff_overlap))

    if not units:
        return []

    bodies: list[str] = []
    current = ""
    for unit in units:
        if not current:
            current = unit
            continue
        candidate = f"{current}\n\n{unit}"
        if len(candidate) <= body_limit:
            current = candidate
        else:
            bodies.append(current)
            carry = _tail_overlap(current, eff_overlap)
            if carry and len(carry) + 2 + len(unit) <= body_limit:
                current = f"{carry}\n\n{unit}"
            else:
                current = unit

    if current:
        bodies.append(current)

    return [
        _make_draft("description", str(idx), f"{prefix}{body}") for idx, body in enumerate(bodies)
    ]


def chunk_specs(product: Product) -> ChunkDraft | None:
    """Format product specifications into a single ``key: value`` per-line chunk."""
    if not product.specs:
        return None
    lines: list[str] = []
    for raw_key, raw_val in product.specs.items():
        key = str(raw_key).strip()
        val = str(raw_val).strip() if raw_val is not None else ""
        if key and val:
            lines.append(f"{key}: {val}")
    if not lines:
        return None
    return _make_draft("specs", "specs", "\n".join(lines))


def _format_price(price: Decimal | float | int | None, currency: str) -> str | None:
    if price is None:
        return None
    dec = Decimal(str(price))
    if dec == dec.to_integral_value():
        formatted = f"{int(dec):,}"
    else:
        formatted = f"{dec:,.2f}"
    return f"{formatted} {currency}".strip()


def chunk_meta(product: Product, shop: Shop | None = None) -> ChunkDraft:
    """Build a single structured metadata summary chunk for ``product``."""
    lines: list[str] = [f"Sản phẩm: {product.title.strip()}"]
    if product.brand:
        lines.append(f"Thương hiệu: {product.brand.strip()}")
    if product.category:
        lines.append(f"Danh mục: {product.category.strip()}")
    price_str = _format_price(product.price, product.currency)
    if price_str:
        lines.append(f"Giá: {price_str}")
    if product.rating_avg is not None:
        rating_line = f"Đánh giá: {product.rating_avg:.2f}/5"
        if product.rating_count is not None:
            rating_line += f" ({product.rating_count} lượt đánh giá)"
        lines.append(rating_line)
    if product.sold_count is not None:
        lines.append(f"Đã bán: {product.sold_count}")
    elif product.sold_text:
        lines.append(f"Đã bán: {product.sold_text.strip()}")
    if shop is not None and shop.name:
        lines.append(f"Cửa hàng: {shop.name.strip()}")
    return _make_draft("meta", "meta", "\n".join(lines))


def chunk_reviews(reviews: Sequence[Review], *, min_chars: int = 10) -> list[ChunkDraft]:
    """Convert non-empty customer reviews (>= ``min_chars``) into individual chunks."""
    drafts: list[ChunkDraft] = []
    for review in reviews:
        title = (review.title or "").strip()
        content = (review.content or "").strip()
        if title and content:
            sep = " " if title[-1] in ".!?" else ". "
            body = f"{title}{sep}{content}"
        else:
            body = title or content

        if len(body) < min_chars:
            continue

        text = f"Đánh giá {review.rating}★: {body}"
        drafts.append(_make_draft("review", review.external_id, text))
    return drafts


def _resolve_chunk_limits(
    max_chars: int | None,
    overlap_chars: int | None,
) -> tuple[int, int]:
    try:
        settings = get_settings()
    except Exception:
        settings = None
    eff_max = (
        max_chars
        if max_chars is not None
        else (settings.CHUNK_MAX_CHARS if settings is not None else 1200)
    )
    eff_overlap = (
        overlap_chars
        if overlap_chars is not None
        else (settings.CHUNK_OVERLAP_CHARS if settings is not None else 150)
    )
    return eff_max, eff_overlap


def build_product_chunks(
    product: Product,
    reviews: Sequence[Review],
    *,
    shop: Shop | None = None,
    max_chars: int | None = None,
    overlap_chars: int | None = None,
    min_review_chars: int = 10,
) -> list[ChunkDraft]:
    """Generate all chunk drafts (meta, specs, description, reviews) for a product."""
    eff_max, eff_overlap = _resolve_chunk_limits(max_chars, overlap_chars)

    drafts: list[ChunkDraft] = [chunk_meta(product, shop=shop)]
    specs_draft = chunk_specs(product)
    if specs_draft is not None:
        drafts.append(specs_draft)

    drafts.extend(
        chunk_description(
            product.title,
            product.description_text,
            max_chars=eff_max,
            overlap_chars=eff_overlap,
        )
    )
    drafts.extend(chunk_reviews(reviews, min_chars=min_review_chars))

    # Deduplicate by the unique key (source_type, source_ref, content_hash).
    unique: list[ChunkDraft] = []
    seen: set[tuple[str, str, str]] = set()
    for draft in drafts:
        key = (draft.source_type, draft.source_ref, draft.content_hash)
        if key not in seen:
            seen.add(key)
            unique.append(draft)
    return unique


async def sync_product_chunks(
    session: AsyncSession,
    product: Product,
    drafts: Sequence[ChunkDraft],
    *,
    embedder: Embedder | None = None,
) -> ChunkSyncStats:
    """Synchronize ``chunks`` rows for ``product`` and embed only new/unembedded chunks.

    - Matching ``(source_type, source_ref, content_hash)`` chunks are kept untouched
      and are NOT re-embedded when they already carry an embedding from ``embedder.name``.
    - Obsolete chunks no longer present in ``drafts`` are deleted.
    - New chunks are inserted and embedded in batch.
    """
    existing_result = await session.execute(select(Chunk).where(Chunk.product_id == product.id))
    existing_chunks = list(existing_result.scalars().all())

    existing_by_key: dict[tuple[str, str, str], Chunk] = {
        (chunk.source_type, chunk.source_ref, chunk.content_hash): chunk
        for chunk in existing_chunks
    }
    desired_by_key: dict[tuple[str, str, str], ChunkDraft] = {
        (draft.source_type, draft.source_ref, draft.content_hash): draft for draft in drafts
    }

    deleted = 0
    for key, chunk in existing_by_key.items():
        if key not in desired_by_key:
            await session.delete(chunk)
            deleted += 1

    active_embedder = (
        embedder if (embedder is not None and not isinstance(embedder, NullEmbedder)) else None
    )

    now = datetime.now(UTC)
    created = 0
    kept = 0
    to_embed: list[Chunk] = []

    for key, draft in desired_by_key.items():
        existing = existing_by_key.get(key)
        if existing is not None:
            kept += 1
            if existing.shop_id != product.shop_id:
                existing.shop_id = product.shop_id
            if active_embedder is not None and (
                existing.embedding is None or existing.embedding_model != active_embedder.name
            ):
                to_embed.append(existing)
        else:
            chunk = Chunk(
                id=uuid.uuid4(),
                product_id=product.id,
                shop_id=product.shop_id,
                source_type=draft.source_type,
                source_ref=draft.source_ref,
                text=draft.text,
                content_hash=draft.content_hash,
                created_at=now,
                updated_at=now,
            )
            session.add(chunk)
            created += 1
            if active_embedder is not None:
                to_embed.append(chunk)

    embed_seconds = 0.0
    embedded = 0
    if active_embedder is not None and to_embed:
        t0 = time.monotonic()
        vectors = await active_embedder.embed([chunk.text for chunk in to_embed])
        embed_seconds = round(time.monotonic() - t0, 3)
        if len(vectors) != len(to_embed):
            raise RuntimeError(
                f"Embedder '{active_embedder.name}' returned {len(vectors)} vectors "
                f"for {len(to_embed)} chunks."
            )
        for chunk, vec in zip(to_embed, vectors, strict=True):
            chunk.embedding = vec
            chunk.embedding_model = active_embedder.name
            chunk.dim = len(vec)
            chunk.updated_at = now
        embedded = len(to_embed)

    await session.flush()

    return ChunkSyncStats(
        total=len(desired_by_key),
        created=created,
        kept=kept,
        deleted=deleted,
        embedded=embedded,
        embed_seconds=embed_seconds,
    )
