"""Release checks against a self-created, loopback-only disposable PostgreSQL.

Set VANGUARD_TEST_POSTGRES_BIN to a directory containing initdb and pg_ctl.
No application DATABASE_URL is read; a fresh cluster is created and stopped.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import URL, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.account import budget_ledger, event_inbox
from app.core.account.control_models import AccountBudgetReservation
from app.core.account.models import TelegramAccount
from app.core.config import settings
from app.core.database import Base
from app.core.worker_status import TelegramEventInbox
from app.modules.acquisition.p2_models import GrowthOperation, GrowthOperationAttempt, GrowthOutbox
from app.modules.acquisition.p2_outbox import (
    ActorResult,
    P2TargetBlocked,
    UnknownExternalOutcome,
    dispatch_outbox_once,
    enqueue_operation,
    reconcile_growth_operation,
    transition_operation,
)
from app.modules.acquisition.qualification_snapshot import (
    QualificationSnapshot,
    bump_fact_version,
    create_or_get_snapshot,
)
from scripts.apply_sql_migrations import DEFAULT_MIGRATIONS, MIGRATIONS_DIR, _split_sql_statements
from tests.conftest import _import_models


@pytest.fixture(scope="module")
def isolated_pg_url(tmp_path_factory):
    configured = os.environ.get("VANGUARD_TEST_POSTGRES_BIN")
    if not configured:
        pytest.skip("set VANGUARD_TEST_POSTGRES_BIN for a self-created disposable cluster")
    binaries = Path(configured).resolve(strict=True)
    root = tmp_path_factory.mktemp("vanguard-p0-p4-postgres")
    data = root / "data"
    suffix = ".exe" if os.name == "nt" else ""

    def run(name, *args):
        # Windows postgres children can inherit pg_ctl's pipe handles. A log
        # file avoids communicate() waiting for a long-lived server to exit.
        with (root / "commands.log").open("ab") as log:
            return subprocess.run(
                [str(binaries / (name + suffix)), *map(str, args)],
                check=True, stdout=log, stderr=log, timeout=45,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )

    run("initdb", "-D", data, "-U", "postgres", "-A", "trust", "--locale=C", "--encoding=UTF8")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    started = False
    try:
        run("pg_ctl", "-D", data, "-l", root / "postgres.log", "-w", "-t", "30",
            "-o", f"-h 127.0.0.1 -p {port} -c shared_buffers=16MB -c max_connections=20", "start")
        started = True
        yield URL.create("postgresql+asyncpg", username="postgres", host="127.0.0.1", port=port, database="postgres")
    finally:
        if started or (data / "postmaster.pid").exists():
            run("pg_ctl", "-D", data, "-w", "-t", "30", "-m", "fast", "stop")


@pytest_asyncio.fixture
async def pg_sessions(isolated_pg_url):
    _import_models()
    engine = create_async_engine(isolated_pg_url, pool_size=12, max_overflow=0,
                                 connect_args={"command_timeout": 30})
    # Create the legacy baseline, then apply the actual P0-P4 SQL scripts.
    # This catches SQL-only triggers and indexes which SQLite does not test.
    baseline = {"telegram_account", "group", "ad_campaign", "group_qualification_audit", "telegram_event_inbox"}
    pending = list(baseline)
    while pending:
        for foreign_key in Base.metadata.tables[pending.pop()].foreign_keys:
            dependency = foreign_key.column.table.name
            if dependency not in baseline:
                baseline.add(dependency)
                pending.append(dependency)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
            await conn.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[
                table for table in Base.metadata.sorted_tables if table.name in baseline
            ]))
            for _ in range(2):
                for name in DEFAULT_MIGRATIONS:
                    if int(name.split("_", 1)[0]) >= 67:
                        for statement in _split_sql_statements((MIGRATIONS_DIR / name).read_text(encoding="utf-8")):
                            await conn.execute(text(statement))
        sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
        async with sessions.begin() as db:
            db.add(TelegramAccount(id=1, identifier="p0p4-isolated", session_name="p0p4-isolated"))
        yield sessions
    finally:
        await engine.dispose()


async def snapshot_and_reservation(db):
    snapshot = await create_or_get_snapshot(
        db, target_key="group:gate", group_version="1", rule_version="rule:1",
        evidence={"member": True}, verdict="allowed", source_fact_version="1",
    )
    await budget_ledger.reserve(
        db, "release-gate", account_id=1, workload="growth", lane="ad", reserved_reads=1,
        lease_expires_at=datetime.utcnow() + timedelta(minutes=10),
    )
    return snapshot


async def enqueue(db, snapshot):
    return await enqueue_operation(
        db, kind="join_group", account_id=1, target_key=snapshot.target_key,
        snapshot_id=snapshot.snapshot_id, group_version=snapshot.group_version,
        rule_version=snapshot.rule_version, evidence_hash=snapshot.evidence_hash,
        budget_reservation_id="release-gate", payload={"target": "gate"}, run_id="run:1",
    )


@pytest.mark.asyncio
async def test_pg_concurrent_snapshot_enqueue_and_budget_settlement(pg_sessions):
    async def submit():
        async with pg_sessions.begin() as db:
            snapshot = await snapshot_and_reservation(db)
            operation, _, _ = await enqueue(db, snapshot)
            return operation.id

    ids = await asyncio.gather(*(submit() for _ in range(12)))
    assert len(set(ids)) == 1
    async with pg_sessions() as db:
        for model in (QualificationSnapshot, GrowthOperation, GrowthOutbox, AccountBudgetReservation):
            assert await db.scalar(select(func.count()).select_from(model)) == 1

    async def settle():
        async with pg_sessions.begin() as db:
            return await budget_ledger.settle(db, "release-gate", 1)

    assert sum(await asyncio.gather(*(settle() for _ in range(12)))) == 1


@pytest.mark.asyncio
async def test_pg_attempt_is_committed_before_actor_and_trigger_accepts_dispatch(pg_sessions):
    async with pg_sessions.begin() as db:
        operation, _, _ = await enqueue(db, await snapshot_and_reservation(db))
        operation_id = operation.id

    async def actor(operation, _payload, fence):
        async with pg_sessions() as observer:
            assert await observer.scalar(select(func.count()).select_from(GrowthOperationAttempt)) == 1
        return ActorResult("confirmed", receipt={"membership": True}, actor_generation=fence)

    async with pg_sessions() as db:
        await dispatch_outbox_once(db, owner="isolated", fencing_token="generation:1", actor_submitter=actor)
    async with pg_sessions() as db:
        assert (await db.get(GrowthOperation, operation_id)).state == "resolved"
        assert (await db.scalar(select(GrowthOutbox))).state == "succeeded"


@pytest.mark.asyncio
async def test_pg_fact_increments_are_atomic_and_snapshots_immutable(pg_sessions):
    async def increment():
        async with pg_sessions.begin() as db:
            return await bump_fact_version(db, "group:concurrent")

    assert set(await asyncio.gather(*(increment() for _ in range(12)))) == {str(v) for v in range(1, 13)}
    async with pg_sessions.begin() as db:
        await snapshot_and_reservation(db)
    with pytest.raises(DBAPIError, match="immutable"):
        async with pg_sessions.begin() as db:
            await db.execute(text("UPDATE qualification_snapshot SET evidence_json = '{}'"))


@pytest.mark.asyncio
async def test_pg_terminal_reconciliation_and_orphans_rejected(pg_sessions):
    async with pg_sessions.begin() as db:
        await enqueue(db, await snapshot_and_reservation(db))
    with pytest.raises(DBAPIError):
        async with pg_sessions.begin() as db:
            await db.execute(text("UPDATE growth_operation SET state = 'failed', reconciliation_required = true, state_version = state_version + 1"))
    with pytest.raises(DBAPIError):
        async with pg_sessions.begin() as db:
            db.add(GrowthOutbox(operation_id=9999, account_id=1, kind="join_group"))


@pytest.mark.asyncio
async def test_pg_canonical_migrations_include_all_p4_tables(pg_sessions):
    async with pg_sessions() as db:
        for table in ("growth_pacing_decision", "growth_pacing_daily_account_stat", "growth_history_governance_batch", "growth_operation_archive"):
            assert await db.scalar(text("SELECT to_regclass(:table)"), {"table": table}) == table


@pytest.mark.asyncio
async def test_pg_p1_claims_are_exclusive_and_attempted_expiry_is_not_replayed(pg_sessions, monkeypatch):
    monkeypatch.setattr(settings, "P1_RUNTIME_RELIABILITY_ENABLED", True)
    async with pg_sessions.begin() as db:
        for index in range(12):
            db.add(TelegramEventInbox(account_id=1, event_key=f"gate:{index}",
                                     kind="listener_fact", chat_id=-100000 - index,
                                     payload_json="{}", state="pending"))

    async def claim(index):
        async with pg_sessions.begin() as db:
            rows = await event_inbox.claim_batch(db, [1], local_only=True, limit=1, owner=f"worker:{index}")
            return [row.id for row in rows]

    claimed = [item for batch in await asyncio.gather(*(claim(i) for i in range(12))) for item in batch]
    assert len(claimed) == len(set(claimed)) == 12
    async with pg_sessions.begin() as db:
        row = await db.get(TelegramEventInbox, claimed[0])
        row.external_attempted = True
        row.claim_lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
    async with pg_sessions.begin() as db:
        assert (await event_inbox.recover_expired_leases(db))["reconciled"] == 1
        assert await event_inbox.claim_batch(db, [1], local_only=True, limit=1) == []


@pytest.mark.asyncio
async def test_pg_archived_operation_remains_idempotent(pg_sessions):
    from app.modules.acquisition.history_governance import archive_batch

    async with pg_sessions.begin() as db:
        snapshot = await snapshot_and_reservation(db)
        operation, _, _ = await enqueue(db, snapshot)
        original_id = operation.id
        await transition_operation(db, original_id, "failed", now=datetime.utcnow() - timedelta(days=100))
    async with pg_sessions() as db:
        archived = await archive_batch(db, datetime.utcnow())
        assert archived["archived_count"] == 1
    async with pg_sessions.begin() as db:
        repeated, _, reused = await enqueue(db, snapshot)
        assert reused and repeated.id == original_id


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome, final_state, outbox_state", [
    ("confirmed", "resolved", "succeeded"),
    ("failed", "failed", "dead"),
])
async def test_pg_reconciliation_and_outbox_commit_together(
    pg_sessions, outcome, final_state, outbox_state,
):
    async with pg_sessions.begin() as db:
        operation, _, _ = await enqueue(db, await snapshot_and_reservation(db))
        operation_id = operation.id

    async def actor(_operation, _payload, _fence):
        raise UnknownExternalOutcome("provider_timeout")

    async with pg_sessions() as db:
        await dispatch_outbox_once(db, owner="sender", fencing_token="generation", actor_submitter=actor)

    async def verifier(_operation):
        return {"outcome": outcome, "receipt": {"membership": outcome == "confirmed"}}

    with pytest.raises(RuntimeError, match="rollback verification"):
        async with pg_sessions.begin() as db:
            await reconcile_growth_operation(db, operation_id, verifier=verifier)
            raise RuntimeError("rollback verification")
    async with pg_sessions() as db:
        assert (await db.get(GrowthOperation, operation_id)).reconciliation_required
        assert (await db.scalar(select(GrowthOutbox))).state == "reconciliation_required"
        assert await db.scalar(select(func.count()).select_from(GrowthOperationAttempt)) == 1
    async with pg_sessions.begin() as db:
        assert (await reconcile_growth_operation(db, operation_id, verifier=verifier))["state"] == "succeeded"
    async with pg_sessions() as db:
        operation = await db.get(GrowthOperation, operation_id)
        outbox = await db.scalar(select(GrowthOutbox))
        assert operation.state == final_state and not operation.reconciliation_required
        assert outbox.state == outbox_state and outbox.completed_at is not None
        assert outbox.lease_owner is outbox.lease_expires_at is outbox.fencing_token is None


@pytest.mark.asyncio
async def test_pg_concurrent_reconciliation_rejects_changed_operation(pg_sessions):
    from app.modules.acquisition.p2_outbox import mark_reconciliation_required

    async with pg_sessions.begin() as db:
        operation, _, _ = await enqueue(db, await snapshot_and_reservation(db))
        operation_id = operation.id
        await mark_reconciliation_required(db, operation_id, reason="unknown")
    ready = asyncio.Event()
    entered = 0

    async def verifier(_operation):
        nonlocal entered
        entered += 1
        if entered == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), timeout=5)
        return {"outcome": "confirmed", "receipt": {"membership": True}}

    async def reconcile():
        async with pg_sessions.begin() as db:
            return await reconcile_growth_operation(db, operation_id, verifier=verifier)

    results = await asyncio.gather(reconcile(), reconcile())
    assert sorted(result["state"] for result in results) == ["pending", "succeeded"]
    assert next(result for result in results if result["state"] == "pending")["error"] == "p2_reconciliation_state_changed"
    async with pg_sessions() as db:
        assert (await db.get(GrowthOperation, operation_id)).state == "resolved"
        assert (await db.scalar(select(GrowthOutbox))).state == "succeeded"
        assert await db.scalar(select(func.count()).select_from(GrowthOperationAttempt)) == 1


@pytest.mark.asyncio
async def test_pg_legacy_archive_remains_duplicate_guard_after_cleanup(pg_sessions):
    from app.modules.acquisition.history_governance import archive_batch, cleanup_expired

    async with pg_sessions.begin() as db:
        snapshot = await snapshot_and_reservation(db)
        operation, _, _ = await enqueue(db, snapshot)
        await transition_operation(db, operation.id, "failed", now=datetime.utcnow() - timedelta(days=100))
    async with pg_sessions() as db:
        assert (await archive_batch(db, datetime.utcnow()))["archived_count"] == 1
    async with pg_sessions.begin() as db:
        # Simulate an archive produced by the old delete-after-archive policy.
        await db.execute(text("DELETE FROM growth_operation"))
        await db.execute(text("UPDATE growth_operation_archive SET archived_at = :old"),
                         {"old": datetime.utcnow() - timedelta(days=400)})
    async with pg_sessions() as db:
        assert (await cleanup_expired(db, datetime.utcnow()))["archives"] == 0
    with pytest.raises(P2TargetBlocked):
        async with pg_sessions.begin() as db:
            await enqueue(db, snapshot)
