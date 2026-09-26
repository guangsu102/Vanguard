"""Persist fair join-request reconciliation scheduling."""

import sqlalchemy as sa
from alembic import op

revision = "044_join_reconciliation_schedule"
down_revision = "043_durable_join_budget"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in ("reconciliation_checked_at", "reconciliation_next_at"):
        op.add_column(
            "acquisition_auto_join_attempt", sa.Column(name, sa.DateTime(), nullable=True)
        )
    op.create_index(
        "idx_auto_join_reconciliation_due",
        "acquisition_auto_join_attempt",
        ["reconciliation_next_at", "request_sent_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_auto_join_reconciliation_due", table_name="acquisition_auto_join_attempt")
    op.drop_column("acquisition_auto_join_attempt", "reconciliation_next_at")
    op.drop_column("acquisition_auto_join_attempt", "reconciliation_checked_at")
