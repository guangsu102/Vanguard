"""Add durable managed Bot provisioning operations.

Revision ID: 040_managed_bot_provisions
Revises: 039_account_spam_checks
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "040_managed_bot_provisions"
down_revision = "039_account_spam_checks"
branch_labels = None
depends_on = None

_TABLE = "managed_bot_provision"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("owner_account_id", sa.Integer(), nullable=False),
        sa.Column("manager_bot_profile_id", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.String(length=64), nullable=False),
        sa.Column("username", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="queued", nullable=False),
        sa.Column("current_step", sa.String(length=32), server_default="preflight", nullable=False),
        sa.Column("bot_user_id", sa.BigInteger(), nullable=True),
        sa.Column("guardian_bot_profile_id", sa.Integer(), nullable=True),
        sa.Column("owned_bot_profile_id", sa.Integer(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
        sa.Column("retryable", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("error_message", sa.String(length=255), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(), nullable=True),
        sa.Column("celery_task_id", sa.String(length=64), nullable=True),
        sa.Column("lease_id", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("create_attempted_at", sa.DateTime(), nullable=True),
        sa.Column("external_created_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
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
            name="managed_bot_provision_attempts_non_negative",
        ),
        sa.CheckConstraint(
            "max_attempts >= 1",
            name="managed_bot_provision_max_attempts_positive",
        ),
        sa.ForeignKeyConstraint(
            ["owner_account_id"],
            ["telegram_account.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["manager_bot_profile_id"],
            ["guardian_bot_profile.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["guardian_bot_profile_id"],
            ["guardian_bot_profile.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["owned_bot_profile_id"],
            ["owned_bot_profiles.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("bot_user_id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(
        "uq_managed_bot_provision_reserved_username",
        _TABLE,
        ["username"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('queued','running','retry_wait','needs_attention','succeeded') "
            "OR bot_user_id IS NOT NULL OR external_created_at IS NOT NULL"
        ),
    )
    op.create_index(
        "idx_managed_bot_provision_status_retry",
        _TABLE,
        ["status", "next_retry_at"],
        unique=False,
    )
    op.create_index(
        "idx_managed_bot_provision_owner",
        _TABLE,
        ["owner_account_id"],
        unique=False,
    )
    op.create_index(
        "idx_managed_bot_provision_manager",
        _TABLE,
        ["manager_bot_profile_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_managed_bot_provision_manager", table_name=_TABLE)
    op.drop_index(
        "uq_managed_bot_provision_reserved_username",
        table_name=_TABLE,
    )
    op.drop_index("idx_managed_bot_provision_owner", table_name=_TABLE)
    op.drop_index("idx_managed_bot_provision_status_retry", table_name=_TABLE)
    op.drop_table(_TABLE)
