"""Add data_provider_profiles table and Shopee/enriched review columns.

Revision ID: 0005_data_providers
Revises: 0004_chat
Create Date: 2026-11-27 09:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_data_providers"
down_revision: str | Sequence[str] | None = "0004_chat"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "data_provider_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            sa.String(length=128),
            nullable=False,
            server_default="local",
        ),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("api_token_enc", sa.Text(), nullable=False),
        sa.Column(
            "max_cost_usd_per_link",
            sa.Numeric(precision=6, scale=2),
            nullable=False,
            server_default="0.50",
        ),
        sa.Column(
            "account_info",
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
        sa.CheckConstraint(
            "max_cost_usd_per_link >= 0.05 AND max_cost_usd_per_link <= 5.00",
            name="ck_data_provider_profiles_max_cost",
        ),
    )
    op.create_index(
        "ix_data_provider_profiles_user_id",
        "data_provider_profiles",
        ["user_id"],
    )

    op.add_column("reviews", sa.Column("variant", sa.Text(), nullable=True))
    op.add_column(
        "reviews",
        sa.Column(
            "tags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "reviews",
        sa.Column(
            "sub_ratings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column("reviews", sa.Column("seller_reply", sa.Text(), nullable=True))
    op.add_column(
        "reviews",
        sa.Column("media_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("reviews", sa.Column("quality_score", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("reviews", "quality_score")
    op.drop_column("reviews", "media_count")
    op.drop_column("reviews", "seller_reply")
    op.drop_column("reviews", "sub_ratings")
    op.drop_column("reviews", "tags")
    op.drop_column("reviews", "variant")
    op.drop_index("ix_data_provider_profiles_user_id", table_name="data_provider_profiles")
    op.drop_table("data_provider_profiles")
