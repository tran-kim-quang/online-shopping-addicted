"""Streaming UTF-8 BOM CSV export with formula-injection protection for sources."""

from __future__ import annotations

import csv
import io
import json
import re
import unicodedata
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Product, Review, Shop, Source
from app.reports.source_report import _compute_discount_pct, _compute_pct

UTF8_BOM_BYTES = b"\xef\xbb\xbf"
DEFAULT_CSV_BATCH_SIZE = 500
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_SLUG_RE = re.compile(r"[^a-z0-9]+")

PRODUCTS_CSV_COLUMNS: tuple[str, ...] = (
    "shop_name",
    "product_id",
    "external_id",
    "title",
    "brand",
    "category",
    "url",
    "price",
    "list_price",
    "discount_pct",
    "sold_count",
    "rating_avg",
    "rating_count",
    "rating_count_effective",
    "review_count_reported",
    "reviews_collected",
    "coverage_pct",
    "positive",
    "neutral",
    "negative",
    "positive_pct",
    "last_review_at",
)

REVIEWS_CSV_BASE_COLUMNS: tuple[str, ...] = (
    "product_external_id",
    "product_title",
    "review_external_id",
    "rating",
    "sentiment",
    "sentiment_source",
    "title",
    "content",
    "aspects",
    "helpful_count",
    "created_at_source",
    "variant",
    "tags",
    "sub_ratings",
    "seller_reply",
    "media_count",
)

_ASPECT_SYMBOLS: dict[str, str] = {
    "positive": "+",
    "negative": "-",
    "neutral": "=",
}


