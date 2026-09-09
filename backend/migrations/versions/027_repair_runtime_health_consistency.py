"""Repair runtime health and membership consistency.

Revision ID: 027_repair_runtime_health_consistency
Revises: 026_add_ad_only_join_queue
"""

from alembic import op

revision = "027_repair_runtime_health_consistency"
down_revision = "026_add_ad_only_join_queue"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE group_account_membership
        SET ad_status = 'blocked',
            updated_at = NOW()
        WHERE status IN ('left', 'banned', 'rejected')
          AND ad_status IS DISTINCT FROM 'blocked'
        """
    )
    op.create_index(
        "idx_acquisition_conversation_context_expires",
        "acquisition_conversation_context",
        ["expires_at"],
    )
    op.create_check_constraint(
        "ck_group_membership_inactive_ad_blocked",
        "group_account_membership",
        "status NOT IN ('left', 'banned', 'rejected') OR ad_status = 'blocked'",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_group_membership_inactive_ad_blocked",
        "group_account_membership",
        type_="check",
    )
    op.drop_index(
        "idx_acquisition_conversation_context_expires",
        table_name="acquisition_conversation_context",
    )
