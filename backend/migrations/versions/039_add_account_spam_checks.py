"""Add official SpamBot account checks and restricted account state.

Revision ID: 039_account_spam_checks
Revises: 038_member_observations
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "039_account_spam_checks"
down_revision = "038_member_observations"
branch_labels = None
depends_on = None

_OPERATION_TABLE = "account_spam_check_operation"
_ITEM_TABLE = "account_spam_check_item"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # AccountStatus uses SQLAlchemy's native enum and therefore stores enum
        # member names. PostgreSQL requires ADD VALUE outside the surrounding
        # migration transaction on versions where a new label cannot be used
        # until commit. This migration does not write RESTRICTED rows itself.
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE accountstatus ADD VALUE IF NOT EXISTS 'RESTRICTED'")

    op.add_column(
        "telegram_account",
        sa.Column(
            "spam_check_status",
            sa.String(length=20),
            server_default="unknown",
            nullable=False,
        ),
    )
    op.add_column(
        "telegram_account",
        sa.Column("spam_checked_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "telegram_account",
        sa.Column("spam_check_summary", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "telegram_account",
        sa.Column("spam_restriction_confirmed_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "telegram_account",
        sa.Column("restriction_previous_status", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "telegram_account",
        sa.Column("restriction_source", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "telegram_account",
        sa.Column("restriction_reason", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "telegram_account",
        sa.Column("restriction_detected_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "idx_account_spam_check_status",
        "telegram_account",
        ["spam_check_status"],
        unique=False,
    )
    op.create_table(
        _OPERATION_TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("account_ids_snapshot", sa.Text(), nullable=False),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("trigger", sa.String(length=20), server_default="manual", nullable=False),
        sa.Column("status", sa.String(length=24), server_default="queued", nullable=False),
        sa.Column("total_accounts", sa.Integer(), nullable=False),
        sa.Column("processed_accounts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("clear_accounts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("restricted_accounts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_accounts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cancelled_accounts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
        sa.Column("last_error", sa.String(length=255), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "total_accounts >= 1 AND total_accounts <= 2000",
            name="account_spam_total_accounts_range",
        ),
        sa.CheckConstraint(
            "max_attempts >= 1",
            name="account_spam_max_attempts_positive",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(
        "idx_account_spam_operation_status_created",
        _OPERATION_TABLE,
        ["status", "created_at"],
        unique=False,
    )

    op.create_table(
        _ITEM_TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("operation_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=24), server_default="pending", nullable=False),
        sa.Column("result", sa.String(length=20), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("reason_code", sa.String(length=80), nullable=True),
        sa.Column("response_summary", sa.String(length=255), nullable=True),
        sa.Column("account_status_before", sa.String(length=20), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(), nullable=True),
        sa.Column("lease_id", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("checked_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name="account_spam_attempts_non_negative",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            [_OPERATION_TABLE + ".id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["telegram_account.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "operation_id",
            "account_id",
            name="uq_account_spam_item_operation_account",
        ),
    )
    op.create_index(
        "idx_account_spam_item_operation_status",
        _ITEM_TABLE,
        ["operation_id", "status"],
        unique=False,
    )
    op.create_index(
        "idx_account_spam_item_status_retry",
        _ITEM_TABLE,
        ["status", "next_retry_at"],
        unique=False,
    )
    op.create_index(
        "idx_account_spam_item_account_created",
        _ITEM_TABLE,
        ["account_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_account_spam_item_account_created", table_name=_ITEM_TABLE)
    op.drop_index("idx_account_spam_item_status_retry", table_name=_ITEM_TABLE)
    op.drop_index("idx_account_spam_item_operation_status", table_name=_ITEM_TABLE)
    op.drop_table(_ITEM_TABLE)
    op.drop_index("idx_account_spam_operation_status_created", table_name=_OPERATION_TABLE)
    op.drop_table(_OPERATION_TABLE)
    op.drop_index("idx_account_spam_check_status", table_name="telegram_account")
    op.drop_column("telegram_account", "restriction_detected_at")
    op.drop_column("telegram_account", "restriction_reason")
    op.drop_column("telegram_account", "restriction_source")
    op.drop_column("telegram_account", "restriction_previous_status")
    op.drop_column("telegram_account", "spam_restriction_confirmed_at")
    op.drop_column("telegram_account", "spam_check_summary")
    op.drop_column("telegram_account", "spam_checked_at")
    op.drop_column("telegram_account", "spam_check_status")
    # PostgreSQL enum labels cannot be safely removed while existing databases
    # may still contain historical values. RESTRICTED is intentionally retained.
