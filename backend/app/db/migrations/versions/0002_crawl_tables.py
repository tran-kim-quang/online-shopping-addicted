"""Create sources, shops, products, and reviews tables.

Revision ID: 0002_crawl_tables
Revises: 0001_initial
Create Date: 2026-11-25 19:55:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_crawl_tables"
down_revision: str | Sequence[str] | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("url_type", sa.String(length=32), nullable=True),
        sa.Column("platform", sa.String(length=64), nullable=True),
        sa.Column(
            "external_ref",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "stats",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("crawled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "url_type IS NULL OR url_type IN ('shop', 'product')",
            name="ck_sources_url_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'done', 'failed', 'partial')",
            name="ck_sources_status",
        ),
    )

    op.create_table(
        "shops",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("platform", sa.String(length=64), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("rating", sa.Float(), nullable=True),
        sa.Column("follower_count", sa.Integer(), nullable=True),
        sa.Column("product_count", sa.Integer(), nullable=True),
        sa.Column(
            "raw",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "crawled_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("platform", "external_id", name="uq_shops_platform_external_id"),
    )

    op.create_table(
        "products",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "shop_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("shops.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("platform", sa.String(length=64), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("price", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("list_price", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column(
            "currency",
            sa.String(length=16),
            nullable=False,
            server_default="VND",
        ),
        sa.Column("sold_count", sa.Integer(), nullable=True),
        sa.Column("sold_text", sa.Text(), nullable=True),
        sa.Column("rating_avg", sa.Float(), nullable=True),
        sa.Column("rating_count", sa.Integer(), nullable=True),
        sa.Column("review_count", sa.Integer(), nullable=True),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("brand", sa.String(length=255), nullable=True),
        sa.Column("short_description", sa.Text(), nullable=True),
        sa.Column("description_html", sa.Text(), nullable=True),
        sa.Column("description_text", sa.Text(), nullable=True),
        sa.Column(
            "specs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "images",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "raw",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "crawled_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("platform", "external_id", name="uq_products_platform_external_id"),
    )
    op.create_index("ix_products_shop_id", "products", ["shop_id"])
    op.create_index(
        "ix_products_platform_sold_count_desc",
        "products",
        ["platform", sa.text("sold_count DESC")],
    )

    op.create_table(
        "reviews",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "product_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("products.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("author", sa.String(length=255), nullable=True),
        sa.Column("created_at_source", sa.DateTime(timezone=True), nullable=True),
        sa.Column("helpful_count", sa.Integer(), nullable=True),
        sa.Column(
            "images",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "raw",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "crawled_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("product_id", "external_id", name="uq_reviews_product_external_id"),
    )
    op.create_index("ix_reviews_product_id_rating", "reviews", ["product_id", "rating"])


def downgrade() -> None:
    op.drop_index("ix_reviews_product_id_rating", table_name="reviews")
    op.drop_table("reviews")

    op.drop_index("ix_products_platform_sold_count_desc", table_name="products")
    op.drop_index("ix_products_shop_id", table_name="products")
    op.drop_table("products")

    op.drop_table("shops")
    op.drop_table("sources")
