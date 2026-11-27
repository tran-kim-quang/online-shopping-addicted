"""Normalized Pydantic schemas for URL classification, crawled entities, and results."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field


class ParsedURL(BaseModel):
    """Classified e-commerce URL pointing to a product or shop."""

    platform: str
    kind: Literal["product", "shop"]
    product_id: str | None = None
    spid: str | None = None
    shop_slug: str | None = None
    seller_id: str | None = None
    canonical_url: str


class ShopData(BaseModel):
    """Normalized seller/shop payload produced by a platform adapter."""

    platform: str
    external_id: str
    name: str
    url: str
    rating: float | None = None
    follower_count: int | None = None
    product_count: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class ProductData(BaseModel):
    """Normalized product payload produced by a platform adapter."""

    platform: str
    external_id: str
    title: str
    url: str
    spid: str | None = None
    price: Decimal | None = None
    list_price: Decimal | None = None
    currency: str = "VND"
    sold_count: int | None = None
    sold_text: str | None = None
    rating_avg: float | None = None
    rating_count: int | None = None
    review_count: int | None = None
    category: str | None = None
    brand: str | None = None
    short_description: str | None = None
    description_html: str | None = None
    description_text: str | None = None
    specs: dict[str, str] = Field(default_factory=dict)
    images: list[str] = Field(default_factory=list)
    shop: ShopData | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class ReviewData(BaseModel):
    """Normalized customer review payload produced by a platform adapter."""

    external_id: str
    rating: int
    title: str | None = None
    content: str | None = None
    author: str | None = None
    created_at_source: datetime | None = None
    helpful_count: int | None = None
    variant: str | None = None
    tags: list[str] = Field(default_factory=list)
    sub_ratings: dict[str, int] = Field(default_factory=dict)
    seller_reply: str | None = None
    media_count: int = 0
    quality_score: float | None = None
    images: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


class CrawlStats(BaseModel):
    """Aggregated counters and per-item errors from a crawl run."""

    products_total: int = 0
    products_succeeded: int = 0
    products_failed: int = 0
    reviews_count: int = 0
    errors: list[dict[str, Any]] = Field(default_factory=list)
    duration_seconds: float = 0.0


class ProgressEvent(BaseModel):
    """Progress update emitted during crawling (wired to SSE in Phase 3)."""

    stage: str
    done: int
    total: int | None = None
    message: str = ""


class CrawlResult(BaseModel):
    """Final outcome of ``crawl_url``."""

    source_id: uuid.UUID | None = None
    status: Literal["pending", "running", "done", "failed", "partial"]
    shop_id: uuid.UUID | None = None
    product_ids: list[uuid.UUID] = Field(default_factory=list)
    stats: CrawlStats = Field(default_factory=CrawlStats)
    error: str | None = None
    preview_product: ProductData | None = Field(default=None, exclude=True)
    preview_reviews: list[ReviewData] = Field(default_factory=list, exclude=True)
