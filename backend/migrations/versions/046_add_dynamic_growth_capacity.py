"""Durable outbound capacity and verified qualification workflow context."""
from pathlib import Path
from alembic import op

revision = '046_dynamic_growth_capacity'
down_revision = '045_group_qualification'
branch_labels = None
depends_on = None


def upgrade() -> None:
    path = Path(__file__).resolve().parents[1] / '061_add_dynamic_growth_capacity.sql'
    for statement in path.read_text(encoding='utf-8-sig').split(';'):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    # Evidence is retained on rollback; the old application ignores additive fields.
    raise RuntimeError('Pause external writes and retain additive evidence schema when rolling back code.')
