"""add group ad policy evidence hash

Revision ID: 020_add_ad_policy_evidence_hash
Revises: 019_add_group_ad_policy_event_account
Create Date: 2026-08-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "020_add_ad_policy_evidence_hash"
down_revision = "019_add_group_ad_policy_event_account"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "group_ad_profile",
        sa.Column("ad_policy_evidence_hash", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("group_ad_profile", "ad_policy_evidence_hash")
