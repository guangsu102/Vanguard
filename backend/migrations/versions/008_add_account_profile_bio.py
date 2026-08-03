"""add account profile bio

Revision ID: 008_add_account_profile_bio
Revises: 007_add_ad_capacity_survival
Create Date: 2026-07-05
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "008_add_account_profile_bio"
down_revision = "007_add_ad_capacity_survival"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("telegram_account", sa.Column("profile_bio", sa.String(length=255), nullable=True))
    op.add_column("telegram_account", sa.Column("profile_bio_synced_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("telegram_account", "profile_bio_synced_at")
    op.drop_column("telegram_account", "profile_bio")
