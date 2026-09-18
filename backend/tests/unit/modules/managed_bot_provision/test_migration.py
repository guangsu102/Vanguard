from __future__ import annotations

from pathlib import Path

from sqlalchemy import UniqueConstraint

from app.core.account.models import ManagedBotProvision
from scripts.apply_sql_migrations import DEFAULT_MIGRATIONS, _split_sql_statements

BACKEND_ROOT = Path(__file__).resolve().parents[4]
SQL_MIGRATION = "054_add_managed_bot_provisions.sql"


def test_managed_bot_model_and_sql_migration_match_contract():
    table = ManagedBotProvision.__table__
    expected_columns = {
        "id",
        "idempotency_key",
        "request_hash",
        "owner_account_id",
        "manager_bot_profile_id",
        "display_name",
        "username",
        "status",
        "current_step",
        "bot_user_id",
        "guardian_bot_profile_id",
        "owned_bot_profile_id",
        "attempts",
        "max_attempts",
        "retryable",
        "error_code",
        "error_message",
        "next_retry_at",
        "celery_task_id",
        "lease_id",
        "lease_expires_at",
        "heartbeat_at",
        "create_attempted_at",
        "external_created_at",
        "started_at",
        "finished_at",
        "created_by_id",
        "created_at",
        "updated_at",
    }
    assert set(table.columns.keys()) == expected_columns
    assert next(iter(table.c.owner_account_id.foreign_keys)).ondelete == "RESTRICT"
    assert next(iter(table.c.manager_bot_profile_id.foreign_keys)).ondelete == "RESTRICT"
    assert next(iter(table.c.guardian_bot_profile_id.foreign_keys)).ondelete == "SET NULL"
    assert next(iter(table.c.owned_bot_profile_id.foreign_keys)).ondelete == "SET NULL"
    assert table.c.username.unique is not True
    assert not any(
        isinstance(constraint, UniqueConstraint)
        and {column.name for column in constraint.columns} == {"username"}
        for constraint in table.constraints
    )
    reserved_username_index = next(
        index
        for index in table.indexes
        if index.name == "uq_managed_bot_provision_reserved_username"
    )
    assert reserved_username_index.unique is True
    assert reserved_username_index.dialect_options["postgresql"]["where"] is not None
    assert reserved_username_index.dialect_options["sqlite"]["where"] is not None

    migration = BACKEND_ROOT / "migrations" / SQL_MIGRATION
    statements = _split_sql_statements(migration.read_text(encoding="utf-8"))
    assert SQL_MIGRATION in DEFAULT_MIGRATIONS
    assert len(statements) == 5
    assert "CREATE TABLE IF NOT EXISTS managed_bot_provision" in statements[0]
    assert "create_attempted_at" in statements[0]
    assert "REFERENCES owned_bot_profiles(id) ON DELETE SET NULL" in statements[0]
    assert "username VARCHAR(32) NOT NULL UNIQUE" not in statements[0]
    assert "uq_managed_bot_provision_reserved_username" in statements[1]
    assert "CREATE UNIQUE INDEX IF NOT EXISTS" in statements[1]
    assert "WHERE status IN" in statements[1]
    assert "OR bot_user_id IS NOT NULL" in statements[1]
    assert "OR external_created_at IS NOT NULL" in statements[1]


def test_alembic_revision_extends_current_head():
    migration = (
        BACKEND_ROOT
        / "migrations"
        / "versions"
        / "040_add_managed_bot_provisions.py"
    ).read_text(encoding="utf-8")
    assert 'revision = "040_managed_bot_provisions"' in migration
    assert 'down_revision = "039_account_spam_checks"' in migration
    assert '"managed_bot_provision"' in migration
    assert 'sa.UniqueConstraint("username")' not in migration
    assert '"uq_managed_bot_provision_reserved_username"' in migration
    assert "unique=True" in migration
    assert "postgresql_where=sa.text(" in migration
