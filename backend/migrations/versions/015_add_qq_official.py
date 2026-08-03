"""add QQ official group governance tables

Revision ID: 015_add_qq_official
Revises: 014_ad_campaign_target_groups
Create Date: 2026-07-15
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "015_add_qq_official"
down_revision = "014_ad_campaign_target_groups"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "qq_bot_connection",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("app_id", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="offline"),
        sa.Column("bot_openid", sa.String(length=128), nullable=True),
        sa.Column("session_id", sa.String(length=255), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("last_connected_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("app_id", name="uq_qq_bot_connection_app_id"),
    )
    op.create_table(
        "qq_managed_group",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("connection_id", sa.Integer(), nullable=False),
        sa.Column("group_openid", sa.String(length=128), nullable=False),
        sa.Column("local_name", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="active"),
        sa.Column("monitoring_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("notifications_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("auto_recall_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("receive_all_messages_enabled", sa.Boolean(), nullable=True),
        sa.Column("proactive_messages_enabled", sa.Boolean(), nullable=True),
        sa.Column("last_message_at", sa.DateTime(), nullable=True),
        sa.Column("bot_added_at", sa.DateTime(), nullable=True),
        sa.Column("bot_removed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["connection_id"], ["qq_bot_connection.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("connection_id", "group_openid", name="uq_qq_group_connection_openid"),
    )
    op.create_index("idx_qq_managed_group_status", "qq_managed_group", ["status"])
    op.create_index("idx_qq_managed_group_last_message", "qq_managed_group", ["last_message_at"])
    op.create_table(
        "qq_group_message",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("provider_message_id", sa.String(length=255), nullable=False),
        sa.Column("member_openid", sa.String(length=128), nullable=True),
        sa.Column("member_role", sa.String(length=30), nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("attachments_json", sa.Text(), nullable=True),
        sa.Column("is_at_bot", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("moderation_status", sa.String(length=30), nullable=False, server_default="unreviewed"),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("recalled_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["group_id"], ["qq_managed_group.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_id", "provider_message_id", name="uq_qq_message_group_provider"),
    )
    op.create_index("idx_qq_group_message_time", "qq_group_message", ["group_id", "occurred_at"])
    op.create_index("idx_qq_group_message_member", "qq_group_message", ["member_openid"])
    op.create_table(
        "qq_group_event",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("member_openid", sa.String(length=128), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["group_id"], ["qq_managed_group.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", name="uq_qq_group_event_event_id"),
    )
    op.create_index("idx_qq_group_event_group_time", "qq_group_event", ["group_id", "occurred_at"])
    op.create_index("idx_qq_group_event_type", "qq_group_event", ["event_type"])
    op.create_table(
        "qq_group_command",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("command_type", sa.String(length=40), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="pending"),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["group_id"], ["qq_managed_group.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_qq_group_command_status", "qq_group_command", ["status", "created_at"])
    op.create_index("idx_qq_group_command_group", "qq_group_command", ["group_id", "created_at"])


def downgrade() -> None:
    op.drop_table("qq_group_command")
    op.drop_table("qq_group_event")
    op.drop_table("qq_group_message")
    op.drop_table("qq_managed_group")
    op.drop_table("qq_bot_connection")
