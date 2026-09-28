"""Durable listener inbox and 720-hour qualification evidence lifetime."""
from pathlib import Path
from alembic import op

revision = "048_listener_continuity"
down_revision = "047_adaptive_group_frequency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    path = Path(__file__).resolve().parents[1] / "063_listener_inbox_qualification_lifetime.sql"
    for statement in path.read_text(encoding="utf-8-sig").split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    # Application rollback retains the inbox and original timestamps. The old
    # application's checked_at gate still enforces its former 24-hour policy.
    raise RuntimeError("Retain inbox and qualification evidence; use application rollback.")
