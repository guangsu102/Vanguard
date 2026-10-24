"""Fault and concurrency tests using disposable PG/Redis Unix sockets only."""

import asyncio
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import URL, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.account import budget_ledger as ledger
from app.core.account import read_work
from app.core.account import reconciliation_stream as stream
from app.core.account.control_models import (
    AccountBudgetReservation,
    AccountBudgetSnapshot,
    ReconciliationTask,
)
from app.core.account.reserved_reads import RESERVED_BUDGET_LUA, reservation_args
from app.core.account.rpc_governor import RpcDeferred, limits_for
from app.core.config import settings
from app.core.database import Base
from tests.conftest import _import_models

pytest_plugins = ["tests.integration.test_growth_account_scheduler"]


@pytest_asyncio.fixture
async def control_sessions():
    _import_models()
    socket = os.getenv("GROWTH_TEST_PG_SOCKET")
    if not socket:
        pytest.skip("requires disposable PostgreSQL Unix socket")
    assert Path(socket).is_absolute() and Path(socket, ".s.PGSQL.5432").is_socket()
    url = URL.create("postgresql+asyncpg", username="postgres", database="postgres", query={"host": socket})
    schema = "p0_" + uuid.uuid4().hex
    admin = create_async_engine(url)
    engine = create_async_engine(url, pool_size=4, max_overflow=0,
                                 connect_args={"server_settings": {"search_path": schema}})
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        async with engine.begin() as conn:
            await conn.run_sync(lambda db: Base.metadata.create_all(db, tables=[
                AccountBudgetReservation.__table__, AccountBudgetSnapshot.__table__,
                ReconciliationTask.__table__,
            ]))
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


@pytest.mark.asyncio
async def test_pg_concurrent_reserve_and_duplicate_settlement(control_sessions):
    async def reserve():
        async with control_sessions.begin() as db:
            await ledger.reserve(db, "same-id", account_id=34, workload="review", lane="critical",
                                 reserved_reads=12, lease_expires_at=datetime.utcnow() + timedelta(minutes=5))

    await asyncio.gather(*(reserve() for _ in range(12)))
    async with control_sessions() as db:
        assert await db.scalar(select(func.count()).select_from(AccountBudgetReservation)) == 1

    async def settle():
        async with control_sessions.begin() as db:
            return await ledger.settle(db, "same-id", 3)

    assert sum(await asyncio.gather(*(settle() for _ in range(12)))) == 1
    async with control_sessions() as db:
        row = await db.get(AccountBudgetReservation, "same-id")
        assert row.consumed_reads == 3 and row.state == "settled"


@pytest.mark.asyncio
async def test_stream_eviction_duplicate_delivery_crash_reclaim_and_canary(control_sessions, queue_store, monkeypatch):
    monkeypatch.setattr(settings, "RECONCILIATION_STREAM_ENABLED", True)
    monkeypatch.setattr(settings, "RECONCILIATION_SHADOW_MODE", False)
    monkeypatch.setattr(settings, "RECONCILIATION_ACCOUNT_IDS", "34")
    async with control_sessions.begin() as db:
        await stream._insert_task(db, kind="ad", source_id=1, account_id=34, priority=10)
        await stream._insert_task(db, kind="ad", source_id=2, account_id=35, priority=10)
    async with control_sessions() as db:
        assert await stream.publish_pending(db, queue_store) == 1
    first = await stream.read_group(queue_store, consumer="dead", count=1, block_ms=1)
    assert len(first) == 1
    async with control_sessions() as db:
        row = await stream.claim_task(db, "ad:1:v1", "dead")
        row.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
        await db.commit()
    reclaimed = await stream.reclaim(queue_store, consumer="live", min_idle_ms=0)
    assert len(reclaimed) == 1 and reclaimed[0][0] == first[0][0]
    async with control_sessions() as db:
        claimed = await stream.claim_task(db, "ad:1:v1", "live")
        assert claimed is not None and claimed.attempts == 2
        assert not await stream.finish_task(db, "ad:1:v1", "dead", state="succeeded")
        assert await stream.finish_task(db, "ad:1:v1", "live", state="pending", retry_after=1)
        claimed.available_at = datetime.utcnow() - timedelta(seconds=1)
        await db.commit()
    # Lost Stream and consumer group do not remove the durable task.
    await queue_store.delete(stream.STREAM_KEY)
    async with control_sessions() as db:
        assert await stream.publish_pending(db, queue_store) == 1
    messages = await stream.read_group(queue_store, consumer="restored", count=2, block_ms=1)
    assert len(messages) == 1
    async with control_sessions() as db:
        assert await stream.claim_task(db, "ad:1:v1", "restored")
        assert await stream.finish_task(db, "ad:1:v1", "restored", state="succeeded")
        assert await stream.claim_task(db, "ad:1:v1", "duplicate") is None
        other = await db.scalar(select(ReconciliationTask).where(ReconciliationTask.source_id == 2))
        assert other.attempts == 0 and other.state == "pending"
    await queue_store.xack(stream.STREAM_KEY, stream.CONSUMER_GROUP, messages[0][0])
    assert (await queue_store.xpending(stream.STREAM_KEY, stream.CONSUMER_GROUP))["pending"] == 0


