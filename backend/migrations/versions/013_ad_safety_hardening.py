"""harden ad policy, delivery, and survival lifecycle

Revision ID: 013_ad_safety_hardening
Revises: 012_ad_policy_lifecycle
Create Date: 2026-07-11
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "013_ad_safety_hardening"
down_revision = "012_ad_policy_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "telegram_account_operation_config",
        sa.Column("last_group_cleanup_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "ad_delivery_log",
        sa.Column("survival_retry_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "ad_delivery_log",
        sa.Column("reservation_token", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "idx_ad_delivery_reservation",
        "ad_delivery_log",
        ["reservation_token"],
        unique=True,
    )

    op.create_table(
        "group_ad_policy_event",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("telegram_group_id", sa.BigInteger(), nullable=False),
        sa.Column("previous_mode", sa.String(length=40), nullable=True),
        sa.Column("new_mode", sa.String(length=40), nullable=False),
        sa.Column("confidence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("changed_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["group_id"], ["group.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_group_ad_policy_event_group",
        "group_ad_policy_event",
        ["group_id", "created_at"],
    )
    op.create_index(
        "idx_group_ad_policy_event_mode",
        "group_ad_policy_event",
        ["new_mode", "created_at"],
    )

    op.execute(
        """
        INSERT INTO group_ad_profile (
            group_id,
            telegram_group_id,
            ad_policy_mode,
            ad_policy_confidence,
            ad_tier,
            daily_capacity,
            score,
            survival_count,
            deleted_count,
            consecutive_survivals,
            consecutive_deletions,
            created_at,
            updated_at
        )
        SELECT
            g.id,
            g.group_id,
            'unknown',
            0,
            'observing',
            0,
            0,
            0,
            0,
            0,
            0,
            now(),
            now()
        FROM "group" AS g
        WHERE NOT EXISTS (
            SELECT 1 FROM group_ad_profile AS p WHERE p.group_id = g.id
        )
        """
    )
    op.execute(
        """
        UPDATE group_ad_profile
        SET ad_tier = 'observing', daily_capacity = 0, updated_at = now()
        WHERE ad_policy_mode IN ('unknown', 'approval_required')
        """
    )


def downgrade() -> None:
    op.drop_index("idx_group_ad_policy_event_mode", table_name="group_ad_policy_event")
    op.drop_index("idx_group_ad_policy_event_group", table_name="group_ad_policy_event")
    op.drop_table("group_ad_policy_event")
    op.drop_index("idx_ad_delivery_reservation", table_name="ad_delivery_log")
    op.drop_column("ad_delivery_log", "reservation_token")
    op.drop_column("ad_delivery_log", "survival_retry_count")
    op.drop_column("telegram_account_operation_config", "last_group_cleanup_at")
