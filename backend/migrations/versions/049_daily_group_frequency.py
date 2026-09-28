"""Daily feedback from the preceding advertising cycle's last message."""

from pathlib import Path

from alembic import op

revision = "049_daily_group_frequency"
down_revision = "048_listener_continuity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    path = Path(__file__).resolve().parents[1] / "064_daily_group_frequency.sql"
    for statement in path.read_text(encoding="utf-8").split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Use application rollback and retain daily review and send evidence.")
