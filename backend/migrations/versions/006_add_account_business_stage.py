"""add account business stage

Revision ID: 006_add_account_business_stage
Revises: 005_add_account_risk_controls
Create Date: 2026-07-03
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "006_add_account_business_stage"
down_revision = "005_add_account_risk_controls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "telegram_account_operation_config",
        sa.Column("business_stage", sa.String(length=20), nullable=False, server_default="new"),
    )
    op.create_index(
        "idx_account_operation_business_stage",
        "telegram_account_operation_config",
        ["business_stage"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_account_operation_business_stage", table_name="telegram_account_operation_config")
    op.drop_column("telegram_account_operation_config", "business_stage")
