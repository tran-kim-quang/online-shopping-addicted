"""Add chat_sessions, chat_messages, and review_summaries tables.

Revision ID: 0004_chat
Revises: 0003_ingestion
Create Date: 2026-11-26 09:45:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_chat"
down_revision: str | Sequence[str] | None = "0003_ingestion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            sa.String(length=128),
            nullable=False,
            server_default="local",
        ),
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "llm_profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("llm_profiles.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("title", sa.Text(), nullable=True),
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
    op.create_index("ix_chat_sessions_source_id", "chat_sessions", ["source_id"])

    op.create_table(
        "chat_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "tool_calls",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "citations",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "usage",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "role IN ('user', 'assistant')",
            name="ck_chat_messages_role",
        ),
    )
    op.create_index(
        "ix_chat_messages_session_id_created_at",
        "chat_messages",
        ["session_id", "created_at"],
    )

    op.create_table(
        "review_summaries",
        sa.Column(
            "product_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("products.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("reviews_fingerprint", sa.CHAR(length=64), nullable=False),
        sa.Column(
            "summary",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("review_summaries")
    op.drop_index("ix_chat_messages_session_id_created_at", table_name="chat_messages")
    op.drop_table("chat_messages")
    op.drop_index("ix_chat_sessions_source_id", table_name="chat_sessions")
    op.drop_table("chat_sessions")
