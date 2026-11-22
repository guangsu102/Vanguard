"""Allow account deletion to cascade through account/member qualification history."""

from pathlib import Path

from alembic import op
from sqlalchemy import inspect

from scripts.apply_sql_migrations import _split_sql_statements

revision = "053_account_qualification_delete"
down_revision = "052_p5_release_control"
branch_labels = None
depends_on = None


def upgrade() -> None:
    source = Path(__file__).resolve().parents[1] / "075_fix_account_qualification_delete.sql"
    for statement in _split_sql_statements(source.read_text(encoding="utf-8")):
        op.execute(statement)


def downgrade() -> None:
    for fk in inspect(op.get_bind()).get_foreign_keys("group_qualification_audit"):
        if fk["constrained_columns"] not in (["account_id"], ["membership_id"]):
            continue
        op.drop_constraint(fk["name"], "group_qualification_audit", type_="foreignkey")
        op.create_foreign_key(
            fk["name"],
            "group_qualification_audit",
            fk["referred_table"],
            fk["constrained_columns"],
            fk["referred_columns"],
            referent_schema=fk["referred_schema"],
        )
