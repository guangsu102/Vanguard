"""Harden resource search task lifecycle.

Revision ID: 029_harden_resource_search
Revises: 028_add_resource_search
"""

import sqlalchemy as sa
from alembic import op

revision = "029_harden_resource_search"
down_revision = "028_add_resource_search"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "acquisition_resource_search_run",
        sa.Column("celery_task_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "acquisition_resource_search_run",
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "acquisition_resource_search_run",
        sa.Column("cancel_requested_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "acquisition_resource_search_account",
        sa.Column(
            "successful_keywords",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.create_index(
        "idx_resource_search_run_task",
        "acquisition_resource_search_run",
        ["celery_task_id"],
    )
    op.create_index(
        "idx_resource_search_run_heartbeat",
        "acquisition_resource_search_run",
        ["status", "heartbeat_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_resource_search_run_heartbeat",
        table_name="acquisition_resource_search_run",
    )
    op.drop_index(
        "idx_resource_search_run_task",
        table_name="acquisition_resource_search_run",
    )
    op.drop_column("acquisition_resource_search_account", "successful_keywords")
    op.drop_column("acquisition_resource_search_run", "cancel_requested_at")
    op.drop_column("acquisition_resource_search_run", "heartbeat_at")
    op.drop_column("acquisition_resource_search_run", "celery_task_id")
