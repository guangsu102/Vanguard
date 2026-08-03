"""add ad capacity and survival tracking

Revision ID: 007_add_ad_capacity_survival
Revises: 006_add_account_business_stage
Create Date: 2026-07-05
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "007_add_ad_capacity_survival"
down_revision = "006_add_account_business_stage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "group_ad_profile",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("telegram_group_id", sa.BigInteger(), nullable=False),
        sa.Column("ad_tier", sa.String(length=30), nullable=False, server_default="low"),
        sa.Column("daily_capacity", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("survival_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deleted_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("consecutive_survivals", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("consecutive_deletions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_probe_at", sa.DateTime(), nullable=True),
        sa.Column("last_survived_at", sa.DateTime(), nullable=True),
        sa.Column("last_deleted_at", sa.DateTime(), nullable=True),
        sa.Column("blocked_at", sa.DateTime(), nullable=True),
        sa.Column("blocked_reason", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["group_id"], ["group.id"], name=op.f("fk_group_ad_profile_group_id_group"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_group_ad_profile")),
        sa.UniqueConstraint("group_id", name="uq_group_ad_profile_group"),
    )
    op.create_index("idx_group_ad_profile_tg_group", "group_ad_profile", ["telegram_group_id"], unique=False)
    op.create_index("idx_group_ad_profile_tier", "group_ad_profile", ["ad_tier"], unique=False)
    op.create_index("idx_group_ad_profile_blocked", "group_ad_profile", ["blocked_at"], unique=False)

    op.add_column(
        "group_account_membership",
        sa.Column("ad_status", sa.String(length=40), nullable=False, server_default="warming"),
    )
    op.add_column(
        "group_account_membership",
        sa.Column("account_group_daily_cap", sa.Integer(), nullable=False, server_default="400"),
    )
    op.add_column("group_account_membership", sa.Column("interaction_started_at", sa.DateTime(), nullable=True))
    op.add_column(
        "group_account_membership",
        sa.Column("interaction_sent_today", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("group_account_membership", sa.Column("first_ad_allowed_at", sa.DateTime(), nullable=True))
    op.add_column("group_account_membership", sa.Column("last_ad_survived_at", sa.DateTime(), nullable=True))
    op.add_column("group_account_membership", sa.Column("last_ad_deleted_at", sa.DateTime(), nullable=True))
    op.create_index("idx_group_membership_ad_status", "group_account_membership", ["account_id", "ad_status"], unique=False)

    op.add_column(
        "ad_delivery_log",
        sa.Column("survival_status", sa.String(length=30), nullable=False, server_default="not_required"),
    )
    op.add_column("ad_delivery_log", sa.Column("survival_check_due_at", sa.DateTime(), nullable=True))
    op.add_column("ad_delivery_log", sa.Column("survival_checked_at", sa.DateTime(), nullable=True))
    op.add_column("ad_delivery_log", sa.Column("survival_error", sa.Text(), nullable=True))
    op.create_index("idx_ad_delivery_survival_due", "ad_delivery_log", ["survival_status", "survival_check_due_at"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_ad_delivery_survival_due", table_name="ad_delivery_log")
    op.drop_column("ad_delivery_log", "survival_error")
    op.drop_column("ad_delivery_log", "survival_checked_at")
    op.drop_column("ad_delivery_log", "survival_check_due_at")
    op.drop_column("ad_delivery_log", "survival_status")

    op.drop_index("idx_group_membership_ad_status", table_name="group_account_membership")
    op.drop_column("group_account_membership", "last_ad_deleted_at")
    op.drop_column("group_account_membership", "last_ad_survived_at")
    op.drop_column("group_account_membership", "first_ad_allowed_at")
    op.drop_column("group_account_membership", "interaction_sent_today")
    op.drop_column("group_account_membership", "interaction_started_at")
    op.drop_column("group_account_membership", "account_group_daily_cap")
    op.drop_column("group_account_membership", "ad_status")

    op.drop_index("idx_group_ad_profile_blocked", table_name="group_ad_profile")
    op.drop_index("idx_group_ad_profile_tier", table_name="group_ad_profile")
    op.drop_index("idx_group_ad_profile_tg_group", table_name="group_ad_profile")
    op.drop_table("group_ad_profile")
