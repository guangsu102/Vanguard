"""Add membership permission and next-ad state without changing live policy."""
from pathlib import Path

from alembic import op

from scripts.apply_sql_migrations import _split_sql_statements

revision = "055_growth_permissions"
down_revision = "054_qualification_work_grants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    source = Path(__file__).resolve().parents[1] / "077_growth_permissions.sql"
    for statement in _split_sql_statements(source.read_text(encoding="utf-8")):
        op.execute(statement)


def downgrade() -> None:
    # An application rollback keeps these harmless columns and all result data.
    pass
