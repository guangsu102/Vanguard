"""add explicit unknown group advertisement probe state

Revision ID: 018_add_ad_policy_probe
Revises: 017_add_account_operation_mode
Create Date: 2026-08-18
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "018_add_ad_policy_probe"
down_revision = "017_add_account_operation_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "group_ad_profile",
        sa.Column("ad_policy_probe_status", sa.String(length=30), nullable=False, server_default="not_started"),
    )
    op.add_column("group_ad_profile", sa.Column("ad_policy_probe_at", sa.DateTime(), nullable=True))
    op.add_column("group_ad_profile", sa.Column("ad_policy_probe_account_id", sa.Integer(), nullable=True))
    op.add_column("group_ad_profile", sa.Column("ad_policy_probe_error", sa.Text(), nullable=True))
    op.create_index(
        "idx_group_ad_profile_probe",
        "group_ad_profile",
        ["ad_policy_probe_status", "ad_policy_probe_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_group_ad_profile_probe", table_name="group_ad_profile")
    op.drop_column("group_ad_profile", "ad_policy_probe_error")
    op.drop_column("group_ad_profile", "ad_policy_probe_account_id")
    op.drop_column("group_ad_profile", "ad_policy_probe_at")
    op.drop_column("group_ad_profile", "ad_policy_probe_status")