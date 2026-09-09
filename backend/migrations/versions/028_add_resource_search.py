"""Add read-only multi-account resource search.

Revision ID: 028_add_resource_search
Revises: 027_repair_runtime_health_consistency
"""

import sqlalchemy as sa
from alembic import op

revision = "028_add_resource_search"
down_revision = "027_repair_runtime_health_consistency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "acquisition_resource_search_run",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("keywords_json", sa.Text(), nullable=False),
        sa.Column("account_ids_json", sa.Text(), nullable=False),
        sa.Column("max_results_per_keyword", sa.Integer(), server_default="20", nullable=False),
        sa.Column("status", sa.String(length=20), server_default="queued", nullable=False),
        sa.Column("total_accounts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("completed_accounts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("successful_accounts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_accounts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("raw_result_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("unique_result_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_resource_search_run_status_created",
        "acquisition_resource_search_run",
        ["status", "created_at"],
    )

    op.create_table(
        "acquisition_resource_search_account",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("account_identifier", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="queued", nullable=False),
        sa.Column("keywords_completed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("result_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("flood_wait_seconds", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["acquisition_resource_search_run.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["account_id"], ["telegram_account.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "account_id", name="uq_resource_search_run_account"),
    )
    op.create_index(
        "idx_resource_search_account_run_status",
        "acquisition_resource_search_account",
        ["run_id", "status"],
    )

    op.create_table(
        "acquisition_resource_search_result",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("dedupe_key", sa.String(length=500), nullable=False),
        sa.Column("telegram_group_id", sa.BigInteger(), nullable=True),
        sa.Column("title", sa.String(length=500), server_default="", nullable=False),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("invite_link", sa.String(length=500), nullable=True),
        sa.Column("member_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("is_private", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("matched_keywords_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column(
            "discovered_by_account_ids_json", sa.Text(), server_default="[]", nullable=False
        ),
        sa.Column("discovery_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("review_status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("first_found_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("last_found_at", sa.DateTime(), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("reviewed_by_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id"], ["acquisition_resource_search_run.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "dedupe_key", name="uq_resource_search_run_dedupe"),
    )
    op.create_index(
        "idx_resource_search_result_run_review",
        "acquisition_resource_search_result",
        ["run_id", "review_status"],
    )
    op.create_index(
        "idx_resource_search_result_group",
        "acquisition_resource_search_result",
        ["telegram_group_id"],
    )
    op.create_index(
        "idx_resource_search_result_members",
        "acquisition_resource_search_result",
        ["member_count"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_resource_search_result_members",
        table_name="acquisition_resource_search_result",
    )
    op.drop_index(
        "idx_resource_search_result_group",
        table_name="acquisition_resource_search_result",
    )
    op.drop_index(
        "idx_resource_search_result_run_review",
        table_name="acquisition_resource_search_result",
    )
    op.drop_table("acquisition_resource_search_result")
    op.drop_index(
        "idx_resource_search_account_run_status",
        table_name="acquisition_resource_search_account",
    )
    op.drop_table("acquisition_resource_search_account")
    op.drop_index(
        "idx_resource_search_run_status_created",
        table_name="acquisition_resource_search_run",
    )
    op.drop_table("acquisition_resource_search_run")
