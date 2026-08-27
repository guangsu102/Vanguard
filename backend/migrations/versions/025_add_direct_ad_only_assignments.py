"""Extend ad-only handovers with direct assignments.

Revision ID: 025_add_direct_ad_only_assignments
Revises: 024_add_ad_only_recommendations
"""

import sqlalchemy as sa
from alembic import op

revision = "025_add_direct_ad_only_assignments"
down_revision = "024_add_ad_only_recommendations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "group_ad_handover",
        sa.Column(
            "workflow_type",
            sa.String(length=20),
            server_default="assessment",
            nullable=False,
        ),
    )
    op.alter_column("group_ad_handover", "assessment_id", nullable=True)
    op.alter_column("group_ad_handover", "group_id", nullable=True)
    op.alter_column("group_ad_handover", "source_growth_account_id", nullable=True)
    op.add_column(
        "group_ad_handover",
        sa.Column("permission_mode", sa.String(length=40), nullable=True),
    )
    op.add_column(
        "group_ad_handover",
        sa.Column("permission_note", sa.Text(), nullable=True),
    )
    op.add_column(
        "group_ad_handover",
        sa.Column("permission_expires_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "group_ad_handover",
        sa.Column("permission_previous_json", sa.Text(), nullable=True),
    )
    op.add_column(
        "group_ad_handover",
        sa.Column("membership_previous_json", sa.Text(), nullable=True),
    )
    op.alter_column("group_ad_only_event", "group_id", nullable=True)
    op.create_index(
        "idx_group_ad_handover_workflow",
        "group_ad_handover",
        ["workflow_type", "status", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_group_ad_handover_workflow", table_name="group_ad_handover"
    )
    op.alter_column("group_ad_only_event", "group_id", nullable=False)
    op.drop_column("group_ad_handover", "membership_previous_json")
    op.drop_column("group_ad_handover", "permission_previous_json")
    op.drop_column("group_ad_handover", "permission_expires_at")
    op.drop_column("group_ad_handover", "permission_note")
    op.drop_column("group_ad_handover", "permission_mode")
    op.alter_column("group_ad_handover", "source_growth_account_id", nullable=False)
    op.alter_column("group_ad_handover", "group_id", nullable=False)
    op.alter_column("group_ad_handover", "assessment_id", nullable=False)
    op.drop_column("group_ad_handover", "workflow_type")
