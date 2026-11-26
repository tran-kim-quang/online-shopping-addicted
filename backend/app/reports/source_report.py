"""Deterministic SQL-aggregated overview report for a crawled/ingested source."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import case, func, literal_column, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.models import Chunk, Product, Review, Shop, Source
from app.ingest.pipeline import resolve_source_scope

# ---------------------------------------------------------------------------
# Pydantic response schemas (also exported to OpenAPI for Phase 5 UI)
# ---------------------------------------------------------------------------


class ReportSourceInfo(BaseModel):
    """Metadata for the reported source."""

    id: uuid.UUID
    url: str
    url_type: str | None = None
    platform: str | None = None
    status: str
    display_name: str | None = None
    crawled_at: datetime | None = None
    ingested_at: datetime | None = None
    refresh_after: datetime | None = None


class ReportShopInfo(BaseModel):
    """Summary of the shop associated with the source, when available."""

    id: uuid.UUID
    name: str
    url: str
    rating: float | None = None
    follower_count: int | None = None
    product_count: int | None = None


class SentimentCounts(BaseModel):
    """Review sentiment breakdown across the source scope."""

    positive: int = 0
    neutral: int = 0
    negative: int = 0
    unanalyzed: int = 0


class SentimentSourceCounts(BaseModel):
    """Breakdown of how review sentiments were produced."""

    llm: int = 0
    rating: int = 0
    none: int = 0


class ChunkCounts(BaseModel):
    """Indexed chunk counts across the source scope."""

    total: int = 0
    by_type: dict[str, int] = Field(default_factory=dict)


class ReportTotals(BaseModel):
    """Aggregate metrics across all products, reviews, and chunks in scope."""

    products: int = 0
    products_with_price: int = 0
    sold_total: int = 0
    price_min: float | None = None
    price_max: float | None = None
    price_median: float | None = None
    rating_avg_weighted: float | None = None
    review_count_reported: int = 0
    reviews_collected: int = 0
    reviews_rating_only: int = 0
    coverage_pct: float | None = None
    sentiment: SentimentCounts = Field(default_factory=SentimentCounts)
    sentiment_source: SentimentSourceCounts = Field(default_factory=SentimentSourceCounts)
    chunks: ChunkCounts = Field(default_factory=ChunkCounts)


class RatingBucket(BaseModel):
    """Star-rating count and percentage from collected reviews."""

    stars: int
    count: int = 0
    pct: float | None = None


class ProductReportRow(BaseModel):
    """Per-product metrics row included in ``SourceReport.products``."""

    product_id: uuid.UUID
    external_id: str
    title: str
    brand: str | None = None
    category: str | None = None
    url: str
    image_url: str | None = None
    short_description: str | None = None
    price: float | None = None
    list_price: float | None = None
    discount_pct: float | None = None
    sold_count: int | None = None
    rating_avg: float | None = None
    rating_count: int | None = None
    rating_count_effective: int | None = None
    review_count_reported: int | None = None
    reviews_collected: int = 0
    coverage_pct: float | None = None
    positive: int = 0
    neutral: int = 0
    negative: int = 0
    positive_pct: float | None = None
    last_review_at: datetime | None = None


class RankingItem(BaseModel):
    """Compact leaderboard entry (product id, title, and ranked metric value)."""

    product_id: uuid.UUID
    title: str
    value: int | float


class ReportRankings(BaseModel):
    """Top-N product leaderboards across key business dimensions."""

    best_selling: list[RankingItem] = Field(default_factory=list)
    most_praised: list[RankingItem] = Field(default_factory=list)
    highest_rated: list[RankingItem] = Field(default_factory=list)
    most_complained: list[RankingItem] = Field(default_factory=list)
    biggest_discount: list[RankingItem] = Field(default_factory=list)
    cheapest: list[RankingItem] = Field(default_factory=list)


class AspectCount(BaseModel):
    """Aggregated mention count for an extracted review aspect."""

    aspect: str
    count: int


class ReportAspects(BaseModel):
    """Top positive and negative aspects extracted from reviews."""

    top_positive: list[AspectCount] = Field(default_factory=list)
    top_negative: list[AspectCount] = Field(default_factory=list)


class ReportWarning(BaseModel):
    """Structured data-quality or coverage warning with a stable code."""

    code: str
    message: str
    params: dict[str, int | float | str] = Field(default_factory=dict)
    product_ids: list[uuid.UUID] | None = None


class SourceReport(BaseModel):
    """Complete deterministic overview report for a source."""

    source: ReportSourceInfo
    shop: ReportShopInfo | None = None
    totals: ReportTotals
    rating_distribution: list[RatingBucket]
    products: list[ProductReportRow]
    rankings: ReportRankings
    aspects: ReportAspects
    warnings: list[ReportWarning]
    generated_at: datetime


# ---------------------------------------------------------------------------
# Numeric & time helpers
# ---------------------------------------------------------------------------


def _to_number(value: Decimal | float | int | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 2)


def _compute_discount_pct(
    price: Decimal | float | None,
    list_price: Decimal | float | None,
) -> float | None:
    if price is None or list_price is None:
        return None
    price_dec = Decimal(str(price))
    list_dec = Decimal(str(list_price))
    if list_dec <= 0:
        return None
    if list_dec <= price_dec:
        return 0.0
    pct = (list_dec - price_dec) * Decimal("100") / list_dec
    return round(float(pct), 1)


def _compute_pct(numerator: int | float, denominator: int | float | None) -> float | None:
    if denominator is None or denominator <= 0:
        return None
    return round((float(numerator) / float(denominator)) * 100.0, 1)


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _resolve_cache_ttl_hours(settings: Settings | None) -> int:
    if settings is not None:
        return settings.CRAWL_CACHE_TTL_HOURS
    try:
        return get_settings().CRAWL_CACHE_TTL_HOURS
    except Exception:
        return 48


def _build_rankings(rows: list[ProductReportRow], top_n: int) -> ReportRankings:
    best_selling = [
        RankingItem(product_id=r.product_id, title=r.title, value=r.sold_count)
        for r in sorted(
            (r for r in rows if r.sold_count is not None),
            key=lambda r: (
                -(r.sold_count if r.sold_count is not None else -1),
                -(r.rating_count_effective if r.rating_count_effective is not None else -1),
                r.title.lower(),
                str(r.product_id),
            ),
        )[:top_n]
        if r.sold_count is not None
    ]

    most_praised = [
        RankingItem(product_id=r.product_id, title=r.title, value=r.positive)
        for r in sorted(
            (r for r in rows if r.positive > 0),
            key=lambda r: (
                -r.positive,
                -(r.rating_avg if r.rating_avg is not None else -1.0),
                r.title.lower(),
                str(r.product_id),
            ),
        )[:top_n]
    ]

    highest_rated = [
        RankingItem(product_id=r.product_id, title=r.title, value=r.rating_avg)
        for r in sorted(
            (
                r
                for r in rows
                if r.rating_avg is not None
                and r.rating_count_effective is not None
                and r.rating_count_effective >= 3
            ),
            key=lambda r: (
                -(r.rating_avg if r.rating_avg is not None else -1.0),
                -(r.rating_count_effective if r.rating_count_effective is not None else -1),
                -(r.sold_count if r.sold_count is not None else -1),
                r.title.lower(),
                str(r.product_id),
            ),
        )[:top_n]
        if r.rating_avg is not None
    ]

    most_complained = [
        RankingItem(product_id=r.product_id, title=r.title, value=r.negative)
        for r in sorted(
            (r for r in rows if r.negative > 0),
            key=lambda r: (
                -r.negative,
                -r.reviews_collected,
                r.title.lower(),
                str(r.product_id),
            ),
        )[:top_n]
    ]

    biggest_discount = [
        RankingItem(product_id=r.product_id, title=r.title, value=r.discount_pct)
        for r in sorted(
            (r for r in rows if r.discount_pct is not None and r.discount_pct > 0),
            key=lambda r: (
                -(r.discount_pct if r.discount_pct is not None else -1.0),
                -(r.sold_count if r.sold_count is not None else -1),
                r.title.lower(),
                str(r.product_id),
            ),
        )[:top_n]
        if r.discount_pct is not None
    ]

    cheapest = [
        RankingItem(product_id=r.product_id, title=r.title, value=r.price)
        for r in sorted(
            (r for r in rows if r.price is not None),
            key=lambda r: (
                r.price if r.price is not None else float("inf"),
                -(r.sold_count if r.sold_count is not None else -1),
                r.title.lower(),
                str(r.product_id),
            ),
        )[:top_n]
        if r.price is not None
    ]

    return ReportRankings(
        best_selling=best_selling,
        most_praised=most_praised,
        highest_rated=highest_rated,
        most_complained=most_complained,
        biggest_discount=biggest_discount,
        cheapest=cheapest,
    )


def _build_warnings(
    *,
    source: Source,
    totals: ReportTotals,
    product_rows: list[ProductReportRow],
    unanalyzed_product_ids: list[uuid.UUID],
    has_any_aspects: bool,
    ttl_hours: int,
    now_utc: datetime,
) -> list[ReportWarning]:
    warnings: list[ReportWarning] = []

    # 1. partial_reviews
    partial_rows = [
        r
        for r in product_rows
        if r.review_count_reported is not None and r.reviews_collected < r.review_count_reported
    ]
    if partial_rows:
        collected_sum = sum(r.reviews_collected for r in partial_rows)
        reported_sum = sum(
            r.review_count_reported for r in partial_rows if r.review_count_reported is not None
        )
        warnings.append(
            ReportWarning(
                code="partial_reviews",
                message=(
                    f"Mới thu thập {collected_sum}/{reported_sum} review "
                    f"ở {len(partial_rows)} sản phẩm."
                ),
                params={
                    "collected": collected_sum,
                    "reported": reported_sum,
                    "products": len(partial_rows),
                },
                product_ids=[r.product_id for r in partial_rows],
            )
        )

    # 2. reported_count_lower
    lower_rows = [
        r
        for r in product_rows
        if r.review_count_reported is not None and r.reviews_collected > r.review_count_reported
    ]
    if lower_rows:
        warnings.append(
            ReportWarning(
                code="reported_count_lower",
                message=(
                    f"Số review thu thập được nhiều hơn số liệu sàn báo ở {len(lower_rows)} "
                    "sản phẩm (một số sàn không đếm đánh giá chỉ chấm sao)."
                ),
                params={"products": len(lower_rows)},
                product_ids=[r.product_id for r in lower_rows],
            )
        )

    # 3. sentiment_rating_only
    analyzed_count = (
        totals.sentiment.positive + totals.sentiment.neutral + totals.sentiment.negative
    )
    if (
        analyzed_count > 0
        and totals.sentiment_source.llm == 0
        and totals.sentiment_source.rating == analyzed_count
    ):
        rating_only_count = totals.reviews_rating_only or totals.sentiment_source.rating
        warnings.append(
            ReportWarning(
                code="sentiment_rating_only",
                message="Cảm xúc đang suy ra từ số sao; chưa phân tích bằng LLM.",
                params={"count": rating_only_count},
            )
        )

    # 4. no_aspects
    if not has_any_aspects:
        warnings.append(
            ReportWarning(
                code="no_aspects",
                message="Chưa có dữ liệu khía cạnh (aspects) từ đánh giá.",
                params={},
            )
        )

    # 5. unanalyzed_reviews
    if totals.sentiment.unanalyzed > 0:
        warnings.append(
            ReportWarning(
                code="unanalyzed_reviews",
                message=f"Còn {totals.sentiment.unanalyzed} review chưa được phân tích cảm xúc.",
                params={"count": totals.sentiment.unanalyzed},
                product_ids=unanalyzed_product_ids or None,
            )
        )

    # 6. not_ingested
    is_ingested = isinstance(source.stats, dict) and "ingest" in source.stats
    if not is_ingested:
        warnings.append(
            ReportWarning(
                code="not_ingested",
                message=(
                    "Nguồn dữ liệu chưa được ingest (chưa tạo chunk/embedding); "
                    "chỉ hiển thị dữ liệu đã crawl."
                ),
                params={},
            )
        )

    # 7. stale_data
    if source.crawled_at is not None:
        crawled_utc = _to_utc(source.crawled_at)
        if (now_utc - crawled_utc) > timedelta(hours=ttl_hours):
            warnings.append(
                ReportWarning(
                    code="stale_data",
                    message=(
                        f"Dữ liệu đã được thu thập cách đây hơn {ttl_hours} giờ; "
                        "lần mở tiếp theo sẽ tự thu thập mới."
                    ),
                    params={"ttl_hours": ttl_hours},
                )
            )

    # 8. missing_price
    missing_price_ids = [r.product_id for r in product_rows if r.price is None]
    if missing_price_ids:
        warnings.append(
            ReportWarning(
                code="missing_price",
                message=f"Có {len(missing_price_ids)} sản phẩm thiếu thông tin giá.",
                params={"products": len(missing_price_ids)},
                product_ids=missing_price_ids,
            )
        )

    # 9. missing_sold_count
    missing_sold_ids = [r.product_id for r in product_rows if r.sold_count is None]
    if missing_sold_ids:
        warnings.append(
            ReportWarning(
                code="missing_sold_count",
                message=(
                    f"Có {len(missing_sold_ids)} sản phẩm thiếu số lượng đã bán (sold_count)."
                ),
                params={"products": len(missing_sold_ids)},
                product_ids=missing_sold_ids,
            )
        )

    return warnings


# ---------------------------------------------------------------------------
# Main service function (stateless, SQL-aggregated, no LLM calls)
# ---------------------------------------------------------------------------


async def build_source_report(
    session: AsyncSession,
    source: Source,
    *,
    top_n: int = 10,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> SourceReport:
    """Build a deterministic SQL-aggregated :class:`SourceReport` for ``source``.

    Scope matches the QA Agent and ``GET /api/sources/{id}`` via
    :func:`app.ingest.pipeline.resolve_source_scope`. All review and chunk
    statistics are computed via SQL aggregates without loading reviews into
    Python memory.
    """
    effective_top_n = max(1, min(int(top_n), 50))
    now_utc = _to_utc(now) if now is not None else datetime.now(UTC)
    ttl_hours = _resolve_cache_ttl_hours(settings)

    shop: Shop | None
    products: list[Product]
    shop, products = await resolve_source_scope(session, source)
    product_ids = [p.id for p in products]

    ingested_dates = [p.ingested_at for p in products if p.ingested_at is not None]
    ingested_at = max(ingested_dates) if ingested_dates else None

    first_title = products[0].title if products else None
    if source.url_type == "shop":
        resolved_display_name = (shop.name.strip() if shop and shop.name else "") or source.url
    elif source.url_type == "product":
        resolved_display_name = (first_title.strip() if first_title else "") or source.url
    else:
        resolved_display_name = (
            (shop.name.strip() if shop and shop.name else "")
            or (first_title.strip() if first_title else "")
            or source.url
        )

    refresh_after = (
        _to_utc(source.crawled_at) + timedelta(hours=ttl_hours)
        if source.crawled_at is not None
        else None
    )

    source_info = ReportSourceInfo(
        id=source.id,
        url=source.url,
        url_type=source.url_type,
        platform=source.platform,
        status=source.status,
        display_name=resolved_display_name,
        crawled_at=source.crawled_at,
        ingested_at=ingested_at,
        refresh_after=refresh_after,
    )

    shop_info: ReportShopInfo | None = None
    if shop is not None:
        shop_info = ReportShopInfo(
            id=shop.id,
            name=shop.name,
            url=shop.url,
            rating=shop.rating,
            follower_count=shop.follower_count,
            product_count=shop.product_count,
        )

    if not product_ids:
        empty_totals = ReportTotals()
        empty_dist = [RatingBucket(stars=star, count=0, pct=None) for star in range(5, 0, -1)]
        warnings = _build_warnings(
            source=source,
            totals=empty_totals,
            product_rows=[],
            unanalyzed_product_ids=[],
            has_any_aspects=False,
            ttl_hours=ttl_hours,
            now_utc=now_utc,
        )
        return SourceReport(
            source=source_info,
            shop=shop_info,
            totals=empty_totals,
            rating_distribution=empty_dist,
            products=[],
            rankings=ReportRankings(),
            aspects=ReportAspects(),
            warnings=warnings,
            generated_at=now_utc,
        )

    # 1. Aggregate product-level metrics in Postgres (including exact median).
    effective_rating_count = func.coalesce(Product.rating_count, Product.review_count)
    weighted_filter = (
        Product.rating_avg.is_not(None),
        effective_rating_count.is_not(None),
        effective_rating_count > 0,
    )
    prod_agg = (
        await session.execute(
            select(
                func.count().label("products"),
                func.count(Product.price).label("products_with_price"),
                func.coalesce(func.sum(Product.sold_count), 0).label("sold_total"),
                func.min(Product.price).label("price_min"),
                func.max(Product.price).label("price_max"),
                func.percentile_cont(0.5).within_group(Product.price.asc()).label("price_median"),
                func.sum(Product.rating_avg * effective_rating_count)
                .filter(*weighted_filter)
                .label("weighted_rating_num"),
                func.sum(effective_rating_count)
                .filter(*weighted_filter)
                .label("weighted_rating_den"),
                func.coalesce(func.sum(Product.review_count), 0).label("review_count_reported"),
            ).where(Product.id.in_(product_ids))
        )
    ).one()

    weighted_num = prod_agg.weighted_rating_num
    weighted_den = prod_agg.weighted_rating_den
    rating_avg_weighted = (
        round(float(weighted_num) / float(weighted_den), 2)
        if weighted_num is not None and weighted_den is not None and float(weighted_den) > 0
        else None
    )

    # 2. Aggregate overall review sentiment & sentiment_source counts in Postgres.
    rev_totals = (
        await session.execute(
            select(
                func.count().label("reviews_collected"),
                func.count().filter(Review.sentiment == "positive").label("pos"),
                func.count().filter(Review.sentiment == "neutral").label("neu"),
                func.count().filter(Review.sentiment == "negative").label("neg"),
                func.count().filter(Review.sentiment.is_(None)).label("unanalyzed"),
                func.count().filter(Review.sentiment_source == "llm").label("src_llm"),
                func.count().filter(Review.sentiment_source == "rating").label("src_rating"),
                func.count().filter(Review.sentiment_source.is_(None)).label("src_none"),
            ).where(Review.product_id.in_(product_ids))
        )
    ).one()

    reviews_collected = int(rev_totals.reviews_collected or 0)
    review_count_reported = int(prod_agg.review_count_reported or 0)

    # 3. Star rating distribution (always 5 down to 1).
    star_rows = (
        await session.execute(
            select(Review.rating, func.count().label("cnt"))
            .where(Review.product_id.in_(product_ids))
            .group_by(Review.rating)
        )
    ).all()
    counts_by_star: dict[int, int] = {int(r.rating): int(r.cnt or 0) for r in star_rows}
    rating_distribution = [
        RatingBucket(
            stars=star,
            count=counts_by_star.get(star, 0),
            pct=_compute_pct(counts_by_star.get(star, 0), reviews_collected),
        )
        for star in range(5, 0, -1)
    ]

    # 4. Per-product review aggregates (counts, sentiment, unanalyzed, last_review_at).
    per_prod_rows = (
        await session.execute(
            select(
                Review.product_id.label("product_id"),
                func.count().label("reviews_collected"),
                func.count().filter(Review.sentiment == "positive").label("pos"),
                func.count().filter(Review.sentiment == "neutral").label("neu"),
                func.count().filter(Review.sentiment == "negative").label("neg"),
                func.count().filter(Review.sentiment.is_(None)).label("unanalyzed"),
                func.max(Review.created_at_source).label("last_review_at"),
            )
            .where(Review.product_id.in_(product_ids))
            .group_by(Review.product_id)
        )
    ).all()
    per_prod_map: dict[uuid.UUID, Any] = {row.product_id: row for row in per_prod_rows}

    # 5. Chunk counts grouped by source_type.
    chunk_rows = (
        await session.execute(
            select(Chunk.source_type, func.count().label("cnt"))
            .where(Chunk.product_id.in_(product_ids))
            .group_by(Chunk.source_type)
        )
    ).all()
    chunks_by_type: dict[str, int] = {}
    chunks_total = 0
    for row in chunk_rows:
        cnt = int(row.cnt or 0)
        chunks_by_type[str(row.source_type)] = cnt
        chunks_total += cnt

    # 6. Aspects aggregated in Postgres via LATERAL jsonb_array_elements.
    safe_aspects_array = case(
        (func.jsonb_typeof(Review.aspects) == "array", Review.aspects),
        else_=literal_column("'[]'::jsonb"),
    )
    aspect_elem = (
        func.jsonb_array_elements(safe_aspects_array).table_valued("value").lateral("aspect_elem")
    )
    aspect_name_expr = func.lower(
        func.btrim(func.coalesce(aspect_elem.c.value.op("->>")("aspect"), ""))
    )
    aspect_sent_expr = func.lower(
        func.btrim(func.coalesce(aspect_elem.c.value.op("->>")("sentiment"), ""))
    )
    aspect_rows = (
        await session.execute(
            select(
                aspect_sent_expr.label("sentiment"),
                aspect_name_expr.label("aspect"),
                func.count().label("cnt"),
            )
            .select_from(Review)
            .join(aspect_elem, true())
            .where(
                Review.product_id.in_(product_ids),
                func.jsonb_typeof(aspect_elem.c.value) == "object",
                aspect_name_expr != "",
            )
            .group_by(aspect_sent_expr, aspect_name_expr)
            .order_by(func.count().desc(), aspect_name_expr.asc())
        )
    ).all()

    top_positive: list[AspectCount] = []
    top_negative: list[AspectCount] = []
    has_any_aspects = False
    for row in aspect_rows:
        name = str(row.aspect or "").strip()
        sent = str(row.sentiment or "").strip().lower()
        cnt = int(row.cnt or 0)
        if not name or cnt <= 0:
            continue
        has_any_aspects = True
        if sent == "positive" and len(top_positive) < effective_top_n:
            top_positive.append(AspectCount(aspect=name, count=cnt))
        elif sent == "negative" and len(top_negative) < effective_top_n:
            top_negative.append(AspectCount(aspect=name, count=cnt))

    # Build per-product report rows, sorted by sold_count DESC NULLS LAST.
    product_rows: list[ProductReportRow] = []
    unanalyzed_product_ids: list[uuid.UUID] = []

    for prod in products:
        agg = per_prod_map.get(prod.id)
        prod_collected = int(agg.reviews_collected or 0) if agg is not None else 0
        if prod_collected > 0 and agg is not None:
            pos = int(agg.pos or 0)
            neu = int(agg.neu or 0)
            neg = int(agg.neg or 0)
            if int(agg.unanalyzed or 0) > 0:
                unanalyzed_product_ids.append(prod.id)
            last_review_at = agg.last_review_at
        else:
            pos = int(prod.positive_review_count or 0)
            neu = int(prod.neutral_review_count or 0)
            neg = int(prod.negative_review_count or 0)
            last_review_at = None

        first_image: str | None = None
        raw_images = getattr(prod, "images", None)
        if isinstance(raw_images, list) and raw_images:
            candidate = raw_images[0]
            if isinstance(candidate, str) and candidate.strip():
                first_image = candidate.strip()
            elif isinstance(candidate, dict):
                for key in ("base_url", "large_url", "medium_url", "url"):
                    val = candidate.get(key)
                    if isinstance(val, str) and val.strip():
                        first_image = val.strip()
                        break

        analyzed = pos + neu + neg
        product_rows.append(
            ProductReportRow(
                product_id=prod.id,
                external_id=prod.external_id,
                title=prod.title,
                brand=prod.brand,
                category=prod.category,
                url=prod.url,
                image_url=first_image,
                short_description=getattr(prod, "short_description", None),
                price=_to_number(prod.price),
                list_price=_to_number(prod.list_price),
                discount_pct=_compute_discount_pct(prod.price, prod.list_price),
                sold_count=prod.sold_count,
                rating_avg=float(prod.rating_avg) if prod.rating_avg is not None else None,
                rating_count=prod.rating_count,
                rating_count_effective=(
                    prod.rating_count if prod.rating_count is not None else prod.review_count
                ),
                review_count_reported=prod.review_count,
                reviews_collected=prod_collected,
                coverage_pct=_compute_pct(prod_collected, prod.review_count),
                positive=pos,
                neutral=neu,
                negative=neg,
                positive_pct=_compute_pct(pos, analyzed),
                last_review_at=last_review_at,
            )
        )

    product_rows.sort(
        key=lambda r: (
            r.sold_count is None,
            -(r.sold_count if r.sold_count is not None else 0),
            r.rating_count_effective is None,
            -(r.rating_count_effective if r.rating_count_effective is not None else 0),
            r.title.lower(),
            str(r.product_id),
        )
    )

    # Keep unanalyzed_product_ids ordered consistently with product_rows.
    unanalyzed_set = set(unanalyzed_product_ids)
    ordered_unanalyzed_ids = [r.product_id for r in product_rows if r.product_id in unanalyzed_set]

    totals = ReportTotals(
        products=int(prod_agg.products or 0),
        products_with_price=int(prod_agg.products_with_price or 0),
        sold_total=int(prod_agg.sold_total or 0),
        price_min=_to_number(prod_agg.price_min),
        price_max=_to_number(prod_agg.price_max),
        price_median=_to_number(prod_agg.price_median),
        rating_avg_weighted=rating_avg_weighted,
        review_count_reported=review_count_reported,
        reviews_collected=reviews_collected,
        reviews_rating_only=int(rev_totals.src_rating or 0),
        coverage_pct=_compute_pct(reviews_collected, review_count_reported),
        sentiment=SentimentCounts(
            positive=int(rev_totals.pos or 0),
            neutral=int(rev_totals.neu or 0),
            negative=int(rev_totals.neg or 0),
            unanalyzed=int(rev_totals.unanalyzed or 0),
        ),
        sentiment_source=SentimentSourceCounts(
            llm=int(rev_totals.src_llm or 0),
            rating=int(rev_totals.src_rating or 0),
            none=int(rev_totals.src_none or 0),
        ),
        chunks=ChunkCounts(total=chunks_total, by_type=chunks_by_type),
    )

    rankings = _build_rankings(product_rows, effective_top_n)
    aspects = ReportAspects(top_positive=top_positive, top_negative=top_negative)
    warnings = _build_warnings(
        source=source,
        totals=totals,
        product_rows=product_rows,
        unanalyzed_product_ids=ordered_unanalyzed_ids,
        has_any_aspects=has_any_aspects,
        ttl_hours=ttl_hours,
        now_utc=now_utc,
    )

    return SourceReport(
        source=source_info,
        shop=shop_info,
        totals=totals,
        rating_distribution=rating_distribution,
        products=product_rows,
        rankings=rankings,
        aspects=aspects,
        warnings=warnings,
        generated_at=now_utc,
    )
