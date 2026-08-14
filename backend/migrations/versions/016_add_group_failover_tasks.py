"""add group failover recovery tasks

Revision ID: 016_group_failover_tasks
Revises: 015_add_qq_official
Create Date: 2026-08-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "016_group_failover_tasks"
down_revision = "015_add_qq_official"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "acquisition_group_failover_task",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_membership_id", sa.Integer(), nullable=False),
        sa.Column("source_account_id", sa.Integer(), nullable=False),
        sa.Column("target_account_id", sa.Integer(), nullable=True),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("telegram_group_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="queued"),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.DateTime(), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(
            ["source_membership_id"],
            ["group_account_membership.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["source_account_id"], ["telegram_account.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["target_account_id"], ["telegram_account.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["group_id"], ["group.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_membership_id", name="uq_group_failover_source_membership"),
    )
    op.create_index(
        "idx_group_failover_status_retry",
        "acquisition_group_failover_task",
        ["status", "next_retry_at"],
    )
    op.create_index(
        "idx_group_failover_source_account",
        "acquisition_group_failover_task",
        ["source_account_id", "status"],
    )
    op.create_index(
        "idx_group_failover_target_account",
        "acquisition_group_failover_task",
        ["target_account_id", "status"],
    )
    op.create_index("idx_group_failover_group", "acquisition_group_failover_task", ["group_id"])


def downgrade() -> None:
    op.drop_table("acquisition_group_failover_task")
