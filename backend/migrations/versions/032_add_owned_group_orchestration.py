"""Add independent self-owned group orchestration tables.

Revision ID: 032_owned_group_orchestration
Revises: 031_join_ad_cooldowns
"""

import sqlalchemy as sa
from alembic import op

revision = "032_owned_group_orchestration"
down_revision = "031_join_ad_cooldowns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "owned_group_assets",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("internal_name", sa.String(length=120), nullable=False),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=True, unique=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("about", sa.Text(), nullable=True),
        sa.Column("visibility", sa.String(length=16), nullable=False, server_default="public"),
        sa.Column("telegram_username", sa.String(length=64), nullable=True),
        sa.Column("public_link", sa.String(length=255), nullable=True),
        sa.Column(
            "owner_account_id",
            sa.Integer(),
            sa.ForeignKey("telegram_account.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "invite_mode", sa.String(length=32), nullable=False, server_default="direct_invite"
        ),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="draft"),
        sa.Column("member_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
    )
    op.create_index("idx_owned_group_assets_status", "owned_group_assets", ["status"])
    op.create_index("idx_owned_group_assets_owner", "owned_group_assets", ["owner_account_id"])
    op.create_index("idx_owned_group_assets_visibility", "owned_group_assets", ["visibility"])

    op.create_table(
        "owned_group_operations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "group_asset_id",
            sa.Integer(),
            sa.ForeignKey("owned_group_assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("operation_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="draft"),
        sa.Column("selection_snapshot", sa.Text(), nullable=False),
        sa.Column("selection_snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("config_snapshot", sa.Text(), nullable=False),
        sa.Column("config_snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False, unique=True),
        sa.Column("planned_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stop_reason", sa.Text(), nullable=True),
        sa.Column("schedule_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
    )
    op.create_index(
        "idx_owned_group_operations_group_status",
        "owned_group_operations",
        ["group_asset_id", "status"],
    )
    op.create_index(
        "idx_owned_group_operations_schedule", "owned_group_operations", ["status", "schedule_at"]
    )

    op.create_table(
        "owned_group_operation_items",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "operation_id",
            sa.Integer(),
            sa.ForeignKey("owned_group_operations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("resource_type", sa.String(length=16), nullable=False),
        sa.Column("resource_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(), nullable=True),
        sa.Column("invited_at", sa.DateTime(), nullable=True),
        sa.Column("joined_at", sa.DateTime(), nullable=True),
        sa.Column("verified_at", sa.DateTime(), nullable=True),
        sa.Column("admin_required", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("admin_permissions", sa.Text(), nullable=True),
        sa.Column("admin_title", sa.String(length=64), nullable=True),
        sa.Column("lease_id", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.UniqueConstraint(
            "operation_id", "resource_type", "resource_id", name="uq_owned_group_operation_resource"
        ),
    )
    op.create_index(
        "idx_owned_group_items_status_retry",
        "owned_group_operation_items",
        ["status", "next_retry_at"],
    )
    op.create_index(
        "idx_owned_group_items_resource",
        "owned_group_operation_items",
        ["resource_type", "resource_id"],
    )

    op.create_table(
        "owned_group_memberships",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "group_asset_id",
            sa.Integer(),
            sa.ForeignKey("owned_group_assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("resource_type", sa.String(length=16), nullable=False),
        sa.Column("resource_id", sa.Integer(), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="member_verified"),
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("admin_title", sa.String(length=64), nullable=True),
        sa.Column("permissions_snapshot", sa.Text(), nullable=True),
        sa.Column("joined_at", sa.DateTime(), nullable=True),
        sa.Column("last_verified_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.UniqueConstraint(
            "group_asset_id",
            "resource_type",
            "resource_id",
            name="uq_owned_group_membership_resource",
        ),
    )
    op.create_index(
        "idx_owned_group_memberships_resource",
        "owned_group_memberships",
        ["resource_type", "resource_id"],
    )
    op.create_index("idx_owned_group_memberships_status", "owned_group_memberships", ["status"])

    op.create_table(
        "owned_group_admin_assignments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "group_asset_id",
            sa.Integer(),
            sa.ForeignKey("owned_group_assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("resource_type", sa.String(length=16), nullable=False),
        sa.Column("resource_id", sa.Integer(), nullable=False),
        sa.Column("permissions_snapshot", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("admin_title", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("verified_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.UniqueConstraint(
            "group_asset_id", "resource_type", "resource_id", name="uq_owned_group_admin_resource"
        ),
    )
    op.create_index("idx_owned_group_admin_status", "owned_group_admin_assignments", ["status"])

    op.create_table(
        "owned_group_invite_links",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "group_asset_id",
            sa.Integer(),
            sa.ForeignKey("owned_group_assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("link_type", sa.String(length=32), nullable=False),
        sa.Column("link_ciphertext", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "idx_owned_group_invite_links_active",
        "owned_group_invite_links",
        ["group_asset_id", "is_active"],
    )
    op.create_index(
        "uq_owned_group_invite_links_one_active",
        "owned_group_invite_links",
        ["group_asset_id"],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
    )

    op.create_table(
        "owned_bot_profiles",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "owner_account_id",
            sa.Integer(),
            sa.ForeignKey("telegram_account.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("telegram_account.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("bot_user_id", sa.BigInteger(), nullable=True),
        sa.Column("bot_username", sa.String(length=120), nullable=True),
        sa.Column("display_name", sa.String(length=120), nullable=True),
        sa.Column("token_ciphertext", sa.Text(), nullable=False),
        sa.Column(
            "status", sa.String(length=32), nullable=False, server_default="pending_verification"
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_verified_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
    )
    op.create_index("idx_owned_bot_profiles_owner", "owned_bot_profiles", ["owner_account_id"])
    op.create_index("idx_owned_bot_profiles_status", "owned_bot_profiles", ["status"])

    op.create_table(
        "owned_group_audit_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column(
            "group_asset_id",
            sa.Integer(),
            sa.ForeignKey("owned_group_assets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "operation_id",
            sa.Integer(),
            sa.ForeignKey("owned_group_operations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "operation_item_id",
            sa.Integer(),
            sa.ForeignKey("owned_group_operation_items.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("resource_type", sa.String(length=16), nullable=True),
        sa.Column("resource_id", sa.Integer(), nullable=True),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("before_state", sa.Text(), nullable=True),
        sa.Column("after_state", sa.Text(), nullable=True),
        sa.Column("result", sa.String(length=32), nullable=False, server_default="success"),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column("correlation_id", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
    )
    op.create_index(
        "idx_owned_group_audit_group_time",
        "owned_group_audit_events",
        ["group_asset_id", "created_at"],
    )
    op.create_index(
        "idx_owned_group_audit_operation_time",
        "owned_group_audit_events",
        ["operation_id", "created_at"],
    )
    op.create_index(
        "idx_owned_group_audit_resource_time",
        "owned_group_audit_events",
        ["resource_type", "resource_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("owned_group_audit_events")
    op.drop_table("owned_bot_profiles")
    op.drop_table("owned_group_invite_links")
    op.drop_table("owned_group_admin_assignments")
    op.drop_table("owned_group_memberships")
    op.drop_table("owned_group_operation_items")
    op.drop_table("owned_group_operations")
    op.drop_table("owned_group_assets")
