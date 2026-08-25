"""add ad delivery policy and durable scheduler state

Revision ID: 022_add_ad_delivery_policy_scheduler
Revises: 021_add_group_ad_delivery_account
Create Date: 2026-08-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "022_add_ad_delivery_policy_scheduler"
down_revision = "021_add_group_ad_delivery_account"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ad_campaign",
        sa.Column(
            "delivery_policy",
            sa.String(length=20),
            server_default="growth",
            nullable=False,
            comment="delivery policy: growth/ad_only",
        ),
    )
    op.create_check_constraint(
        "ck_ad_campaign_delivery_policy",
        "ad_campaign",
        "delivery_policy IN ('growth', 'ad_only')",
    )
    op.create_index(
        "idx_ad_campaign_delivery_policy",
        "ad_campaign",
        ["delivery_policy"],
    )

    op.alter_column(
        "telegram_account_operation_config",
        "max_messages_per_day",
        existing_type=sa.Integer(),
        nullable=True,
    )

    op.create_table(
        "ad_delivery_schedule_state",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("telegram_group_id", sa.BigInteger(), nullable=False),
        sa.Column("next_due_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="idle"),
        sa.Column("lock_token", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_reason", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["campaign_id"], ["ad_campaign.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["account_id"], ["telegram_account.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["group_id"], ["group.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "campaign_id",
            "account_id",
            "group_id",
            name="uq_ad_delivery_schedule_tuple",
        ),
    )
    op.create_index(
        "idx_ad_delivery_schedule_due",
        "ad_delivery_schedule_state",
        ["status", "next_due_at"],
    )
    op.create_index(
        "idx_ad_delivery_schedule_account",
        "ad_delivery_schedule_state",
        ["account_id", "next_due_at"],
    )
    op.create_index(
        "idx_ad_delivery_schedule_lease",
        "ad_delivery_schedule_state",
        ["lease_expires_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_ad_delivery_schedule_lease", table_name="ad_delivery_schedule_state")
    op.drop_index("idx_ad_delivery_schedule_account", table_name="ad_delivery_schedule_state")
    op.drop_index("idx_ad_delivery_schedule_due", table_name="ad_delivery_schedule_state")
    op.drop_table("ad_delivery_schedule_state")

    op.execute(
        "UPDATE telegram_account_operation_config "
        "SET max_messages_per_day = 3 WHERE max_messages_per_day IS NULL"
    )
    op.alter_column(
        "telegram_account_operation_config",
        "max_messages_per_day",
        existing_type=sa.Integer(),
        nullable=False,
    )
    op.drop_index("idx_ad_campaign_delivery_policy", table_name="ad_campaign")
    op.drop_constraint("ck_ad_campaign_delivery_policy", "ad_campaign", type_="check")
    op.drop_column("ad_campaign", "delivery_policy")
