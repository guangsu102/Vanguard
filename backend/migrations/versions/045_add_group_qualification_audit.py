"""Account scoped group qualification evidence and durable queue."""
from pathlib import Path
from alembic import op
revision = "045_group_qualification"
down_revision = "044_join_reconciliation_schedule"
branch_labels = None
depends_on = None

def upgrade() -> None:
    source = Path(__file__).resolve().parents[1] / "060_add_group_qualification_audit.sql"
    for statement in source.read_text(encoding="utf8").split(";"):
        if statement.strip():
            op.execute(statement)

def downgrade() -> None:
    op.drop_table("group_qualification_audit")