@pytest.mark.asyncio
async def test_ledger_journal_settles_real_lua_consumption_after_crash(control_sessions, queue_store, monkeypatch):
    monkeypatch.setattr(settings, "BUDGET_LEDGER_ENABLED", True)
    work = read_work.ReadWork(34, 2, "review", token="actual-lua")
    limits = limits_for({}, datetime.utcnow())
    keys, args = reservation_args(34, limits, "critical", 12, 0, workload="review",
                                 work_token=work.token, work_mode="reserve", work_group=2,
                                 work_kind="review")
    assert await queue_store.eval(RESERVED_BUDGET_LUA, len(keys), *keys, *args) == 0
    async with control_sessions() as db:
        await ledger.reserve_read_work(db, queue_store, work, 12)
    keys, args = reservation_args(34, limits, "critical", 3, 0, workload="review", work_token=work.token)
    assert await queue_store.eval(RESERVED_BUDGET_LUA, len(keys), *keys, *args) == 0
    assert await queue_store.get(ledger.CONSUMED_PREFIX + work.ledger_id) == "3"
    await queue_store.delete(read_work.key(34))  # process/lease gone, spent counter survives
    async with control_sessions() as db:
        row = await db.get(AccountBudgetReservation, work.ledger_id)
        row.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
        await db.commit()
        assert await ledger.expire_reservations(db, queue_store, datetime.utcnow()) == 1
        await db.commit()
        await db.refresh(row)
        assert row.state == "expired" and row.consumed_reads == 3
        assert await ledger.expire_reservations(db, queue_store, datetime.utcnow()) == 0
    assert await queue_store.get("vanguard:rpc:34:hour") == "3"


@pytest.mark.asyncio
async def test_pg_failure_releases_redis_hold_and_redis_failure_is_closed(queue_store, monkeypatch):
    monkeypatch.setattr(settings, "BUDGET_LEDGER_ENABLED", True)
    reserve = AsyncMock(side_effect=RuntimeError("database_down"))
    monkeypatch.setattr(ledger, "reserve", reserve)
    work = read_work.ReadWork(34, 2, "review")
    token = read_work._current.set(work)
    try:
        with pytest.raises(RpcDeferred, match="telegram_rpc_guard_unavailable"):
            await read_work.ensure(object(), 34, limits_for({}, datetime.utcnow()), queue_store)
        assert not await queue_store.exists(read_work.key(34))
        assert not await queue_store.exists("vanguard:rpc:34:hour")
        assert not work.claimed
        unavailable = AsyncMock()
        unavailable.lrange.side_effect = ConnectionError("redis_down")
        with pytest.raises(RpcDeferred, match="telegram_rpc_guard_unavailable"):
            await read_work.ensure(object(), 34, limits_for({}, datetime.utcnow()), unavailable)
        assert reserve.await_count == 1
    finally:
        read_work._current.reset(token)


@pytest.mark.asyncio
async def test_join_verification_fits_default_critical_share_without_precharging(control_sessions, queue_store, monkeypatch):
    monkeypatch.setattr(settings, "BUDGET_LEDGER_ENABLED", True)
    work = read_work.ReadWork(34, 2, "join_reconciliation")
    token = read_work._current.set(work)
    try:
        async with control_sessions() as db:
            await read_work.ensure(db, 34, limits_for({}, datetime.utcnow()), queue_store)
            assert work.claimed
            row = await db.get(AccountBudgetReservation, work.ledger_id)
            assert row.state == "reserved" and row.consumed_reads == 0
        assert not await queue_store.exists("vanguard:rpc:34:hour")
        # The read-only verifier gets its bounded slice while all send lanes
        # retain their original quotas; authorization does not precharge RPCs.
        keys, args = reservation_args(34, limits_for({}, datetime.utcnow()), "critical", 2, 0,
                                     workload="join", work_token=work.token)
        assert await queue_store.eval(RESERVED_BUDGET_LUA, len(keys), *keys, *args) == 0
        assert await queue_store.get("vanguard:rpc:34:hour") == "2"
    finally:
        read_work._current.reset(token)
