"""add account asset tier

Revision ID: 009_add_account_asset_tier
Revises: 008_add_account_profile_bio
Create Date: 2026-07-05
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "009_add_account_asset_tier"
down_revision = "008_add_account_profile_bio"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "telegram_account",
        sa.Column("asset_tier", sa.String(length=30), nullable=False, server_default="unknown"),
    )
    op.add_column("telegram_account", sa.Column("registered_at", sa.DateTime(), nullable=True))
    op.add_column("telegram_account", sa.Column("asset_verified_at", sa.DateTime(), nullable=True))
    op.add_column("telegram_account", sa.Column("asset_note", sa.String(length=255), nullable=True))
    op.create_index("idx_account_asset_tier", "telegram_account", ["asset_tier"])
    op.create_index("idx_account_registered_at", "telegram_account", ["registered_at"])
    op.alter_column("telegram_account", "asset_tier", server_default=None)


def downgrade() -> None:
    op.drop_index("idx_account_registered_at", table_name="telegram_account")
    op.drop_index("idx_account_asset_tier", table_name="telegram_account")
    op.drop_column("telegram_account", "asset_note")
    op.drop_column("telegram_account", "asset_verified_at")
    op.drop_column("telegram_account", "registered_at")
    op.drop_column("telegram_account", "asset_tier")
