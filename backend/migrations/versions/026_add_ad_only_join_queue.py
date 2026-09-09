"""Add paced ad-only group join queue fields.

Revision ID: 026_add_ad_only_join_queue
Revises: 025_add_direct_ad_only_assignments
"""

import sqlalchemy as sa
from alembic import op

revision = "026_add_ad_only_join_queue"
down_revision = "025_add_direct_ad_only_assignments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("group_ad_handover", sa.Column("batch_id", sa.String(length=64), nullable=True))
    op.add_column(
        "group_ad_handover",
        sa.Column("queue_position", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "group_ad_handover",
        sa.Column("join_interval_min_minutes", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "group_ad_handover",
        sa.Column("join_interval_max_minutes", sa.Integer(), server_default="30", nullable=False),
    )
    op.add_column("group_ad_handover", sa.Column("next_attempt_at", sa.DateTime(), nullable=True))
    op.add_column("group_ad_handover", sa.Column("campaign_previous_json", sa.Text(), nullable=True))
    op.create_index(
        "idx_group_ad_handover_join_queue",
        "group_ad_handover",
        ["workflow_type", "status", "next_attempt_at"],
    )
    op.create_index(
        "idx_group_ad_handover_account_queue",
        "group_ad_handover",
        ["target_ad_only_account_id", "status", "next_attempt_at"],
    )
    op.create_index(
        "idx_group_ad_handover_batch",
        "group_ad_handover",
        ["batch_id", "queue_position"],
    )


def downgrade() -> None:
    op.drop_index("idx_group_ad_handover_batch", table_name="group_ad_handover")
    op.drop_index("idx_group_ad_handover_account_queue", table_name="group_ad_handover")
    op.drop_index("idx_group_ad_handover_join_queue", table_name="group_ad_handover")
    op.drop_column("group_ad_handover", "campaign_previous_json")
    op.drop_column("group_ad_handover", "next_attempt_at")
    op.drop_column("group_ad_handover", "join_interval_max_minutes")
    op.drop_column("group_ad_handover", "join_interval_min_minutes")
    op.drop_column("group_ad_handover", "queue_position")
    op.drop_column("group_ad_handover", "batch_id")
