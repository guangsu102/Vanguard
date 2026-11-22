"""Real-session regression tests for original-request reconciliation boundaries."""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.account import read_schedule, read_work
from app.core.account.models import TelegramAccount
from app.core.account.rpc_governor import RpcDeferred
from app.core.database import Base
from app.core.database_transaction_boundary import TransactionBoundaryBlocked
from app.core.group.models import Group, GroupAccountMembership
from app.modules.acquisition import automation as module
from app.modules.acquisition.models import AutoJoinAttempt, GroupQualificationAudit
from tests.conftest import _import_models
from tests.unit.test_qualification_automation_lifecycle import case

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def one_connection_db(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'joins.db').as_posix()}",
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.25,
    )
    _import_models()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with AsyncSession(engine, expire_on_commit=False) as db:
        yield db
    await engine.dispose()


async def setup_read(db, monkeypatch, outcome=None, on_read=None):
    service, account, group, attempt, _, now = await case(db)
    monkeypatch.setattr(module, "_now", lambda: now)
    wrapper = Obj(client=Obj(is_connected=Mock(return_value=True)), operation_lease_lost=False)
    events = []

    async def available(label):
        assert not db.in_transaction(), label
        # A real checkout during each external wait proves the first session
        # returned its connection, including on single-connection engines.
        async with AsyncSession(db.bind) as other:
            assert await other.scalar(select(func.count(TelegramAccount.id))) == 1
        events.append(label)

    async def sync(accounts):
        await available("sync")
        assert accounts[0].id == account.id
        assert not hasattr(accounts[0], "_sa_instance_state")

    async def acquire(*_args, **_kwargs):
        await available("acquire")
        return wrapper

    async def release(value):
        assert value is wrapper
        await available("release")

    async def resolve(*_args):
        await available("resolve")
        if on_read:
            await on_read(service, account.id, group.id, attempt.id, now)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def ready(*_args, **_kwargs):
        assert not db.in_transaction(), "read_wait_entry"
        return None

    @asynccontextmanager
    async def operation(*_args, **_kwargs):
        await available("operation_enter")
        try:
            yield
        finally:
            await available("operation_exit")

    service._sync_account_pool = AsyncMock(side_effect=sync)
    service.account_pool.acquire_by_id = AsyncMock(side_effect=acquire)
    service.account_pool.release = AsyncMock(side_effect=release)
    service.telegram_execution = Obj(
        resolve_join_group_by_link_membership=AsyncMock(side_effect=resolve)
    )
    service._qualification_workflow_enabled = AsyncMock(return_value=False)
    service._dynamic_qualification_mode = AsyncMock(return_value=False)
    monkeypatch.setattr(read_schedule, "read_wait", AsyncMock(side_effect=ready))
    monkeypatch.setattr(read_work, "operation", operation)
    return service, account, group, attempt, now, events


