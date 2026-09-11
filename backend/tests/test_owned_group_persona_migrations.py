from __future__ import annotations

import importlib.util
import io
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateTable

from app.core.account.models import TelegramAccount

BACKEND_ROOT = Path(__file__).resolve().parents[1]
RAW_MIGRATION = BACKEND_ROOT / "migrations" / "050_add_account_ai_persona.sql"
ALEMBIC_MIGRATION = BACKEND_ROOT / "migrations" / "versions" / "036_add_account_ai_persona.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "migration_036_add_account_ai_persona",
        ALEMBIC_MIGRATION,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_model_uses_one_deferred_group_and_cross_database_json() -> None:
    # Inspect the already-declared properties without configuring every mapper in
    # the application; this migration test intentionally has no database fixture.
    attributes = {
        key: attribute
        for key, attribute in TelegramAccount.__mapper__._props.items()  # noqa: SLF001
        if key.startswith("ai_persona")
    }
    assert set(attributes) == {
        "ai_persona",
        "ai_persona_revision",
        "ai_persona_hash",
        "ai_persona_updated_at",
        "ai_persona_updated_by",
    }
    assert {attribute.group for attribute in attributes.values()} == {"account_persona"}

    persona_type = TelegramAccount.__table__.c.ai_persona.type
    assert persona_type.dialect_impl(sqlite.dialect()).none_as_null is True
    assert persona_type.dialect_impl(postgresql.dialect()).none_as_null is True
    assert persona_type.compile(dialect=postgresql.dialect()) == "JSONB"


def test_model_emits_dialect_specific_hash_checks() -> None:
    postgresql_ddl = str(
        CreateTable(TelegramAccount.__table__).compile(dialect=postgresql.dialect())
    )
    sqlite_ddl = str(CreateTable(TelegramAccount.__table__).compile(dialect=sqlite.dialect()))

    assert "~ '^[0-9a-f]{64}$'" in postgresql_ddl
    assert "GLOB" not in postgresql_ddl
    assert "NOT GLOB '*[^0-9a-f]*'" in sqlite_ddl
    assert "~ '^[0-9a-f]{64}$'" not in sqlite_ddl
    assert "ai_persona_revision >= 0" in postgresql_ddl
    assert "ai_persona_revision >= 0" in sqlite_ddl


def test_sqlite_enforces_persona_revision_and_hash_consistency() -> None:
    engine = create_engine("sqlite://")
    TelegramAccount.__table__.create(engine)
    table = TelegramAccount.__table__
    base = {"identifier": "persona-account", "session_name": "persona-session"}

    with engine.begin() as connection:
        connection.execute(table.insert().values(**base))

    invalid = {
        **base,
        "identifier": "bad-persona-account",
        "session_name": "bad-persona-session",
        "ai_persona": {"schema_version": 1},
        "ai_persona_revision": 1,
        "ai_persona_hash": "A" * 64,
    }
    import pytest
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(table.insert().values(**invalid))


def test_raw_migration_is_replay_safe_and_never_overwrites_settings() -> None:
    sql = RAW_MIGRATION.read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS ai_persona JSONB" in sql
    assert "ADD COLUMN IF NOT EXISTS ai_persona_revision INTEGER" in sql
    assert "ai_persona_hash !~ '^[0-9a-f]{64}$'" in sql
    assert "'app.runtime_settings'" in sql
    assert '"ownedGroupAiPersona"' in sql
    assert "ON CONFLICT (key) DO NOTHING" in sql
    assert "ON CONFLICT (key) DO UPDATE" not in sql


def test_alembic_revision_extends_stage_two_head_and_compiles_postgresql() -> None:
    module = _load_migration()
    assert module.revision == "036_add_account_ai_persona"
    assert module.down_revision == "035_owned_group_audit_resource_type"

    output = io.StringIO()
    context = MigrationContext.configure(
        url="postgresql://",
        opts={"as_sql": True, "output_buffer": output},
    )
    with Operations.context(context):
        module.upgrade()
    ddl = output.getvalue()
    assert "ADD COLUMN ai_persona JSONB" in ddl
    assert "ai_persona_hash ~ '^[0-9a-f]{64}$'" in ddl
    assert "ON CONFLICT (key) DO NOTHING" in ddl


def test_alembic_downgrade_removes_only_persona_node_not_settings_row() -> None:
    source = ALEMBIC_MIGRATION.read_text(encoding="utf-8")
    assert "- 'ownedGroupAiPersona'" in source
    assert "json_remove(value, '$.ownedGroupAiPersona')" in source
    assert "DELETE FROM system_setting" not in source
    assert "drop_table" not in source
