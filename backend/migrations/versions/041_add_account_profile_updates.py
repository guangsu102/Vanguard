"""Add durable serial ad-account profile update operations.

Revision ID: 041_account_profile_updates
Revises: 040_managed_bot_provisions
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "041_account_profile_updates"
down_revision = "040_managed_bot_provisions"
branch_labels = None
depends_on = None

_OPERATION_TABLE = "account_profile_update_operation"
_ITEM_TABLE = "account_profile_update_item"
_LEASE_TABLE = "account_profile_update_queue_lease"


def upgrade() -> None:
    op.create_table(
        _OPERATION_TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("account_ids_snapshot", sa.Text(), nullable=False),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("profile_bio", sa.String(length=70), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="queued", nullable=False),
        sa.Column("total_accounts", sa.Integer(), nullable=False),
        sa.Column("processed_accounts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("succeeded_accounts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_accounts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cancelled_accounts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("skipped_accounts", sa.Integer(), server_default="0", nullable=False),
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
            name="account_profile_update_total_accounts_range",
        ),
        sa.CheckConstraint(
            "max_attempts >= 1",
            name="account_profile_update_max_attempts_positive",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_table(
        _ITEM_TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("operation_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("baseline_bio_hash", sa.String(length=64), nullable=False),
        sa.Column("desired_bio_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("reason_code", sa.String(length=80), nullable=True),
        sa.Column("error_message", sa.String(length=255), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(), nullable=True),
        sa.Column("lease_id", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("remote_attempted_at", sa.DateTime(), nullable=True),
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
            name="account_profile_update_attempts_non_negative",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            [f"{_OPERATION_TABLE}.id"],
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
            name="uq_account_profile_update_item_operation_account",
        ),
    )
    op.create_table(
        _LEASE_TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("lease_id", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "id = 1",
            name="account_profile_update_queue_lease_singleton",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.bulk_insert(
        sa.table(_LEASE_TABLE, sa.column("id", sa.Integer())),
        [{"id": 1}],
    )
    op.create_index(
        "idx_account_profile_update_operation_status_created",
        _OPERATION_TABLE,
        ["status", "created_at"],
        unique=False,
    )
    op.create_index(
        "idx_account_profile_update_item_operation_status",
        _ITEM_TABLE,
        ["operation_id", "status"],
        unique=False,
    )
    op.create_index(
        "idx_account_profile_update_item_status_retry",
        _ITEM_TABLE,
        ["status", "next_retry_at"],
        unique=False,
    )
    op.create_index(
        "idx_account_profile_update_item_account_created",
        _ITEM_TABLE,
        ["account_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_account_profile_update_item_account_created", table_name=_ITEM_TABLE)
    op.drop_index("idx_account_profile_update_item_status_retry", table_name=_ITEM_TABLE)
    op.drop_index("idx_account_profile_update_item_operation_status", table_name=_ITEM_TABLE)
    op.drop_index("idx_account_profile_update_operation_status_created", table_name=_OPERATION_TABLE)
    op.drop_table(_LEASE_TABLE)
    op.drop_table(_ITEM_TABLE)
    op.drop_table(_OPERATION_TABLE)
