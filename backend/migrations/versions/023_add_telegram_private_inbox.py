"""Add Telegram private chat inbox tables.

Revision ID: 023_add_telegram_private_inbox
Revises: 022_add_ad_delivery_policy_scheduler
"""

from alembic import op
import sqlalchemy as sa


revision = "023_add_telegram_private_inbox"
down_revision = "022_add_ad_delivery_policy_scheduler"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "telegram_private_conversation",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("peer_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("peer_username", sa.String(length=100), nullable=True),
        sa.Column("peer_display_name", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="open"),
        sa.Column("handling_mode", sa.String(length=20), nullable=False, server_default="auto"),
        sa.Column("assigned_admin_id", sa.Integer(), nullable=True),
        sa.Column("unread_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_message_preview", sa.String(length=255), nullable=True),
        sa.Column("last_message_direction", sa.String(length=20), nullable=True),
        sa.Column("last_message_at", sa.DateTime(), nullable=True),
        sa.Column("last_inbound_at", sa.DateTime(), nullable=True),
        sa.Column("last_outbound_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"], ["telegram_account.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id",
            "peer_telegram_id",
            name="uq_private_conversation_account_peer",
        ),
    )
    op.create_index(
        "idx_private_conversation_last_message",
        "telegram_private_conversation",
        ["status", "last_message_at"],
    )
    op.create_index(
        "idx_private_conversation_account_last",
        "telegram_private_conversation",
        ["account_id", "last_message_at"],
    )
    op.create_index(
        "idx_private_conversation_unread",
        "telegram_private_conversation",
        ["unread_count", "last_message_at"],
    )

    op.create_table(
        "telegram_private_message",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("peer_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("reply_to_telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("direction", sa.String(length=20), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("message_type", sa.String(length=30), nullable=False, server_default="text"),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("media_json", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("operator_id", sa.Integer(), nullable=True),
        sa.Column("client_request_id", sa.String(length=64), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"], ["telegram_account.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["telegram_private_conversation.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id",
            "peer_telegram_id",
            "telegram_message_id",
            name="uq_private_message_account_peer_telegram_id",
        ),
        sa.UniqueConstraint(
            "client_request_id", name="uq_private_message_client_request"
        ),
    )
    op.create_index(
        "idx_private_message_conversation_time",
        "telegram_private_message",
        ["conversation_id", "occurred_at"],
    )
    op.create_index(
        "idx_private_message_outbox",
        "telegram_private_message",
        ["direction", "status", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_private_message_outbox", table_name="telegram_private_message")
    op.drop_index(
        "idx_private_message_conversation_time",
        table_name="telegram_private_message",
    )
    op.drop_table("telegram_private_message")
    op.drop_index(
        "idx_private_conversation_unread",
        table_name="telegram_private_conversation",
    )
    op.drop_index(
        "idx_private_conversation_account_last",
        table_name="telegram_private_conversation",
    )
    op.drop_index(
        "idx_private_conversation_last_message",
        table_name="telegram_private_conversation",
    )
    op.drop_table("telegram_private_conversation")
