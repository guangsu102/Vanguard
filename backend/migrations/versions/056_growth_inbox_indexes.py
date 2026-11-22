"""Bound inbox claim scans without rewriting or removing event history."""
from alembic import op

revision = "056_growth_inbox_indexes"
down_revision = "055_growth_permissions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_telegram_event_inbox_live_peer "
                   "ON telegram_event_inbox (account_id, chat_id, id) "
                   "WHERE state IN ('pending', 'processing', 'executing', 'reconciliation_required')")
        op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_telegram_event_inbox_live_account "
                   "ON telegram_event_inbox (account_id, id) WHERE state = 'pending'")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_telegram_event_inbox_live_account")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_telegram_event_inbox_live_peer")
