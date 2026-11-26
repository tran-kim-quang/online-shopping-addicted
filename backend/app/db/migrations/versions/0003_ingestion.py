"""Add unaccent/f_unaccent, review sentiment fields, product counts, and chunks table.

Revision ID: 0003_ingestion
Revises: 0002_crawl_tables
Create Date: 2026-11-26 01:35:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0003_ingestion"
down_revision: str | Sequence[str] | None = "0002_crawl_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION f_unaccent(text)
        RETURNS text
        LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT AS
        $func$
        SELECT public.unaccent('public.unaccent', $1)
        $func$
        """
    )

    # Reviews: sentiment analysis & aspect extraction fields.
    op.add_column("reviews", sa.Column("sentiment", sa.String(length=16), nullable=True))
    op.add_column("reviews", sa.Column("sentiment_source", sa.String(length=16), nullable=True))
    op.add_column(
        "reviews",
        sa.Column(
            "aspects",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "reviews",
        sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("reviews", sa.Column("content_hash", sa.String(length=64), nullable=True))
    op.create_check_constraint(
        "ck_reviews_sentiment",
        "reviews",
        "sentiment IS NULL OR sentiment IN ('positive', 'neutral', 'negative')",
    )
    op.create_check_constraint(
        "ck_reviews_sentiment_source",
        "reviews",
        "sentiment_source IS NULL OR sentiment_source IN ('llm', 'rating')",
    )

    # Products: precomputed sentiment counters and ingestion timestamp.
    op.add_column(
        "products",
        sa.Column(
            "positive_review_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "products",
        sa.Column(
            "negative_review_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "products",
        sa.Column(
            "neutral_review_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "products",
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Chunks: searchable text segments with generated tsvector + optional pgvector embedding.
    op.create_table(
        "chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "product_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("products.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "shop_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("shops.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=False, server_default=""),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.CHAR(length=64), nullable=False),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('simple', f_unaccent(coalesce(text, '')))",
                persisted=True,
            ),
            nullable=True,
        ),
        sa.Column("embedding", Vector(), nullable=True),
        sa.Column("embedding_model", sa.Text(), nullable=True),
        sa.Column("dim", sa.Integer(), nullable=True),
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
        sa.CheckConstraint(
            "source_type IN ('description', 'specs', 'review', 'meta')",
            name="ck_chunks_source_type",
        ),
        sa.UniqueConstraint(
            "product_id",
            "source_type",
            "source_ref",
            "content_hash",
            name="uq_chunks_product_source_hash",
        ),
    )

    op.create_index("ix_chunks_tsv", "chunks", ["tsv"], postgresql_using="gin")
    op.create_index("ix_chunks_product_id", "chunks", ["product_id"])
    op.create_index("ix_chunks_shop_id", "chunks", ["shop_id"])
    op.execute(
        """
        CREATE INDEX ix_chunks_embedding_hnsw_1024
        ON chunks
        USING hnsw ((embedding::vector(1024)) vector_cosine_ops)
        WHERE dim = 1024
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw_1024")
    op.drop_index("ix_chunks_shop_id", table_name="chunks")
    op.drop_index("ix_chunks_product_id", table_name="chunks")
    op.drop_index("ix_chunks_tsv", table_name="chunks")
    op.drop_table("chunks")

    op.drop_column("products", "ingested_at")
    op.drop_column("products", "neutral_review_count")
    op.drop_column("products", "negative_review_count")
    op.drop_column("products", "positive_review_count")

    op.drop_constraint("ck_reviews_sentiment_source", "reviews", type_="check")
    op.drop_constraint("ck_reviews_sentiment", "reviews", type_="check")
    op.drop_column("reviews", "content_hash")
    op.drop_column("reviews", "analyzed_at")
    op.drop_column("reviews", "aspects")
    op.drop_column("reviews", "sentiment_source")
    op.drop_column("reviews", "sentiment")

    op.execute("DROP FUNCTION IF EXISTS f_unaccent(text)")
