"""add account managed warmup fields

Revision ID: 010_add_account_managed_warmup
Revises: 009_add_account_asset_tier
Create Date: 2026-07-07
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "010_add_account_managed_warmup"
down_revision = "009_add_account_asset_tier"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("telegram_account", sa.Column("managed_started_at", sa.DateTime(), nullable=True))
    op.add_column(
        "telegram_account",
        sa.Column("warmup_stage", sa.String(length=20), nullable=False, server_default="observe"),
    )
    op.add_column("telegram_account", sa.Column("warmup_stage_updated_at", sa.DateTime(), nullable=True))
    op.add_column("telegram_account", sa.Column("warmup_hold_until", sa.DateTime(), nullable=True))
    op.add_column("telegram_account", sa.Column("warmup_note", sa.String(length=255), nullable=True))

    op.execute(
        """
        UPDATE telegram_account
        SET managed_started_at = COALESCE(managed_started_at, created_at, now())
        WHERE managed_started_at IS NULL
        """
    )
    op.execute(
        """
        UPDATE telegram_account
        SET warmup_stage = CASE
            WHEN managed_started_at <= now() - interval '15 days' THEN 'normal'
            ELSE 'observe'
        END,
            warmup_stage_updated_at = COALESCE(warmup_stage_updated_at, now())
        """
    )

    op.create_index("idx_account_managed_started_at", "telegram_account", ["managed_started_at"])
    op.create_index("idx_account_warmup_stage", "telegram_account", ["warmup_stage"])
    op.alter_column("telegram_account", "warmup_stage", server_default=None)


def downgrade() -> None:
    op.drop_index("idx_account_warmup_stage", table_name="telegram_account")
    op.drop_index("idx_account_managed_started_at", table_name="telegram_account")
    op.drop_column("telegram_account", "warmup_note")
    op.drop_column("telegram_account", "warmup_hold_until")
    op.drop_column("telegram_account", "warmup_stage_updated_at")
    op.drop_column("telegram_account", "warmup_stage")
    op.drop_column("telegram_account", "managed_started_at")
