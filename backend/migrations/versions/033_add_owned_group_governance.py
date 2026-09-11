"""Add Guardian governance links to self-owned group assets.

Revision ID: 033_owned_group_governance
Revises: 032_owned_group_orchestration
"""

import sqlalchemy as sa
from alembic import op

revision = "033_owned_group_governance"
down_revision = "032_owned_group_orchestration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "owned_group_assets",
        sa.Column("core_group_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "owned_group_assets",
        sa.Column("managed_binding_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "owned_group_assets",
        sa.Column("guardian_bot_account_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "owned_group_assets",
        sa.Column(
            "governance_status",
            sa.String(length=32),
            nullable=False,
            server_default="disabled",
        ),
    )
    op.add_column(
        "owned_group_assets",
        sa.Column("governance_pending_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "owned_group_assets",
        sa.Column("governance_enabled_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "owned_group_assets",
        sa.Column("governance_last_checked_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "owned_group_assets",
        sa.Column("governance_last_error_code", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "owned_group_assets",
        sa.Column("governance_last_error_message", sa.Text(), nullable=True),
    )

    op.create_foreign_key(
        "fk_owned_group_assets_core_group",
        "owned_group_assets",
        "group",
        ["core_group_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_owned_group_assets_managed_binding",
        "owned_group_assets",
        "managed_group_binding",
        ["managed_binding_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_owned_group_assets_guardian_bot_account",
        "owned_group_assets",
        "telegram_account",
        ["guardian_bot_account_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        op.f("ck_owned_group_assets_governance_status"),
        "owned_group_assets",
        "governance_status IN ('disabled', 'pending', 'managed', 'degraded')",
    )
    op.create_index(
        "idx_owned_group_assets_core_group",
        "owned_group_assets",
        ["core_group_id"],
    )
    op.create_index(
        "uq_owned_group_assets_managed_binding",
        "owned_group_assets",
        ["managed_binding_id"],
        unique=True,
    )
    op.create_index(
        "idx_owned_group_assets_guardian_status",
        "owned_group_assets",
        ["guardian_bot_account_id", "governance_status"],
    )

    # The server default already backfills PostgreSQL rows added by this
    # migration. Keep this explicit update for databases restored from older
    # schemas where a nullable column may have been staged manually.
    op.execute(
        "UPDATE owned_group_assets "
        "SET governance_status = 'disabled' "
        "WHERE governance_status IS NULL"
    )


def downgrade() -> None:
    op.drop_index("idx_owned_group_assets_guardian_status", table_name="owned_group_assets")
    op.drop_index("uq_owned_group_assets_managed_binding", table_name="owned_group_assets")
    op.drop_index("idx_owned_group_assets_core_group", table_name="owned_group_assets")
    op.drop_constraint(
        op.f("ck_owned_group_assets_governance_status"),
        "owned_group_assets",
        type_="check",
    )
    op.drop_constraint(
        "fk_owned_group_assets_guardian_bot_account",
        "owned_group_assets",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_owned_group_assets_managed_binding",
        "owned_group_assets",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_owned_group_assets_core_group",
        "owned_group_assets",
        type_="foreignkey",
    )
    op.drop_column("owned_group_assets", "governance_last_error_message")
    op.drop_column("owned_group_assets", "governance_last_error_code")
    op.drop_column("owned_group_assets", "governance_last_checked_at")
    op.drop_column("owned_group_assets", "governance_enabled_at")
    op.drop_column("owned_group_assets", "governance_pending_at")
    op.drop_column("owned_group_assets", "governance_status")
    op.drop_column("owned_group_assets", "guardian_bot_account_id")
    op.drop_column("owned_group_assets", "managed_binding_id")
    op.drop_column("owned_group_assets", "core_group_id")
