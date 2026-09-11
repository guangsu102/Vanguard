"""Schema contract for stage-2 owned-group audit resource types."""

from pathlib import Path

from sqlalchemy import BigInteger

from app.modules.owned_group.models_extra import OwnedGroupAuditEvent
from scripts.apply_sql_migrations import DEFAULT_MIGRATIONS, _split_sql_statements

BACKEND_ROOT = Path(__file__).resolve().parents[4]
MIGRATION_NAME = "049_widen_owned_group_audit_resource_type.sql"


def test_audit_resource_type_model_allows_stage2_identifier() -> None:
    column = OwnedGroupAuditEvent.__table__.c.resource_type

    assert column.type.length == 32
    assert len("message_execution") <= column.type.length
    assert isinstance(OwnedGroupAuditEvent.__table__.c.resource_id.type, BigInteger)


def test_curated_sql_migration_widens_without_narrowing_future_schema() -> None:
    assert DEFAULT_MIGRATIONS.index(MIGRATION_NAME) == (
        DEFAULT_MIGRATIONS.index("048_add_owned_group_messaging.sql") + 1
    )
    sql = (BACKEND_ROOT / "migrations" / MIGRATION_NAME).read_text(encoding="utf-8")
    statements = _split_sql_statements(sql)
    normalized = " ".join(statements[0].split())

    assert len(statements) == 1
    assert "current_length IS NOT NULL AND current_length < 32" in normalized
    assert "ALTER COLUMN resource_type TYPE VARCHAR(32)" in normalized
    assert "resource_id_udt = 'int4'" in normalized
    assert "resource_id_udt <> 'int8'" in normalized
    assert "ALTER COLUMN resource_id TYPE BIGINT USING resource_id::bigint" in normalized
    assert "apply migration 044 first" in normalized


def test_alembic_chain_contains_matching_audit_width_revision() -> None:
    migration = (
        BACKEND_ROOT / "migrations" / "versions" / "035_widen_owned_group_audit_resource_type.py"
    ).read_text(encoding="utf-8")

    assert 'revision = "035_owned_group_audit_resource_type"' in migration
    assert 'down_revision = "034_owned_group_messaging"' in migration
    assert "type_=sa.String(length=32)" in migration
    assert "type_=sa.BigInteger()" in migration
    assert 'postgresql_using="resource_id::bigint"' in migration
