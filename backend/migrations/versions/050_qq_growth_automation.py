"""QQ accounts, shared creative bindings and durable growth operations."""

from pathlib import Path

from alembic import op

revision = "050_qq_growth_automation"
down_revision = "049_daily_group_frequency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    path = Path(__file__).resolve().parents[1] / "065_qq_growth_automation.sql"
    for statement in path.read_text(encoding="utf-8").split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Retain QQ operation receipts during application rollback.")
