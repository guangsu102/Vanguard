from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateTable

from app.core.account.models import TelegramAccount  # noqa: F401
from app.core.account.persona import NEUTRAL_PERSONA_HASH
from app.core.group.models import Group  # noqa: F401
from app.modules.acquisition.models import KeywordTrigger, MessageTemplate  # noqa: F401
from app.modules.owned_group.messaging_models import GroupAccountMessageExecution
from app.modules.owned_group.models import OwnedGroupAsset  # noqa: F401

BACKEND_ROOT = Path(__file__).resolve().parents[1]
RAW_MIGRATION = (
    BACKEND_ROOT / "migrations" / "051_add_owned_group_message_persona_snapshot.sql"
)
ALEMBIC_MIGRATION = (
    BACKEND_ROOT
    / "migrations"
    / "versions"
    / "037_add_owned_group_message_persona_snapshot.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "migration_037_add_owned_group_message_persona_snapshot",
        ALEMBIC_MIGRATION,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_execution_model_uses_nullable_json_and_exact_hex_checks() -> None:
    snapshot_type = GroupAccountMessageExecution.__table__.c.persona_snapshot.type
    assert snapshot_type.dialect_impl(sqlite.dialect()).none_as_null is True
    assert snapshot_type.dialect_impl(postgresql.dialect()).none_as_null is True

    postgresql_ddl = str(
        CreateTable(GroupAccountMessageExecution.__table__).compile(
            dialect=postgresql.dialect()
        )
    )
    sqlite_ddl = str(
        CreateTable(GroupAccountMessageExecution.__table__).compile(dialect=sqlite.dialect())
    )
    assert postgresql_ddl.count("~ '^[0-9a-f]{64}$'") == 3
    assert "GLOB" not in postgresql_ddl
    assert sqlite_ddl.count("NOT GLOB '*[^0-9a-f]*'") == 3
    assert "owned-group-neutral-legacy-v1" not in postgresql_ddl


def test_raw_migration_has_stop_window_checks_and_exact_legacy_snapshot() -> None:
    sql = RAW_MIGRATION.read_text(encoding="utf-8")
    assert "LOCK TABLE group_account_message_execution IN SHARE ROW EXCLUSIVE MODE" in sql
    assert "status IN ('generating', 'sending')" in sql
    assert "stage3 persona queued execution mapping precheck failed" in sql
    assert "execution.account_id <> policy.account_id" in sql
    assert "execution.telegram_chat_id <> core_group.group_id" in sql
    assert "business_snapshot_v1" in sql
    assert "owned-group-neutral-legacy-v1" in sql
    assert NEUTRAL_PERSONA_HASH in sql
    assert "U&'[\\200B-\\200F\\202A-\\202E\\2060\\2066-\\2069\\FEFF]'" in sql
    assert "prompt_hash =" not in sql
    assert "governance_rules_hash =" not in sql


def test_alembic_revision_is_linear_and_compiles_postgresql() -> None:
    module = _load_migration()
    assert module.revision == "037_message_persona_snapshot"
    assert module.down_revision == "036_add_account_ai_persona"

    output = io.StringIO()
    context = MigrationContext.configure(
        url="postgresql://",
        opts={"as_sql": True, "output_buffer": output},
    )
    module.op = Operations(context)
    module.upgrade()
    ddl = output.getvalue()
    assert "ADD COLUMN persona_revision_snapshot INTEGER" in ddl
    assert "ADD COLUMN persona_snapshot JSONB" in ddl
    assert "SHARE ROW EXCLUSIVE MODE" in ddl
    assert "owned-group-neutral-legacy-v1" in ddl
    assert "business_snapshot_v1" in ddl
    assert "~ '^[0-9a-f]{64}$'" in ddl
    assert "U&'[\\200B-\\200F\\202A-\\202E\\2060\\2066-\\2069\\FEFF]'" in ddl


def test_sqlite_upgrade_backfills_queued_and_preserves_template_nulls() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE "group" (
                    id INTEGER PRIMARY KEY,
                    group_id BIGINT NOT NULL,
                    title VARCHAR(255)
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE owned_group_assets (
                    id INTEGER PRIMARY KEY,
                    core_group_id INTEGER,
                    telegram_chat_id BIGINT
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE group_account_message_policy (
                    id INTEGER PRIMARY KEY,
                    owned_group_asset_id INTEGER NOT NULL,
                    core_group_id INTEGER NOT NULL,
                    account_id INTEGER NOT NULL,
                    allowed_topics JSON NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE group_account_message_execution (
                    id INTEGER PRIMARY KEY,
                    policy_id INTEGER NOT NULL,
                    owned_group_asset_id INTEGER NOT NULL,
                    core_group_id INTEGER NOT NULL,
                    telegram_chat_id BIGINT NOT NULL,
                    account_id INTEGER NOT NULL,
                    mode_snapshot VARCHAR(16) NOT NULL,
                    status VARCHAR(32) NOT NULL,
                    prompt_context JSON
                )
                """
            )
        )
        connection.execute(
            text("INSERT INTO \"group\" (id, group_id, title) VALUES (7, -1007, '  测试\\n群  ')")
        )
        connection.execute(
            text(
                "INSERT INTO owned_group_assets (id, core_group_id, telegram_chat_id) "
                "VALUES (8, 7, -1007)"
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO group_account_message_policy
                    (id, owned_group_asset_id, core_group_id, account_id, allowed_topics)
                VALUES
                    (9, 8, 7, 17, :allowed_topics)
                """
            ),
            {"allowed_topics": json.dumps([" 配置建议 ", "配置建议", "使用体验"])},
        )
        connection.execute(
            text(
                """
                INSERT INTO group_account_message_execution
                    (id, policy_id, owned_group_asset_id, core_group_id,
                     telegram_chat_id, account_id, mode_snapshot, status, prompt_context)
                VALUES
                    (10, 9, 8, 7, -1007, 17, 'ai', 'queued', :prompt_context),
                    (11, 9, 8, 7, -1007, 17, 'template', 'queued', NULL)
                """
            ),
            {
                "prompt_context": json.dumps(
                    {"client": "kept", "business_snapshot_v1": {"forged": True}}
                )
            },
        )

        module = _load_migration()
        context = MigrationContext.configure(connection)
        module.op = Operations(context)
        module.upgrade()

        queued = connection.execute(
            text(
                """
                SELECT persona_revision_snapshot, persona_source_snapshot,
                       persona_snapshot, persona_hash, prompt_template_version,
                       prompt_context, prompt_hash, governance_rules_hash
                FROM group_account_message_execution WHERE id = 10
                """
            )
        ).mappings().one()
        assert queued["persona_revision_snapshot"] == 0
        assert queued["persona_source_snapshot"] == "legacy_default"
        assert json.loads(queued["persona_snapshot"])["name"] == "中性群友"
        assert queued["persona_hash"] == NEUTRAL_PERSONA_HASH
        assert queued["prompt_template_version"] == "owned-group-neutral-legacy-v1"
        assert queued["prompt_hash"] is None
        assert queued["governance_rules_hash"] is None
        prompt_context = json.loads(queued["prompt_context"])
        assert prompt_context["client"] == "kept"
        assert prompt_context["business_snapshot_v1"] == {
            "allowed_topics": ["配置建议", "使用体验"],
            "group_title": "测试\\n群",
        }

        template = connection.execute(
            text(
                """
                SELECT persona_revision_snapshot, persona_source_snapshot,
                       persona_snapshot, persona_hash, prompt_template_version,
                       prompt_hash, governance_rules_hash
                FROM group_account_message_execution WHERE id = 11
                """
            )
        ).mappings().one()
        assert all(value is None for value in template.values())


def test_curated_sql_migrations_register_stage_three_in_order() -> None:
    source = (BACKEND_ROOT / "scripts" / "apply_sql_migrations.py").read_text(
        encoding="utf-8"
    )
    assert source.index('"049_widen_owned_group_audit_resource_type.sql"') < source.index(
        '"050_add_account_ai_persona.sql"'
    ) < source.index('"051_add_owned_group_message_persona_snapshot.sql"')
