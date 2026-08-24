"""add group ad delivery account

Revision ID: 021_add_group_ad_delivery_account
Revises: 020_add_ad_policy_evidence_hash
Create Date: 2026-08-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "021_add_group_ad_delivery_account"
down_revision = "020_add_ad_policy_evidence_hash"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "group",
        sa.Column(
            "ad_delivery_account_id",
            sa.Integer(),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_group_ad_delivery_account",
        "group",
        "telegram_account",
        ["ad_delivery_account_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("idx_group_ad_delivery_account", "group", ["ad_delivery_account_id"])


def downgrade() -> None:
    op.drop_index("idx_group_ad_delivery_account", table_name="group")
    op.drop_constraint("fk_group_ad_delivery_account", "group", type_="foreignkey")
    op.drop_column("group", "ad_delivery_account_id")
