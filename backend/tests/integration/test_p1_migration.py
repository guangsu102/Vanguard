"""P1 DDL/runner contracts against a disposable PostgreSQL Unix socket only."""

import hashlib
import os
import re
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import URL, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import database
from scripts import apply_sql_migrations as runner

MIGRATION = "069_p1_runtime_reliability.sql"
SOURCE = Path(__file__).resolve().parents[2] / "migrations" / MIGRATION
INBOX_COLUMNS = {
    "lane", "available_at", "claim_owner", "claim_lease_expires_at", "coalesce_key",
    "coalesced_count", "checkpoint_stage", "checkpoint_revision", "checkpoint_json",
    "checkpoint_updated_at",
}
AUDIT_COLUMNS = {"checkpoint_stage", "checkpoint_revision", "checkpoint_json", "checkpoint_updated_at"}
INDEXES = {
    "ix_telegram_event_inbox_lane_ready": ("lane", "state", "available_at", "id"),
    "ix_telegram_event_inbox_claim_lease": ("state", "claim_lease_expires_at"),
    "ix_telegram_event_inbox_coalesce": ("account_id", "coalesce_key", "state"),
    "idx_group_qualification_checkpoint": ("membership_id", "checkpoint_revision", "checkpoint_stage"),
}

BASELINE_DDL = (
    """CREATE TABLE telegram_event_inbox (
        id SERIAL PRIMARY KEY, account_id INTEGER NOT NULL, event_key VARCHAR(64) NOT NULL,
        kind VARCHAR(20) NOT NULL, chat_id BIGINT, payload_json TEXT NOT NULL,
        state VARCHAR(32) NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        external_attempted BOOLEAN NOT NULL DEFAULT FALSE,
        received_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        next_attempt_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        started_at TIMESTAMP, completed_at TIMESTAMP, last_error VARCHAR(100),
        CONSTRAINT uq_telegram_event_inbox_identity UNIQUE (account_id, event_key)
    )""",
    """CREATE TABLE group_qualification_audit (
        id SERIAL PRIMARY KEY, membership_id INTEGER NOT NULL, account_id INTEGER NOT NULL,
        group_id INTEGER NOT NULL, batch_id VARCHAR(128) NOT NULL, policy_version VARCHAR(64) NOT NULL,
        content_scope VARCHAR(64) NOT NULL DEFAULT 'text_profile',
        state VARCHAR(32) NOT NULL DEFAULT 'queued', decision VARCHAR(32) NOT NULL DEFAULT 'unknown',
        reason VARCHAR(160), evidence_json TEXT NOT NULL DEFAULT '{}', evidence_hash VARCHAR(64),
        membership_joined_at TIMESTAMP, attempts INTEGER NOT NULL DEFAULT 0,
        next_retry_at TIMESTAMP, created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        checked_at TIMESTAMP, expires_at TIMESTAMP,
        CONSTRAINT uq_group_qualification_batch_member UNIQUE (batch_id, membership_id)
    )""",
    """INSERT INTO telegram_event_inbox
        (account_id,event_key,kind,chat_id,payload_json,state,attempts,external_attempted,received_at,next_attempt_at,last_error)
        VALUES (2,'legacy-listener','listener_fact',-100123,'{"kind":"checkpoint","cursor":73}',
                'pending',4,FALSE,'2026-10-10 01:02:03','2026-10-10 01:03:03','legacy_wait'),
               (34,'legacy-unknown','join',-100456,'{"result":"unknown"}',
                'reconciliation_required',9,TRUE,'2026-10-11 01:02:03','2026-10-11 01:03:03','external_unknown')""",
    """INSERT INTO group_qualification_audit
        (membership_id,account_id,group_id,batch_id,policy_version,state,decision,reason,evidence_json,
         membership_joined_at,attempts,next_retry_at,checked_at,expires_at)
        VALUES (60,2,40,'historical-manual','pp-ai-qualification-v3','manual_required','observe',
                'operator_evidence_required','{"facts":[7,8],"manual":true}',
                '2026-10-01 00:00:00',11,NULL,'2026-10-10 01:02:03','2026-11-09 01:02:03')""",
)


