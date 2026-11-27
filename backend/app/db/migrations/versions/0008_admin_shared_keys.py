"""Add admin shared key columns, partial unique indexes, and user history tables.

Revision ID: 0008_admin_shared_keys
Revises: 0007_scrapecreators_trends
Create Date: 2026-11-27 21:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_admin_shared_keys"
down_revision: str | Sequence[str] | None = "0007_scrapecreators_trends"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "llm_profiles",
        sa.Column(
            "shared_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "llm_profiles",
        sa.Column("daily_limit", sa.Numeric(12, 4), nullable=True),
    )
    op.create_index(
        "uq_llm_profiles_system",
        "llm_profiles",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("user_id = 'system'"),
    )

    op.add_column(
        "data_provider_profiles",
        sa.Column(
            "shared_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "data_provider_profiles",
        sa.Column("daily_limit", sa.Numeric(12, 4), nullable=True),
    )
    op.create_index(
        "uq_data_provider_profiles_system_provider",
        "data_provider_profiles",
        ["user_id", "provider"],
        unique=True,
        postgresql_where=sa.text("user_id = 'system'"),
    )

    op.create_table(
        "user_sources",
        sa.Column("user_id", sa.String(length=128), primary_key=True, nullable=False),
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sources.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "first_seen",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_seen",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_user_sources_user_last_seen",
        "user_sources",
        ["user_id", sa.text("last_seen DESC")],
    )

    op.create_table(
        "user_trend_searches",
        sa.Column("user_id", sa.String(length=128), primary_key=True, nullable=False),
        sa.Column(
            "trend_search_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("trend_searches.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "first_seen",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_seen",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_user_trend_searches_user_last_seen",
        "user_trend_searches",
        ["user_id", sa.text("last_seen DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_user_trend_searches_user_last_seen", table_name="user_trend_searches")
    op.drop_table("user_trend_searches")

    op.drop_index("ix_user_sources_user_last_seen", table_name="user_sources")
    op.drop_table("user_sources")

    op.drop_index(
        "uq_data_provider_profiles_system_provider",
        table_name="data_provider_profiles",
    )
    op.drop_column("data_provider_profiles", "daily_limit")
    op.drop_column("data_provider_profiles", "shared_enabled")

    op.drop_index("uq_llm_profiles_system", table_name="llm_profiles")
    op.drop_column("llm_profiles", "daily_limit")
    op.drop_column("llm_profiles", "shared_enabled")
