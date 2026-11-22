"""Regression coverage for stale ledger readers and expired task owners."""

import importlib
import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.account import outbound_reconciliation as outbound
from app.core.account import reconciliation_handlers as handlers
from app.core.account import reconciliation_stream as stream
from app.core.account.control_models import ReconciliationTask
from app.core.account.models import (
    AccountOperationConfig,
    AccountOutboundAttempt,
    Proxy,
    TelegramAccount,
    TelegramAPIConfig,
)
from app.core.config import settings
from app.core.database import Base


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def ownership_sessions(request):
    """Use independent sessions, optionally against disposable PostgreSQL."""
    # Release-image tests run without the repository conftest, including on an
    # older production baseline that does not contain the P4/P5 model modules.
    for module in (
        "app.core.account.models", "app.core.group.models", "app.core.keyword.models",
        "app.core.user.models", "app.core.campaign.models", "app.core.worker_status",
        "app.modules.acquisition.models", "app.modules.guardian.models",
        "app.modules.private_chat.models", "app.modules.qq.models",
        "app.modules.owned_group.models", "app.modules.owned_group.models_extra",
        "app.modules.owned_group.messaging_models", "app.integrations.xboard.models",
    ):
        importlib.import_module(module)
    schema = None
    admin = None
    if request.param == "postgres":
        url = os.getenv("VANGUARD_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("isolated PostgreSQL URL not configured")
        schema = "reconciliation_owner_" + uuid.uuid4().hex
        admin = create_async_engine(url)
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(
            url, pool_size=4, max_overflow=0,
            connect_args={"server_settings": {"search_path": schema}},
        )
    else:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(lambda connection: Base.metadata.create_all(
                connection, tables=[
                    Proxy.__table__, TelegramAPIConfig.__table__, TelegramAccount.__table__,
                    AccountOperationConfig.__table__, AccountOutboundAttempt.__table__,
                    ReconciliationTask.__table__,
                ],
            ))
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        if admin is not None:
            async with admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await admin.dispose()


@pytest.fixture
def enabled_reconciliation(monkeypatch):
    monkeypatch.setattr(settings, "RECONCILIATION_STREAM_ENABLED", True)
    monkeypatch.setattr(settings, "RECONCILIATION_SHADOW_MODE", False)
    monkeypatch.setattr(settings, "RECONCILIATION_ACCOUNT_IDS", "*")


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal,message_id", [("succeeded", 100), ("failed", None)])
async def test_stale_ledger_reader_preserves_another_sessions_terminal_result(
    ownership_sessions, monkeypatch, terminal, message_id,
):
    recover = AsyncMock()
    monkeypatch.setattr(outbound, "recover_missing_receipts", recover)
    now = datetime.utcnow()
    async with ownership_sessions() as stale_session:
        account = TelegramAccount(id=2, identifier="ownership-test", session_name="ownership-test")
        stale = AccountOutboundAttempt(
            account_id=2, attempt_key="ad:old-send", category="ad", target_key="-1001234567890",
            state="unknown", message_id=None, attempted_at=now - timedelta(days=2),
            created_at=now - timedelta(days=2),
        )
        stale_session.add(account)
        await stale_session.flush()
        stale_session.add(stale)
        await stale_session.commit()
        # Load NULL fields too: otherwise a later SELECT can populate unloaded
        # fields and accidentally hide the stale identity-map regression.
        await stale_session.refresh(stale)
        async with ownership_sessions() as current_session:
            current = await current_session.get(AccountOutboundAttempt, stale.id)
            current.state, current.message_id = terminal, message_id
            await current_session.commit()
        assert stale.state == "unknown" and stale.message_id is None
        log = Obj(
            id=1, account_id=2, reservation_token="old-send", telegram_group_id=-1001234567890,
            telegram_message_id=99, sent_at=None,
        )
        assert await outbound.reconcile_ad_ledger(stale_session, log, now=now) is None
        assert stale.state == terminal and stale.message_id == message_id
        await stale_session.commit()
        async with ownership_sessions() as observer:
            current = await observer.get(AccountOutboundAttempt, stale.id)
            assert current.state == terminal and current.message_id == message_id
    recover.assert_not_awaited()


async def new_task(db):
    row = ReconciliationTask(
        task_key="ad:1:v1", kind="ad", source_id=1, account_id=2,
        available_at=datetime.utcnow() - timedelta(seconds=1),
        evidence_json='{"owner":"initial"}', result_json='{"state":"pending"}',
        reason="initial",
    )
    db.add(row)
    await db.commit()
    return row


@pytest.mark.asyncio
async def test_expired_executor_cannot_replace_successors_evidence(
    ownership_sessions, enabled_reconciliation, monkeypatch,
):
    async with ownership_sessions() as stale_session:
        task = await new_task(stale_session)
        assert await stream.claim_task(stale_session, task.task_key, "old")
        successor = {"state": "succeeded", "evidence": {"owner": "new"}, "observed": True}

        async def complete_successor(*args, **kwargs):
            async with ownership_sessions() as current_session:
                current = await current_session.get(ReconciliationTask, task.id)
                current.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
                await current_session.commit()
                assert await stream.claim_task(current_session, task.task_key, "new")
                assert await stream.finish_task(
                    current_session, task.task_key, "new", state="succeeded", result=successor,
                )
            return {
                "state": "manual_required", "error": "stale_evidence",
                "evidence": {"owner": "old"},
            }

        monkeypatch.setattr(handlers, "_execute", complete_successor)
        stale_result = await handlers.execute_task(
            stale_session, task.id, "old", 2, account_pool=Obj(),
        )
        # execute_task must not commit evidence before the final owner CAS.
        async with ownership_sessions() as observer:
            current = await observer.get(ReconciliationTask, task.id)
            assert current.state == "succeeded"
            assert json.loads(current.evidence_json) == successor["evidence"]
        assert not await stream.finish_task(
            stale_session, task.task_key, "old", state=stale_result["state"],
            error_code=stale_result["error"], result=stale_result,
        )
        async with ownership_sessions() as observer:
            current = await observer.get(ReconciliationTask, task.id)
            assert current.state == "succeeded" and current.reason is None
            assert json.loads(current.evidence_json) == successor["evidence"]
            assert json.loads(current.result_json) == {"state": "succeeded", "observed": True}


@pytest.mark.asyncio
async def test_expired_owner_cannot_submit_evidence_even_without_successor(ownership_sessions):
    async with ownership_sessions() as db:
        task = await new_task(db)
        assert await stream.claim_task(db, task.task_key, "expired")
        task.lease_expires_at = datetime.utcnow() - timedelta(seconds=1)
        await db.commit()
        assert not await stream.finish_task(
            db, task.task_key, "expired", state="manual_required", error_code="late",
            result={"state": "manual_required", "error": "late", "evidence": {"late": True}},
        )
        await db.refresh(task)
        assert task.state == "leased" and task.reason == "initial"
        assert json.loads(task.evidence_json) == {"owner": "initial"}
        assert json.loads(task.result_json) == {"state": "pending"}


@pytest.mark.asyncio
async def test_current_owner_commits_retry_and_evidence_together(ownership_sessions):
    async with ownership_sessions() as db:
        task = await new_task(db)
        assert await stream.claim_task(db, task.task_key, "current")
        result = {
            "state": "pending", "error": "waiting_evidence", "retry_after": 120,
            "evidence": {"checked": True},
        }
        assert await stream.finish_task(
            db, task.task_key, "current", state="pending", error_code=result["error"],
            retry_after=result["retry_after"], result=result,
        )
        await db.refresh(task)
        assert task.state == "pending" and task.lease_owner is None
        assert task.available_at > datetime.utcnow() + timedelta(seconds=110)
        assert task.last_error_code == task.reason == result["error"]
        assert json.loads(task.evidence_json) == result["evidence"]
        assert json.loads(task.result_json) == {
            key: value for key, value in result.items() if key != "evidence"
        }


@pytest.mark.asyncio
async def test_worker_passes_evidence_to_the_final_owner_cas(monkeypatch):
    from app.core import reconciliation_worker as worker

    @asynccontextmanager
    async def session():
        yield Obj()

    outcome = {"state": "pending", "error": "waiting", "evidence": {"checked": True}}
    task = Obj(id=1, account_id=2)
    finish = AsyncMock(return_value=True)
    cache = Obj(xack=AsyncMock())
    monkeypatch.setattr(worker, "get_db_session", session)
    monkeypatch.setattr(worker.stream, "claim_task", AsyncMock(return_value=task))
    monkeypatch.setattr(worker.stream, "active_for", lambda _: True)
    monkeypatch.setattr(worker.settings, "RECONCILIATION_SHADOW_MODE", False)
    monkeypatch.setattr(worker, "process", AsyncMock(return_value=outcome))
    monkeypatch.setattr(worker.stream, "finish_task", finish)
    assert await worker.consume_messages(cache, [("1-0", {"task_key": "ad:1:v1"})], "test") == 1
    assert finish.await_args.kwargs["result"] is outcome
    cache.xack.assert_awaited_once()


def bind_worker_sessions(worker, sessions, monkeypatch):
    calls = Obj(count=0)

    @asynccontextmanager
    async def session():
        calls.count += 1
        async with sessions() as db:
            yield db
            await db.commit()

    monkeypatch.setattr(worker, "get_db_session", session)
    return calls


@pytest.mark.asyncio
async def test_batch_retires_old_wakeups_and_claims_live_tasks_sequentially(
    ownership_sessions, enabled_reconciliation, monkeypatch,
):
    from app.core import metrics
    from app.core import reconciliation_worker as worker

    monkeypatch.setattr(settings, "RECONCILIATION_ACCOUNT_IDS", "2")
    now = datetime.utcnow()
    past, future = now - timedelta(minutes=5), now + timedelta(minutes=5)
    async with ownership_sessions() as db:
        for source_id, state, available, expiry, account in [
            (1, "pending", past, None, 2),
            (2, "leased", past, past, 2),
            (3, "pending", future, None, 2),
            (4, "leased", past, future, 2),
            (5, "succeeded", past, None, 2),
            (6, "pending", past, None, 3),
            (7, "leased", past, None, 2),
        ]:
            db.add(ReconciliationTask(
                task_key=f"ad:{source_id}:v1", kind="ad", source_id=source_id,
                account_id=account, state=state, available_at=available,
                lease_owner="previous" if state == "leased" else None,
                lease_expires_at=expiry,
            ))
        await db.commit()
    stale = [(f"{number}-0", {"task_key": "ad:5:v1"}) for number in range(192)]
    stale += [
        ("future-0", {"task_key": "ad:3:v1"}),
        ("leased-0", {"task_key": "ad:4:v1"}),
        ("rollout-0", {"task_key": "ad:6:v1"}),
        ("missing-0", {"task_key": "ad:99:v1"}),
        ("malformed-0", {}),
    ]
    live = [(f"live-{key}", {"task_key": f"ad:{key}:v1"}) for key in (1, 2, 7)]
    sessions_used = bind_worker_sessions(worker, ownership_sessions, monkeypatch)
    real_claim = stream.claim_task
    events = []

    async def claim(db, key, owner, **kwargs):
        events.append(("claim", key))
        return await real_claim(db, key, owner, **kwargs)

    async def process(task, owner):
        events.append(("process", task.task_key))
        return {"state": "succeeded", "evidence": {"task_key": task.task_key}}

    claim_spy = AsyncMock(side_effect=claim)
    monkeypatch.setattr(worker.stream, "claim_task", claim_spy)
    monkeypatch.setattr(worker, "process", process)
    monkeypatch.setattr(metrics, "record_reconciliation", AsyncMock())
    cache = Obj(xack=AsyncMock())
    assert await worker.consume_messages(cache, stale + live, "batch") == 3
    assert cache.xack.await_args_list[0].args == (
        stream.STREAM_KEY, stream.CONSUMER_GROUP, *(message_id for message_id, _ in stale)
    )
    assert cache.xack.await_count == 4 and claim_spy.await_count == 3
    assert sessions_used.count == 7  # One read, then one claim + finish per live task.
    assert events == [
        (stage, f"ad:{key}:v1") for key in (1, 2, 7) for stage in ("claim", "process")
    ]
    async with ownership_sessions() as db:
        for source_id in (1, 2, 7):
            row = await db.get(ReconciliationTask, source_id)
            assert row.state == "succeeded" and row.attempts == 1
        assert (await db.get(ReconciliationTask, 3)).state == "pending"
        assert (await db.get(ReconciliationTask, 4)).lease_owner == "previous"
        assert (await db.get(ReconciliationTask, 6)).attempts == 0


@pytest.mark.asyncio
async def test_batch_prefilter_does_not_authorize_a_claim_after_another_owner_wins(
    ownership_sessions, enabled_reconciliation, monkeypatch,
):
    from app.core import reconciliation_worker as worker

    async with ownership_sessions() as db:
        task = await new_task(db)
        task_id, task_key = task.id, task.task_key
    bind_worker_sessions(worker, ownership_sessions, monkeypatch)
    real_prefilter = worker._claimable_task_keys

    async def another_owner_wins(keys):
        candidates = await real_prefilter(keys)
        async with ownership_sessions() as db:
            row = await db.get(ReconciliationTask, task_id)
            row.state, row.lease_owner = "leased", "another-owner"
            row.lease_expires_at = datetime.utcnow() + timedelta(minutes=5)
            await db.commit()
        return candidates

    process = AsyncMock()
    monkeypatch.setattr(worker, "_claimable_task_keys", another_owner_wins)
    monkeypatch.setattr(worker, "process", process)
    cache = Obj(xack=AsyncMock())
    assert await worker.consume_messages(
        cache, [("live-0", {"task_key": task_key}), ("old-0", {})], "loser",
    ) == 0
    process.assert_not_awaited()
    assert cache.xack.await_count == 2
    async with ownership_sessions() as db:
        row = await db.get(ReconciliationTask, task_id)
        assert row.state == "leased" and row.lease_owner == "another-owner"
        assert row.attempts == 0
