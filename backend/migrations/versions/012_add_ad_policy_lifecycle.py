"""add evidence-backed ad policy lifecycle

Revision ID: 012_ad_policy_lifecycle
Revises: 011_coupon_batch_key
Create Date: 2026-07-11
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "012_ad_policy_lifecycle"
down_revision = "011_coupon_batch_key"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("group_ad_profile", sa.Column("ad_policy_mode", sa.String(length=40), nullable=False, server_default="unknown"))
    op.add_column("group_ad_profile", sa.Column("ad_policy_confidence", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("group_ad_profile", sa.Column("ad_policy_source", sa.String(length=80), nullable=True))
    op.add_column("group_ad_profile", sa.Column("ad_policy_verified_at", sa.DateTime(), nullable=True))
    op.add_column("group_ad_profile", sa.Column("ad_policy_expires_at", sa.DateTime(), nullable=True))
    op.add_column("group_ad_profile", sa.Column("tier_changed_at", sa.DateTime(), nullable=True))
    op.add_column("group_ad_profile", sa.Column("paused_until", sa.DateTime(), nullable=True))
    op.add_column("group_account_membership", sa.Column("ad_pause_until", sa.DateTime(), nullable=True))
    op.add_column("group_account_membership", sa.Column("ad_failure_streak", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("ad_delivery_log", sa.Column("survival_stage", sa.String(length=30), nullable=False, server_default="two_minute"))
    op.add_column("ad_delivery_log", sa.Column("survived_two_minute_at", sa.DateTime(), nullable=True))
    op.add_column("ad_delivery_log", sa.Column("survived_one_hour_at", sa.DateTime(), nullable=True))
    op.add_column("ad_delivery_log", sa.Column("survived_twenty_four_hour_at", sa.DateTime(), nullable=True))
    op.create_index("idx_group_ad_profile_policy", "group_ad_profile", ["ad_policy_mode", "ad_policy_expires_at"])
    op.create_index("idx_group_membership_ad_pause", "group_account_membership", ["account_id", "ad_pause_until"])
    op.create_index("idx_ad_delivery_survival_stage", "ad_delivery_log", ["survival_stage", "survival_check_due_at"])


def downgrade() -> None:
    op.drop_index("idx_ad_delivery_survival_stage", table_name="ad_delivery_log")
    op.drop_index("idx_group_membership_ad_pause", table_name="group_account_membership")
    op.drop_index("idx_group_ad_profile_policy", table_name="group_ad_profile")
    op.drop_column("ad_delivery_log", "survived_twenty_four_hour_at")
    op.drop_column("ad_delivery_log", "survived_one_hour_at")
    op.drop_column("ad_delivery_log", "survived_two_minute_at")
    op.drop_column("ad_delivery_log", "survival_stage")
    op.drop_column("group_account_membership", "ad_failure_streak")
    op.drop_column("group_account_membership", "ad_pause_until")
    op.drop_column("group_ad_profile", "paused_until")
    op.drop_column("group_ad_profile", "tier_changed_at")
    op.drop_column("group_ad_profile", "ad_policy_expires_at")
    op.drop_column("group_ad_profile", "ad_policy_verified_at")
    op.drop_column("group_ad_profile", "ad_policy_source")
    op.drop_column("group_ad_profile", "ad_policy_confidence")
    op.drop_column("group_ad_profile", "ad_policy_mode")
