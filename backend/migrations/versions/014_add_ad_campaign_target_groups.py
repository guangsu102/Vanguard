"""add explicit target groups to advertisement campaigns

Revision ID: 014_ad_campaign_target_groups
Revises: 013_ad_safety_hardening
Create Date: 2026-07-14
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "014_ad_campaign_target_groups"
down_revision = "013_ad_safety_hardening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ad_campaign", sa.Column("target_group_ids", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("ad_campaign", "target_group_ids")
