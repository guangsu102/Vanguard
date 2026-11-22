"""Separate final qualification grants from resumable execution work."""

from pathlib import Path

from alembic import op

from scripts.apply_sql_migrations import _split_sql_statements

revision = "054_qualification_work_grants"
down_revision = "053_account_qualification_delete"
branch_labels = None
depends_on = None


def upgrade() -> None:
    source = Path(__file__).resolve().parents[1] / "076_qualification_work_grants.sql"
    for statement in _split_sql_statements(source.read_text(encoding="utf-8")):
        op.execute(statement)


def downgrade() -> None:
    op.drop_table("qualification_work")
    op.drop_table("qualification_active_grant")
    op.drop_column("acquisition_auto_join_attempt", "platform_responded_at")
    op.drop_column("acquisition_auto_join_attempt", "sdk_entered_at")