async def columns(connection, table):
    return set((await connection.execute(text("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema=current_schema() AND table_name=:table
    """), {"table": table})).scalars())


async def historical_rows(connection, table, added):
    # Compare the complete legacy row, including timestamps and JSON text;
    # newly added default columns are deliberately excluded from this equality.
    return (await connection.execute(text(
        f"SELECT to_jsonb(t) - CAST(:added AS text[]) FROM {table} t ORDER BY id"
    ), {"added": sorted(added)})).scalars().all()


@pytest_asyncio.fixture
async def migration_database(monkeypatch, tmp_path):
    socket = os.getenv("GROWTH_TEST_PG_SOCKET")
    if not socket:
        pytest.skip("requires disposable PostgreSQL Unix socket; no network/database URL fallback")
    assert Path(socket).is_absolute() and Path(socket, ".s.PGSQL.5432").is_socket()
    url = URL.create("postgresql+asyncpg", username="postgres", database="postgres", query={"host": socket})
    schema = "p1_migration_" + uuid.uuid4().hex
    admin = create_async_engine(url)
    engine = create_async_engine(url, pool_size=2, max_overflow=0,
                                 connect_args={"server_settings": {"search_path": schema}})
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        async with engine.begin() as connection:
            for statement in BASELINE_DDL:
                await connection.exec_driver_sql(statement)
            baseline = {
                "telegram_event_inbox": await historical_rows(connection, "telegram_event_inbox", INBOX_COLUMNS),
                "group_qualification_audit": await historical_rows(connection, "group_qualification_audit", AUDIT_COLUMNS),
            }
        monkeypatch.setattr(database, "async_session_factory", async_sessionmaker(engine, expire_on_commit=False))
        # Exercise the actual session commit/rollback wrapper without allowing
        # the CLI's final cleanup to dispose an unrelated application engine.
        monkeypatch.setattr(database, "close_db", AsyncMock())
        monkeypatch.setattr(database, "init_db", AsyncMock(side_effect=AssertionError("test must not initialize an application database")))
        monkeypatch.setattr(runner, "MIGRATIONS_DIR", tmp_path)
        (tmp_path / MIGRATION).write_text(SOURCE.read_text(encoding="utf-8"), encoding="utf-8")
        yield engine, baseline, tmp_path
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


def test_p1_migration_is_registered_additive_and_has_no_historical_dml():
    source = SOURCE.read_text(encoding="utf-8")
    statements = runner._split_sql_statements(source)
    assert MIGRATION in runner.DEFAULT_MIGRATIONS
    assert statements
    sql = re.sub(r"--[^\n]*|/\*.*?\*/", "", source, flags=re.S)
    assert not re.search(r"\b(UPDATE|DELETE|INSERT|TRUNCATE|DROP)\b|\bALTER\s+COLUMN\b", sql, re.I)
    assert "oracle-shared-postgres" not in sql
    assert re.search(
        r"JOIN\s+pg_namespace\s+n\s+ON\s+n\.oid\s*=\s*t\.relnamespace.*?"
        r"n\.nspname\s*=\s*current_schema\(\)",
        sql,
        re.I | re.S,
    )


@pytest.mark.asyncio
async def test_p1_migration_repeat_preserves_history_rows_constraints_and_indexes(migration_database):
    engine, baseline, directory = migration_database
    source = (directory / MIGRATION).read_text(encoding="utf-8")
    await runner._apply([MIGRATION])
    async with engine.connect() as connection:
        first_history = (await connection.execute(text(
            "SELECT filename,checksum,applied_at FROM schema_migration_history"
        ))).one()
    assert first_history.filename == MIGRATION
    assert first_history.checksum == hashlib.sha256(source.encode("utf-8")).hexdigest()
    await runner._apply([MIGRATION])
    # Independently repeat the DDL as well, rather than only testing the
    # runner's history skip branch. Both paths must leave history and data intact.
    async with engine.begin() as connection:
        for statement in runner._split_sql_statements(source):
            await connection.exec_driver_sql(statement)
        assert (await connection.execute(text(
            "SELECT filename,checksum,applied_at FROM schema_migration_history"
        ))).one() == first_history
        assert INBOX_COLUMNS <= await columns(connection, "telegram_event_inbox")
        assert AUDIT_COLUMNS <= await columns(connection, "group_qualification_audit")
        for table, added in (("telegram_event_inbox", INBOX_COLUMNS), ("group_qualification_audit", AUDIT_COLUMNS)):
            assert await historical_rows(connection, table, added) == baseline[table]
        indexes = dict((await connection.execute(text(
            "SELECT indexname,indexdef FROM pg_indexes WHERE schemaname=current_schema()"
        ))).all())
        for name, fields in INDEXES.items():
            assert name in indexes
            assert "(" + ", ".join(fields) + ")" in indexes[name]
        constraints = dict((await connection.execute(text("""
            SELECT c.conname,pg_get_constraintdef(c.oid) FROM pg_constraint c
            JOIN pg_class t ON t.oid=c.conrelid JOIN pg_namespace n ON n.oid=t.relnamespace
            WHERE n.nspname=current_schema() AND t.relname='telegram_event_inbox'
        """))).all())
        assert "listener_fact" in constraints["ck_telegram_event_inbox_lane"]
        assert "business" in constraints["ck_telegram_event_inbox_lane"]
        assert "uq_telegram_event_inbox_identity" in constraints
        # Historical listener rows intentionally retain the additive default;
        # the migration does not relabel or reopen old business work.
        assert (await connection.execute(text(
            "SELECT lane,coalesced_count,claim_owner,checkpoint_json FROM telegram_event_inbox ORDER BY id"
        ))).all() == [("business", 0, None, None), ("business", 0, None, None)]
        for lane in ("listener_fact", "business"):
            await connection.execute(text("""
                INSERT INTO telegram_event_inbox(account_id,event_key,kind,payload_json,lane)
                VALUES(2,:key,'message','{}',:lane)
            """), {"key": "new-" + lane, "lane": lane})
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await connection.execute(text("""
                    INSERT INTO telegram_event_inbox(account_id,event_key,kind,payload_json,lane)
                    VALUES(2,'invalid-lane','message','{}','unknown_lane')
                """))


@pytest.mark.asyncio
async def test_lane_constraint_probe_is_limited_to_current_schema(migration_database):
    engine, _, _ = migration_database
    decoy = "p1_decoy_" + uuid.uuid4().hex
    async with engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{decoy}"'))
        await connection.execute(text(f'''CREATE TABLE "{decoy}".telegram_event_inbox (
            lane VARCHAR(24),
            CONSTRAINT ck_telegram_event_inbox_lane CHECK (lane IN ('decoy'))
        )'''))
    try:
        await runner._apply([MIGRATION])
        async with engine.connect() as connection:
            current = (await connection.execute(text("""
                SELECT pg_get_constraintdef(c.oid)
                FROM pg_constraint c
                JOIN pg_class t ON t.oid=c.conrelid
                JOIN pg_namespace n ON n.oid=t.relnamespace
                WHERE c.conname='ck_telegram_event_inbox_lane'
                  AND t.relname='telegram_event_inbox'
                  AND n.nspname=current_schema()
            """))).scalar_one()
            foreign = (await connection.execute(text("""
                SELECT pg_get_constraintdef(c.oid)
                FROM pg_constraint c
                JOIN pg_class t ON t.oid=c.conrelid
                JOIN pg_namespace n ON n.oid=t.relnamespace
                WHERE c.conname='ck_telegram_event_inbox_lane'
                  AND t.relname='telegram_event_inbox'
                  AND n.nspname=:schema
            """), {"schema": decoy})).scalar_one()
            assert "listener_fact" in current and "business" in current
            assert "decoy" in foreign
            assert "listener_fact" not in foreign and "business" not in foreign
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{decoy}" CASCADE'))


@pytest.mark.asyncio
async def test_p1_migration_checksum_mismatch_rejects_changed_ddl(migration_database):
    engine, _, directory = migration_database
    await runner._apply([MIGRATION])
    async with engine.connect() as connection:
        before = (await connection.execute(text("SELECT * FROM schema_migration_history"))).all()
    with (directory / MIGRATION).open("a", encoding="utf-8") as handle:
        handle.write("\nALTER TABLE telegram_event_inbox ADD COLUMN unexpected_checksum_column TEXT;\n")
    with pytest.raises(RuntimeError, match="Migration checksum changed after apply"):
        await runner._apply([MIGRATION])
    async with engine.connect() as connection:
        assert "unexpected_checksum_column" not in await columns(connection, "telegram_event_inbox")
        assert (await connection.execute(text("SELECT * FROM schema_migration_history"))).all() == before


@pytest.mark.asyncio
async def test_p1_migration_fault_rolls_back_every_ddl_and_history_entry(migration_database):
    engine, baseline, directory = migration_database
    (directory / "000_test_baseline.sql").write_text("SELECT 1;\n", encoding="utf-8")
    with (directory / MIGRATION).open("a", encoding="utf-8") as handle:
        handle.write("\nSELECT 1/0;\n")
    with pytest.raises(DBAPIError):
        await runner._apply(["000_test_baseline.sql", MIGRATION])
    async with engine.connect() as connection:
        assert not (INBOX_COLUMNS & await columns(connection, "telegram_event_inbox"))
        assert not (AUDIT_COLUMNS & await columns(connection, "group_qualification_audit"))
        assert (await connection.execute(text("SELECT filename FROM schema_migration_history"))).scalars().all() == ["000_test_baseline.sql"]
        actual_indexes = set((await connection.execute(text(
            "SELECT indexname FROM pg_indexes WHERE schemaname=current_schema()"
        ))).scalars())
        assert not set(INDEXES) & actual_indexes
        for table, added in (("telegram_event_inbox", INBOX_COLUMNS), ("group_qualification_audit", AUDIT_COLUMNS)):
            assert await historical_rows(connection, table, added) == baseline[table]


@pytest.mark.asyncio
async def test_p1_migration_nowait_lock_failure_is_immediate_and_releases_partial_locks(migration_database):
    engine, baseline, _ = migration_database
    sql = SOURCE.read_text(encoding="utf-8")
    assert re.search(r"LOCK\s+TABLE\s+group_qualification_audit\s*,\s*telegram_event_inbox\s+IN\s+ACCESS\s+EXCLUSIVE\s+MODE\s+NOWAIT", sql, re.I)
    async with engine.connect() as blocker:
        async with blocker.begin():
            await blocker.execute(text("LOCK TABLE telegram_event_inbox IN ACCESS SHARE MODE"))
            with pytest.raises(DBAPIError) as caught:
                await runner._apply([MIGRATION])
            assert caught.value.orig.sqlstate == "55P03"
            # The first lock was on the audit table. Its transaction must be
            # rolled back even while this other connection holds the inbox.
            async with engine.begin() as connection:
                await connection.execute(text("LOCK TABLE group_qualification_audit IN ACCESS EXCLUSIVE MODE NOWAIT"))
                assert not (INBOX_COLUMNS & await columns(connection, "telegram_event_inbox"))
                assert not (AUDIT_COLUMNS & await columns(connection, "group_qualification_audit"))
                assert await connection.scalar(text("SELECT to_regclass('schema_migration_history')")) is None
                assert await historical_rows(connection, "telegram_event_inbox", INBOX_COLUMNS) == baseline["telegram_event_inbox"]
    # No poisoned migration history or stale table locks block a clean retry.
    await runner._apply([MIGRATION])
