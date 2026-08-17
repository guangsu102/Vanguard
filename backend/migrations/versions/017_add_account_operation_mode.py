"""add account operation mode

Revision ID: 017_add_account_operation_mode
Revises: 016_group_failover_tasks
Create Date: 2026-08-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "017_add_account_operation_mode"
down_revision = "016_group_failover_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "telegram_account_operation_config",
        sa.Column(
            "operation_mode",
            sa.String(length=20),
            nullable=False,
            server_default="growth",
        ),
    )
    op.create_index(
        "idx_account_operation_mode",
        "telegram_account_operation_config",
        ["operation_mode"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_account_operation_mode",
        table_name="telegram_account_operation_config",
    )
    op.drop_column("telegram_account_operation_config", "operation_mode")
