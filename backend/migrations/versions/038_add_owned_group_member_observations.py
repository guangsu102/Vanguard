"""Add Guardian-observed owned-group member facts.

Revision ID: 038_member_observations
Revises: 037_message_persona_snapshot
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "038_member_observations"
down_revision = "037_message_persona_snapshot"
branch_labels = None
depends_on = None


_TABLE = "owned_group_member_observations"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("group_asset_id", sa.Integer(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("is_bot", sa.Boolean(), nullable=False),
        sa.Column("username_snapshot", sa.String(length=120), nullable=True),
        sa.Column("display_name_snapshot", sa.String(length=200), nullable=True),
        sa.Column(
            "presence_status",
            sa.String(length=16),
            server_default="present",
            nullable=False,
        ),
        sa.Column("last_event_type", sa.String(length=32), nullable=False),
        sa.Column("source_bot_account_id", sa.Integer(), nullable=True),
        sa.Column("last_update_id", sa.BigInteger(), nullable=False),
        sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("left_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "presence_status IN ('present', 'left')",
            name=op.f("ck_owned_group_member_observations_presence_status"),
        ),
        sa.CheckConstraint(
            "last_event_type IN "
            "('message', 'new_chat_member', 'left_chat_member')",
            name=op.f("ck_owned_group_member_observations_last_event_type"),
        ),
        sa.ForeignKeyConstraint(
            ["group_asset_id"],
            ["owned_group_assets.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_bot_account_id"],
            ["telegram_account.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "group_asset_id",
            "telegram_user_id",
            name="uq_owned_group_member_observations_asset_user",
        ),
    )
    op.create_index(
        "idx_owned_group_member_observations_presence",
        _TABLE,
        ["group_asset_id", "presence_status", "last_observed_at"],
        unique=False,
    )
    op.create_index(
        "idx_owned_group_member_observations_last_observed",
        _TABLE,
        ["group_asset_id", "last_observed_at"],
        unique=False,
    )
    op.create_index(
        "idx_owned_group_member_observations_telegram_user",
        _TABLE,
        ["telegram_user_id"],
        unique=False,
    )
    op.create_index(
        "idx_owned_group_member_observations_source_bot",
        _TABLE,
        ["source_bot_account_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_owned_group_member_observations_source_bot", table_name=_TABLE)
    op.drop_index("idx_owned_group_member_observations_telegram_user", table_name=_TABLE)
    op.drop_index("idx_owned_group_member_observations_last_observed", table_name=_TABLE)
    op.drop_index("idx_owned_group_member_observations_presence", table_name=_TABLE)
    op.drop_table(_TABLE)