@pytest.mark.parametrize(
    "outcome",
    [
        None,
        TimeoutError("read"),
        RpcDeferred("telegram_read_budget", 900),
        asyncio.CancelledError(),
        {"id": 900011, "title": "Confirmed"},
    ],
)
async def test_all_network_waits_release_real_single_connection(
    one_connection_db, monkeypatch, outcome
):
    db = one_connection_db
    service, _, _, attempt, now, events = await setup_read(db, monkeypatch, outcome)
    if isinstance(outcome, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await service.reconcile_join_requests()
    else:
        result = await service.reconcile_join_requests()
        assert result["checked"] == 1
        assert result["updated"] == int(isinstance(outcome, dict))
    assert not db.in_transaction()
    expected = (
        ["sync", "operation_enter", "acquire", "resolve", "release", "operation_exit"]
        if isinstance(outcome, BaseException)
        else ["sync", "operation_enter", "acquire", "resolve", "operation_exit", "release"]
    )
    assert events == expected
    service.account_pool.release.assert_awaited_once()
    await db.refresh(attempt)
    assert attempt.request_state == "sent"
    if isinstance(outcome, RpcDeferred):
        assert attempt.reconciliation_failure_count == 0
        assert attempt.reconciliation_next_at >= now + timedelta(seconds=900)


@pytest.mark.parametrize(
    "mutation", ["claim", "target", "confirmed", "released", "group", "membership", "rejoin"]
)
async def test_old_read_cannot_overwrite_replaced_claim_target_or_membership(
    test_db, monkeypatch, mutation
):
    changed = {}

    async def concurrent(_service, account_id, group_id, attempt_id, now):
        async with AsyncSession(test_db.bind, expire_on_commit=False) as other:
            if mutation == "claim":
                await other.execute(
                    update(AutoJoinAttempt)
                    .where(AutoJoinAttempt.id == attempt_id)
                    .values(reconciliation_next_at=now + timedelta(hours=3))
                )
            elif mutation == "target":
                await other.execute(
                    update(AutoJoinAttempt)
                    .where(AutoJoinAttempt.id == attempt_id)
                    .values(target_key="username:new_target")
                )
            elif mutation == "confirmed":
                changed["joined_at"] = now - timedelta(hours=2)
                await other.execute(
                    update(AutoJoinAttempt)
                    .where(AutoJoinAttempt.id == attempt_id)
                    .values(
                        status="success",
                        reconciliation_status="confirmed",
                        reconciliation_next_at=None,
                        joined_at=changed["joined_at"],
                        reason="newer_confirmation",
                    )
                )
            elif mutation == "group":
                await other.execute(
                    update(Group).where(Group.id == group_id).values(username="new_identity")
                )
            elif mutation == "released":
                await other.execute(
                    update(AutoJoinAttempt)
                    .where(AutoJoinAttempt.id == attempt_id)
                    .values(
                        status="failed",
                        request_state="released",
                        reconciliation_status="resolved",
                        reason="newer_resolution",
                        reconciliation_next_at=None,
                    )
                )
            else:
                changed["joined_at"] = now + timedelta(minutes=1)
                changed["deadline"] = now + timedelta(days=1, minutes=1)
                if mutation == "rejoin":
                    await other.execute(
                        update(GroupAccountMembership)
                        .where(
                            GroupAccountMembership.account_id == account_id,
                            GroupAccountMembership.group_id == group_id,
                        )
                        .values(
                            status="joined",
                            joined_at=changed["joined_at"],
                            review_started_at=changed["joined_at"],
                            review_deadline_at=changed["deadline"],
                        )
                    )
                else:
                    other.add(
                        GroupAccountMembership(
                            account_id=account_id,
                            group_id=group_id,
                            telegram_group_id=900011,
                            status="joined",
                            joined_at=changed["joined_at"],
                            review_started_at=changed["joined_at"],
                            review_deadline_at=changed["deadline"],
                        )
                    )
            await other.commit()

    service, account, group, attempt, now, _ = await setup_read(
        test_db, monkeypatch, {"id": 900011, "title": "Old result"}, concurrent
    )
    if mutation == "rejoin":
        test_db.add(
            GroupAccountMembership(
                account_id=account.id,
                group_id=group.id,
                telegram_group_id=900011,
                status="left",
                ad_status="blocked",
                joined_at=now - timedelta(days=2),
                left_at=now - timedelta(days=1),
            )
        )
        await test_db.commit()
    result = await service.reconcile_join_requests()
    assert result["updated"] == 0
    assert result["details"][0]["reason"] == "stale_join_reconciliation"
    assert not test_db.in_transaction()
    await test_db.refresh(attempt)
    assert attempt.reconciliation_checked_at is None
    assert attempt.reconciliation_failure_count == 0
    assert await test_db.scalar(select(func.count(GroupQualificationAudit.id))) == 0
    if mutation == "confirmed":
        assert attempt.status == "success" and attempt.reason == "newer_confirmation"
        assert attempt.joined_at == changed["joined_at"]
    elif mutation == "released":
        assert attempt.status == "failed" and attempt.request_state == "released"
        assert attempt.reason == "newer_resolution"
    elif mutation in {"membership", "rejoin"}:
        member = await test_db.scalar(select(GroupAccountMembership))
        assert member.joined_at == changed["joined_at"]
        assert member.review_deadline_at == changed["deadline"]
        assert attempt.status == "pending"


async def test_repeated_confirmation_keeps_original_membership_clock(test_db, monkeypatch):
    service, account, group, attempt, now, _ = await setup_read(
        test_db, monkeypatch, {"id": 900011}
    )
    joined_at, deadline = now - timedelta(hours=20), now + timedelta(hours=4)
    membership = GroupAccountMembership(
        account_id=account.id,
        group_id=group.id,
        telegram_group_id=900011,
        status="joined",
        joined_at=joined_at,
        review_started_at=joined_at,
        review_deadline_at=deadline,
        review_next_at=deadline,
    )
    test_db.add(membership)
    await test_db.commit()
    assert (await service.reconcile_join_requests())["updated"] == 1
    assert not test_db.in_transaction()
    await test_db.refresh(membership)
    await test_db.refresh(attempt)
    assert membership.joined_at == joined_at
    assert membership.review_started_at == joined_at
    assert membership.review_deadline_at == deadline
    assert membership.review_next_at == deadline
    assert attempt.joined_at == joined_at


async def test_flood_wait_merges_the_fresh_longer_pause(test_db, monkeypatch):
    class FloodWaitError(Exception):
        seconds = 120

    async def concurrent(_service, account_id, _group_id, _attempt_id, now):
        async with AsyncSession(test_db.bind) as other:
            await other.execute(
                update(TelegramAccount)
                .where(TelegramAccount.id == account_id)
                .values(risk_pause_until=now + timedelta(hours=2))
            )
            await other.commit()

    service, account, _, attempt, now, _ = await setup_read(
        test_db, monkeypatch, FloodWaitError(), concurrent
    )
    result = await service.reconcile_join_requests()
    assert result["updated"] == 0
    assert not test_db.in_transaction()
    await test_db.refresh(account)
    await test_db.refresh(attempt)
    assert account.risk_pause_until == now + timedelta(hours=2)
    assert attempt.reconciliation_failure_count == 1
    assert attempt.request_state == "sent"


@pytest.mark.parametrize("write", ["dirty", "orm_flushed", "core", "connection_core", "text"])
async def test_external_writes_are_neither_committed_nor_rolled_back(test_db, monkeypatch, write):
    service, account, _, _, _, _ = await setup_read(test_db, monkeypatch)
    account_id = account.id
    if write in {"dirty", "orm_flushed"}:
        account.display_name = "caller_write"
        if write == "orm_flushed":
            await test_db.flush()
    elif write == "text":
        await test_db.execute(
            text("UPDATE telegram_account SET display_name='caller_write' WHERE id=:id"),
            {"id": account_id},
        )
    elif write == "connection_core":
        connection = await test_db.connection()
        await connection.execute(
            update(TelegramAccount)
            .where(TelegramAccount.id == account_id)
            .values(display_name="caller_write")
        )
    else:
        await test_db.execute(
            update(TelegramAccount)
            .where(TelegramAccount.id == account_id)
            .values(display_name="caller_write")
        )
    with pytest.raises(TransactionBoundaryBlocked):
        await service.reconcile_join_requests()
    assert test_db.in_transaction()
    service._sync_account_pool.assert_not_awaited()
    service.account_pool.acquire_by_id.assert_not_awaited()
    assert (
        await test_db.scalar(
            select(TelegramAccount.display_name).where(TelegramAccount.id == account_id)
        )
        == "caller_write"
    )
    await test_db.rollback()
    assert (
        await test_db.scalar(
            select(TelegramAccount.display_name).where(TelegramAccount.id == account_id)
        )
        is None
    )


async def test_existing_read_snapshot_is_released_without_starving_entry(test_db, monkeypatch):
    service, account, _, _, _, _ = await setup_read(test_db, monkeypatch)
    await test_db.scalar(select(TelegramAccount.id).where(TelegramAccount.id == account.id))
    assert test_db.in_transaction()
    assert (await service.reconcile_join_requests())["checked"] == 1
    assert not test_db.in_transaction()
    service.account_pool.release.assert_awaited_once()


async def test_cancellation_during_local_apply_rolls_back_before_pool_release(test_db, monkeypatch):
    service, _, group, attempt, _, _ = await setup_read(test_db, monkeypatch, {"id": 900011})
    service._qualification_workflow_enabled = AsyncMock(return_value=True)

    async def cancel(group, _attempt, **_kwargs):
        group.title = "uncommitted"
        await test_db.flush()
        raise asyncio.CancelledError()

    service._persist_join_qualification_result = cancel
    with pytest.raises(asyncio.CancelledError):
        await service.reconcile_join_requests()
    assert not test_db.in_transaction()
    service.account_pool.release.assert_awaited_once()
    await test_db.refresh(group)
    await test_db.refresh(attempt)
    assert group.title == "Lifecycle"
    assert attempt.reconciliation_checked_at is None
    assert attempt.status == "pending" and attempt.request_state == "sent"


async def test_unknown_original_write_remains_fenced_after_pending_read(test_db, monkeypatch):
    service, _, _, attempt, now, _ = await setup_read(test_db, monkeypatch)
    attempt.request_state = "outcome_unknown"
    attempt.request_sent_at = now - timedelta(days=10)
    attempt.attempted_at = now - timedelta(days=10)
    await test_db.commit()
    assert (await service.reconcile_join_requests())["updated"] == 0
    await test_db.refresh(attempt)
    assert attempt.request_state == "outcome_unknown" and attempt.status == "pending"
    assert attempt.reconciliation_status == "pending"
    assert attempt.reservation_released_at is None
