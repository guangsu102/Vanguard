"""Count real join requests and enforce Growth campaign daily quotas.

Revision ID: 030_fix_growth_daily_quotas
Revises: 029_harden_resource_search
"""

import sqlalchemy as sa
from alembic import op

revision = "030_fix_growth_daily_quotas"
down_revision = "029_harden_resource_search"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "acquisition_auto_join_attempt",
        sa.Column(
            "telegram_action_attempted",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.execute(
        """
        UPDATE acquisition_auto_join_attempt
        SET telegram_action_attempted = TRUE
        WHERE status = 'success'
           OR joined_at IS NOT NULL
           OR (status = 'pending' AND reason = 'join_request_pending')
        """
    )
    op.create_index(
        "idx_auto_join_account_action_attempted",
        "acquisition_auto_join_attempt",
        ["account_id", "telegram_action_attempted", "attempted_at"],
    )
    op.alter_column(
        "ad_campaign",
        "max_sends_per_account_per_day",
        server_default=sa.text("10"),
    )


def downgrade() -> None:
    op.alter_column(
        "ad_campaign",
        "max_sends_per_account_per_day",
        server_default=sa.text("3"),
    )
    op.drop_index(
        "idx_auto_join_account_action_attempted",
        table_name="acquisition_auto_join_attempt",
    )
    op.drop_column("acquisition_auto_join_attempt", "telegram_action_attempted")