def sanitize_csv_cell(value: Any) -> Any:
    """Neutralize spreadsheet formula injection and serialize scalar cell types."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, str):
        if value and value.startswith(_FORMULA_PREFIXES):
            return "'" + value
        return value
    return value


def format_aspects_cell(aspects: Any) -> str:
    """Format ``reviews.aspects`` JSONB array as ``'giao hàng:+; đóng gói:-'``."""
    if not isinstance(aspects, list) or not aspects:
        return ""
    parts: list[str] = []
    for item in aspects:
        if not isinstance(item, dict):
            continue
        name = str(item.get("aspect", "")).strip()
        if not name:
            continue
        sentiment = str(item.get("sentiment", "")).strip().lower()
        symbol = _ASPECT_SYMBOLS.get(sentiment, "")
        parts.append(f"{name}:{symbol}" if symbol else name)
    return "; ".join(parts)


def slugify_ascii(text: str | None, *, max_length: int = 48, fallback: str = "export") -> str:
    """Convert arbitrary Unicode/Vietnamese text into a safe ASCII filename slug."""
    if not text:
        return fallback
    replaced = text.replace("đ", "d").replace("Đ", "D")
    normalized = unicodedata.normalize("NFKD", replaced)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = _SLUG_RE.sub("-", ascii_only).strip("-")
    if len(slug) > max_length:
        slug = slug[:max_length].rstrip("-")
    return slug or fallback


def build_csv_filename(
    source: Source,
    shop: Shop | None,
    products: Sequence[Product],
    kind: Literal["products", "reviews"],
    *,
    today: datetime | None = None,
) -> str:
    """Build ``<platform>-<slug shop/SP>-<products|reviews>-<YYYYMMDD>.csv``."""
    platform_slug = slugify_ascii(source.platform, max_length=24, fallback="source")
    id_fallback = str(source.id)[:8]

    target_name: str | None = None
    if source.url_type == "shop" and shop is not None and shop.name:
        target_name = shop.name
    elif products and products[0].title:
        target_name = products[0].title
    elif shop is not None and shop.name:
        target_name = shop.name

    target_slug = slugify_ascii(target_name, max_length=48, fallback=id_fallback)
    date_stamp = (today or datetime.now(UTC)).strftime("%Y%m%d")
    return f"{platform_slug}-{target_slug}-{kind}-{date_stamp}.csv"


def _encode_csv_rows(rows: Sequence[Sequence[Any]], *, include_bom: bool = False) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    for row in rows:
        writer.writerow([sanitize_csv_cell(cell) for cell in row])
    encoded = buf.getvalue().encode("utf-8")
    return UTF8_BOM_BYTES + encoded if include_bom else encoded


async def iter_products_csv(
    session: AsyncSession,
    product_ids: Sequence[uuid.UUID],
    *,
    default_shop_name: str | None = None,
    batch_size: int = DEFAULT_CSV_BATCH_SIZE,
) -> AsyncIterator[bytes]:
    """Stream ``products.csv`` bytes in batches of ``batch_size`` using server-side cursors."""
    effective_batch = max(1, int(batch_size))
    yield _encode_csv_rows([PRODUCTS_CSV_COLUMNS], include_bom=True)

    pid_list = list(product_ids)
    if not pid_list:
        return

    review_agg_subq = (
        select(
            Review.product_id.label("product_id"),
            func.count().label("reviews_collected"),
            func.count().filter(Review.sentiment == "positive").label("pos"),
            func.count().filter(Review.sentiment == "neutral").label("neu"),
            func.count().filter(Review.sentiment == "negative").label("neg"),
            func.max(Review.created_at_source).label("last_review_at"),
        )
        .where(Review.product_id.in_(pid_list))
        .group_by(Review.product_id)
        .subquery("rev_agg")
    )

    effective_rating_count = func.coalesce(Product.rating_count, Product.review_count)
    stmt = (
        select(
            Shop.name.label("shop_name"),
            Product.id.label("product_id"),
            Product.external_id.label("external_id"),
            Product.title.label("title"),
            Product.brand.label("brand"),
            Product.category.label("category"),
            Product.url.label("url"),
            Product.price.label("price"),
            Product.list_price.label("list_price"),
            Product.sold_count.label("sold_count"),
            Product.rating_avg.label("rating_avg"),
            Product.rating_count.label("rating_count"),
            effective_rating_count.label("rating_count_effective"),
            Product.review_count.label("review_count_reported"),
            Product.positive_review_count.label("prod_pos"),
            Product.neutral_review_count.label("prod_neu"),
            Product.negative_review_count.label("prod_neg"),
            review_agg_subq.c.reviews_collected.label("reviews_collected"),
            review_agg_subq.c.pos.label("rev_pos"),
            review_agg_subq.c.neu.label("rev_neu"),
            review_agg_subq.c.neg.label("rev_neg"),
            review_agg_subq.c.last_review_at.label("last_review_at"),
        )
        .select_from(Product)
        .outerjoin(Shop, Product.shop_id == Shop.id)
        .outerjoin(review_agg_subq, Product.id == review_agg_subq.c.product_id)
        .where(Product.id.in_(pid_list))
        .order_by(
            Product.sold_count.desc().nullslast(),
            effective_rating_count.desc().nullslast(),
            Product.title.asc(),
            Product.id.asc(),
        )
    )

    stream_result = await session.stream(stmt.execution_options(yield_per=effective_batch))
    try:
        async for partition in stream_result.partitions(effective_batch):
            csv_batch: list[list[Any]] = []
            for row in partition:
                collected = int(row.reviews_collected or 0)
                if collected > 0:
                    pos = int(row.rev_pos or 0)
                    neu = int(row.rev_neu or 0)
                    neg = int(row.rev_neg or 0)
                    last_review_at = row.last_review_at
                else:
                    pos = int(row.prod_pos or 0)
                    neu = int(row.prod_neu or 0)
                    neg = int(row.prod_neg or 0)
                    last_review_at = None

                analyzed = pos + neu + neg
                csv_batch.append(
                    [
                        row.shop_name if row.shop_name is not None else (default_shop_name or ""),
                        row.product_id,
                        row.external_id,
                        row.title,
                        row.brand,
                        row.category,
                        row.url,
                        row.price,
                        row.list_price,
                        _compute_discount_pct(row.price, row.list_price),
                        row.sold_count,
                        float(row.rating_avg) if row.rating_avg is not None else None,
                        row.rating_count,
                        row.rating_count_effective,
                        row.review_count_reported,
                        collected,
                        _compute_pct(collected, row.review_count_reported),
                        pos,
                        neu,
                        neg,
                        _compute_pct(pos, analyzed),
                        last_review_at,
                    ]
                )
            if csv_batch:
                yield _encode_csv_rows(csv_batch)
    finally:
        await stream_result.close()


async def iter_reviews_csv(
    session: AsyncSession,
    product_ids: Sequence[uuid.UUID],
    *,
    include_author: bool = False,
    sentiment: Literal["positive", "neutral", "negative"] | None = None,
    min_rating: int | None = None,
    max_rating: int | None = None,
    batch_size: int = DEFAULT_CSV_BATCH_SIZE,
) -> AsyncIterator[bytes]:
    """Stream ``reviews.csv`` bytes in batches of ``batch_size`` using server-side cursors.

    ``author`` is excluded unless ``include_author=True``. ``raw`` is never selected or exported.
    """
    effective_batch = max(1, int(batch_size))
    headers = (*REVIEWS_CSV_BASE_COLUMNS, "author") if include_author else REVIEWS_CSV_BASE_COLUMNS
    yield _encode_csv_rows([headers], include_bom=True)

    pid_list = list(product_ids)
    if not pid_list:
        return

    columns: list[Any] = [
        Product.external_id.label("product_external_id"),
        Product.title.label("product_title"),
        Review.external_id.label("review_external_id"),
        Review.rating.label("rating"),
        Review.sentiment.label("sentiment"),
        Review.sentiment_source.label("sentiment_source"),
        Review.title.label("title"),
        Review.content.label("content"),
        Review.aspects.label("aspects"),
        Review.helpful_count.label("helpful_count"),
        Review.created_at_source.label("created_at_source"),
        Review.variant.label("variant"),
        Review.tags.label("tags"),
        Review.sub_ratings.label("sub_ratings"),
        Review.seller_reply.label("seller_reply"),
        Review.media_count.label("media_count"),
    ]
    if include_author:
        columns.append(Review.author.label("author"))

    clauses: list[Any] = [Review.product_id.in_(pid_list)]
    if sentiment is not None:
        clauses.append(Review.sentiment == sentiment)
    if min_rating is not None:
        clauses.append(Review.rating >= min_rating)
    if max_rating is not None:
        clauses.append(Review.rating <= max_rating)

    stmt = (
        select(*columns)
        .select_from(Review)
        .join(Product, Review.product_id == Product.id)
        .where(*clauses)
        .order_by(
            Product.external_id.asc(),
            Review.created_at_source.desc().nullslast(),
            Review.id.asc(),
        )
    )

    stream_result = await session.stream(stmt.execution_options(yield_per=effective_batch))
    try:
        async for partition in stream_result.partitions(effective_batch):
            csv_batch: list[list[Any]] = []
            for row in partition:
                raw_tags = getattr(row, "tags", None)
                tags_cell = (
                    "; ".join(str(t).strip() for t in raw_tags if isinstance(t, str) and t.strip())
                    if isinstance(raw_tags, list)
                    else ""
                )
                raw_sub_ratings = getattr(row, "sub_ratings", None)
                sub_ratings_cell = (
                    json.dumps(raw_sub_ratings, ensure_ascii=False, sort_keys=True)
                    if isinstance(raw_sub_ratings, dict) and raw_sub_ratings
                    else ""
                )
                record: list[Any] = [
                    row.product_external_id,
                    row.product_title,
                    row.review_external_id,
                    row.rating,
                    row.sentiment,
                    row.sentiment_source,
                    row.title,
                    row.content,
                    format_aspects_cell(row.aspects),
                    row.helpful_count,
                    row.created_at_source,
                    getattr(row, "variant", None),
                    tags_cell,
                    sub_ratings_cell,
                    getattr(row, "seller_reply", None),
                    getattr(row, "media_count", 0),
                ]
                if include_author:
                    record.append(row.author)
                csv_batch.append(record)
            if csv_batch:
                yield _encode_csv_rows(csv_batch)
    finally:
        await stream_result.close()
