"""Group-wide advertising quotas and immutable feedback evidence."""

from pathlib import Path

from alembic import op

revision = "047_adaptive_group_frequency"
down_revision = "046_dynamic_growth_capacity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    path = Path(__file__).resolve().parents[1] / "062_add_adaptive_group_frequency.sql"
    for statement in path.read_text(encoding="utf-8").split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("Disable adaptive ads and retain frequency/send evidence on rollback.")
