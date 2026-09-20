"""Set Growth advertising capacity defaults.

Revision ID: 042_set_growth_ad_capacity
Revises: 041_account_profile_updates
"""

import sqlalchemy as sa
from alembic import op

revision = "042_set_growth_ad_capacity"
down_revision = "041_account_profile_updates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "ad_campaign",
        "max_sends_per_account_per_day",
        server_default=sa.text("30"),
    )
    op.alter_column(
        "telegram_account_operation_config",
        "max_groups_total",
        server_default=sa.text("100"),
    )


def downgrade() -> None:
    op.alter_column(
        "telegram_account_operation_config",
        "max_groups_total",
        server_default=None,
    )
    op.alter_column(
        "ad_campaign",
        "max_sends_per_account_per_day",
        server_default=sa.text("10"),
    )
