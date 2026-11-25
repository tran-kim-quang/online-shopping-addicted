"""Create pgvector extension and llm_profiles table.

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-25 17:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "llm_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            sa.String(length=128),
            nullable=False,
            server_default="local",
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("base_url", sa.String(length=2048), nullable=False),
        sa.Column("api_key_enc", sa.Text(), nullable=True),
        sa.Column("chat_model", sa.String(length=255), nullable=False),
        sa.Column("small_model", sa.String(length=255), nullable=True),
        sa.Column("embedding_model", sa.String(length=255), nullable=True),
        sa.Column(
            "max_context_tokens",
            sa.Integer(),
            nullable=False,
            server_default="32000",
        ),
        sa.Column(
            "capabilities",
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
    )
    op.create_index("ix_llm_profiles_user_id", "llm_profiles", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_llm_profiles_user_id", table_name="llm_profiles")
    op.drop_table("llm_profiles")
