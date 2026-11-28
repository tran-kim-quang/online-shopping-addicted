"""Add max_credits_per_search to data_provider_profiles and create trend_searches table.

Revision ID: 0007_scrapecreators_trends
Revises: 0006_product_rating_breakdown
Create Date: 2026-11-27 17:20:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_scrapecreators_trends"
down_revision: str | Sequence[str] | None = "0006_product_rating_breakdown"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "data_provider_profiles",
        sa.Column("max_credits_per_search", sa.SmallInteger(), nullable=True),
    )
    op.create_check_constraint(
        "ck_data_provider_profiles_max_credits",
        "data_provider_profiles",
        "max_credits_per_search IS NULL OR (max_credits_per_search BETWEEN 5 AND 100)",
    )

    op.create_table(
        "trend_searches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            sa.String(length=128),
            nullable=False,
            server_default="local",
        ),
        sa.Column("keyword", sa.String(length=100), nullable=False),
        sa.Column("keyword_norm", sa.String(length=100), nullable=False),
        sa.Column(
            "platform",
            sa.String(length=16),
            nullable=False,
            server_default="tiktok",
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="queued",
        ),
        sa.Column(
            "data_provider_profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("data_provider_profiles.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "llm_profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("llm_profiles.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("job_id", sa.String(length=255), nullable=True),
        sa.Column("credits_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("videos_scanned", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("comments_scanned", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "warnings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'cancelled')",
            name="ck_trend_searches_status",
        ),
    )
    op.create_index(
        "ix_trend_searches_keyword_norm",
        "trend_searches",
        ["keyword_norm"],
    )
    op.create_index(
        "ix_trend_searches_user_keyword_created_at",
        "trend_searches",
        ["user_id", "keyword_norm", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_trend_searches_user_keyword_created_at", table_name="trend_searches")
    op.drop_index("ix_trend_searches_keyword_norm", table_name="trend_searches")
    op.drop_table("trend_searches")
    op.drop_constraint(
        "ck_data_provider_profiles_max_credits",
        "data_provider_profiles",
        type_="check",
    )
    op.drop_column("data_provider_profiles", "max_credits_per_search")
