"""add coupon distribution batch key

Revision ID: 011_coupon_batch_key
Revises: 010_add_account_managed_warmup
Create Date: 2026-07-10
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "011_coupon_batch_key"
down_revision = "010_add_account_managed_warmup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "coupon_distribution",
        sa.Column("batch_key", sa.String(length=100), nullable=False, server_default="default", comment="发券批次标识"),
    )
    op.execute(
        """
        UPDATE coupon_distribution
        SET batch_key = COALESCE(campaign_id::text, 'default')
        WHERE batch_key = 'default'
        """
    )
    op.create_index("idx_coupon_batch", "coupon_distribution", ["campaign_id", "batch_key"])
    op.create_unique_constraint(
        "uq_coupon_user_campaign_batch",
        "coupon_distribution",
        ["user_id", "campaign_id", "batch_key"],
    )
    op.alter_column("coupon_distribution", "batch_key", server_default=None)


def downgrade() -> None:
    op.drop_constraint("uq_coupon_user_campaign_batch", "coupon_distribution", type_="unique")
    op.drop_index("idx_coupon_batch", table_name="coupon_distribution")
    op.drop_column("coupon_distribution", "batch_key")
