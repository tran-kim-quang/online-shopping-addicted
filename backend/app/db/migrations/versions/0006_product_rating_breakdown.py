"""Add rating_breakdown JSONB column to products.

Revision ID: 0006_product_rating_breakdown
Revises: 0005_data_providers
Create Date: 2026-11-27 12:30:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_product_rating_breakdown"
down_revision: str | Sequence[str] | None = "0005_data_providers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column(
            "rating_breakdown",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("products", "rating_breakdown")
