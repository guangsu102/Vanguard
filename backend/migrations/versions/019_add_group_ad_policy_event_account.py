"""add account to group ad policy events

Revision ID: 019_add_group_ad_policy_event_account
Revises: 018_add_ad_policy_probe
Create Date: 2026-08-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "019_add_group_ad_policy_event_account"
down_revision = "018_add_ad_policy_probe"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "group_ad_policy_event",
        sa.Column("account_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_group_ad_policy_event_account",
        "group_ad_policy_event",
        "telegram_account",
        ["account_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "idx_group_ad_policy_event_account",
        "group_ad_policy_event",
        ["account_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_group_ad_policy_event_account", table_name="group_ad_policy_event")
    op.drop_constraint("fk_group_ad_policy_event_account", "group_ad_policy_event", type_="foreignkey")
    op.drop_column("group_ad_policy_event", "account_id")
