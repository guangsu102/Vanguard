from pathlib import Path

from scripts.apply_sql_migrations import DEFAULT_MIGRATIONS, _split_sql_statements

BACKEND_ROOT = Path(__file__).resolve().parents[4]


def test_spam_check_schema_precedes_managed_bot_and_does_not_use_new_enum_value():
    migration = "053_add_account_spam_checks.sql"
    assert migration in DEFAULT_MIGRATIONS
    assert DEFAULT_MIGRATIONS.index(migration) < DEFAULT_MIGRATIONS.index(
        "054_add_managed_bot_provisions.sql"
    )
    schema_sql = (BACKEND_ROOT / "migrations" / migration).read_text(encoding="utf-8")
    assert "ALTER TYPE accountstatus ADD VALUE IF NOT EXISTS 'RESTRICTED'" in schema_sql
    assert "status = 'RESTRICTED'::accountstatus" not in schema_sql
    assert "UPDATE telegram_account" not in schema_sql
    assert len(_split_sql_statements(schema_sql)) >= 2
