from pathlib import Path

from scripts.apply_sql_migrations import DEFAULT_MIGRATIONS, _split_sql_statements


def test_durable_join_budget_migration_is_registered_and_keeps_pause_untouched():
    migration_name = "058_add_durable_join_request_budget.sql"
    assert migration_name in DEFAULT_MIGRATIONS

    migration_path = Path(__file__).resolve().parents[2] / "migrations" / migration_name
    content = migration_path.read_text(encoding="utf-8")
    statements = _split_sql_statements(content)

    assert len(statements) >= 6
    assert "request_state" in content
    assert "uq_auto_join_reservation_key" in content
    assert "SET DEFAULT 30" in content
    assert "SET DEFAULT 2880" in content
    assert "target_key" in content
    assert "review_deadline_at" in content
    assert "idx_group_membership_review_due" in content
    assert "'leave_failed')" in content
    assert "join_interval_min_minutes SET DEFAULT 48" in content
    assert "join_interval_max_minutes SET DEFAULT 120" in content
    assert "automation.account_risk_guard" not in content
    assert "jsonb_set(value::jsonb, '{enabled}', 'false'::jsonb, TRUE)" in content
    assert "WHERE key = 'automation.auto_join_scheduler'" in content


def test_join_reconciliation_schedule_sql_preserves_quota_and_pause():
    name = "059_add_join_reconciliation_schedule.sql"
    assert name in DEFAULT_MIGRATIONS
    assert DEFAULT_MIGRATIONS.index(name) > DEFAULT_MIGRATIONS.index("058_add_durable_join_request_budget.sql")
    content = (Path(__file__).resolve().parents[2] / "migrations" / name).read_text(
        encoding="utf-8"
    )
    assert len(_split_sql_statements(content)) == 2
    assert "reconciliation_checked_at" in content and "reconciliation_next_at" in content
    assert "IF NOT EXISTS" in content
    assert "UPDATE " not in content.upper() and "DELETE " not in content.upper()
    assert "system_setting" not in content


def test_reconciliation_alembic_roundtrip_retains_existing_requests(monkeypatch):
    import importlib.util

    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = (
        Path(__file__).resolve().parents[2]
        / "migrations/versions/044_add_join_reconciliation_schedule.py"
    )
    spec = importlib.util.spec_from_file_location("join_reconciliation_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "043_durable_join_budget"
    engine = sa.create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "CREATE TABLE acquisition_auto_join_attempt (id INTEGER PRIMARY KEY, request_sent_at TIMESTAMP, request_state VARCHAR(30), status VARCHAR(30))"
                )
            )
            connection.execute(
                sa.text(
                    "INSERT INTO acquisition_auto_join_attempt VALUES (1, '2026-09-22 09:00:00', 'outcome_unknown', 'pending')"
                )
            )
            original = connection.execute(
                sa.text("SELECT * FROM acquisition_auto_join_attempt")
            ).one()
            monkeypatch.setattr(module, "op", Operations(MigrationContext.configure(connection)))
            module.upgrade()
            columns = {
                col["name"]
                for col in sa.inspect(connection).get_columns("acquisition_auto_join_attempt")
            }
            assert {"reconciliation_checked_at", "reconciliation_next_at"} <= columns
            assert (
                connection.execute(
                    sa.text(
                        "SELECT id, request_sent_at, request_state, status FROM acquisition_auto_join_attempt"
                    )
                ).one()
                == original
            )
            module.downgrade()
            assert (
                connection.execute(sa.text("SELECT * FROM acquisition_auto_join_attempt")).one()
                == original
            )
    finally:
        engine.dispose()
