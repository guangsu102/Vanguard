import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.account.budget_ledger import reserve, settle
from app.core.account.control_models import ReconciliationTask
from app.core.account.reconciliation_stream import claim_task, finish_task, task_key
from app.core.metrics import render


@pytest.mark.asyncio
async def test_budget_reservation_is_idempotent_and_settlement_is_cas(test_db):
    expires = datetime.utcnow() + timedelta(minutes=5)
    first = await reserve(
        test_db,
        "read:test-1",
        account_id=34,
        workload="review",
        lane="critical",
        reserved_reads=8,
        lease_expires_at=expires,
    )
    second = await reserve(
        test_db,
        "read:test-1",
        account_id=34,
        workload="review",
        lane="critical",
        reserved_reads=8,
        lease_expires_at=expires,
    )
    assert first.reservation_id == second.reservation_id
    assert await settle(test_db, "read:test-1", 3)
    await test_db.commit()
    assert not await settle(test_db, "read:test-1", 3)
    row = await test_db.scalar(
        select(type(first)).where(type(first).reservation_id == "read:test-1")
    )
    assert row.state == "settled" and row.consumed_reads == 3


@pytest.mark.asyncio
async def test_reconciliation_claim_and_finish_are_owner_cas(test_db):
    task = ReconciliationTask(
        task_key=task_key("ad", 99),
        kind="ad",
        source_id=99,
        account_id=34,
        available_at=datetime.utcnow() - timedelta(seconds=1),
    )
    test_db.add(task)
    await test_db.commit()

    claimed = await claim_task(test_db, task.task_key, "consumer-a")
    assert claimed is not None and claimed.attempts == 1
    assert await finish_task(test_db, task.task_key, "consumer-b", state="succeeded") is False
    assert await finish_task(test_db, task.task_key, "consumer-a", state="succeeded") is True
    assert await finish_task(test_db, task.task_key, "consumer-a", state="succeeded") is False


class _MetricsCache:
    def __init__(self):
        self.counters = {
            "vanguard_join_attempts|sent|none": "2",
            "vanguard_join_attempts|secret-account-id|none": "100",
        }

    async def hgetall(self, _key):
        return self.counters

    async def get(self, _key):
        return None


@pytest.mark.asyncio
async def test_metrics_exposition_rejects_unlisted_labels():
    cache = _MetricsCache()
    cache.get = AsyncMock(return_value=json.dumps({"gauges": [
        {"name": "vanguard_queue_lag_seconds", "labels": ["private-account-34"], "value": 7},
        {"name": "vanguard_queue_lag_seconds", "labels": ["inbox"], "value": 4},
    ]}))
    payload = (await render(cache)).decode()
    assert "vanguard_join_attempts_total{reason_class=\"none\",status=\"sent\"} 2.0" in payload
    assert "secret-account-id" not in payload
    assert "private-account-34" not in payload
    assert 'vanguard_queue_lag_seconds{queue="inbox"} 4.0' in payload


def test_reconciliation_task_key_is_stable():
    assert task_key("join", 7) == "join:7:v1"


@pytest.mark.asyncio
async def test_reconciliation_context_rejects_writes_before_governor_or_rpc():
    from telethon.tl.functions.messages import SendMessageRequest

    from app.core.account.reconciliation_handlers import verification_only
    from app.core.account.rpc_governor import install_governor

    original = AsyncMock()
    client = SimpleNamespace(_call=original)
    governor = SimpleNamespace(before=AsyncMock(), failed=AsyncMock(), succeeded=AsyncMock())
    install_governor(client, governor)
    token = verification_only.set(True)
    try:
        with pytest.raises(RuntimeError, match="reconciliation_write_forbidden"):
            await client._call(None, SendMessageRequest("unused", "never sent"))
    finally:
        verification_only.reset(token)
    original.assert_not_awaited()
    governor.before.assert_not_awaited()


@pytest.mark.asyncio
async def test_lost_database_finish_never_acknowledges_stream(monkeypatch):
    from contextlib import asynccontextmanager

    from app.core import reconciliation_worker as worker

    @asynccontextmanager
    async def session():
        yield object()

    task = SimpleNamespace(id=5, account_id=34, created_at=datetime.utcnow())
    cache = SimpleNamespace(xack=AsyncMock())
    monkeypatch.setattr(worker, "get_db_session", session)
    monkeypatch.setattr(worker.stream, "claim_task", AsyncMock(return_value=task))
    monkeypatch.setattr(worker.stream, "active_for", lambda _: True)
    monkeypatch.setattr(worker.settings, "RECONCILIATION_SHADOW_MODE", False)
    monkeypatch.setattr(worker, "process", AsyncMock(return_value={"state": "succeeded"}))
    monkeypatch.setattr(worker.stream, "finish_task", AsyncMock(return_value=False))
    assert await worker.consume_messages(cache, [("1-0", {"task_key": "ad:5:v1"})], "test") == 0
    cache.xack.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_does_not_reset_unfinished_task_age(test_db):
    started = datetime.utcnow() - timedelta(minutes=40)
    row = ReconciliationTask(task_key="join:6:v1", kind="join", source_id=6,
                             account_id=34, created_at=started, available_at=started,
                             verification_due_at=started)
    test_db.add(row)
    await test_db.commit()
    assert await claim_task(test_db, row.task_key, "owner")
    assert await finish_task(test_db, row.task_key, "owner", state="pending", retry_after=300)
    await test_db.refresh(row)
    assert row.created_at == started
    assert row.verification_due_at == started
    assert row.available_at > datetime.utcnow()


@pytest.mark.asyncio
async def test_known_approval_wait_moves_only_the_verification_cycle(test_db, monkeypatch):
    from app.core.config import settings
    from app.core.metrics import aggregate

    monkeypatch.setattr(settings, "RECONCILIATION_STREAM_ENABLED", True)
    monkeypatch.setattr(settings, "RECONCILIATION_SHADOW_MODE", False)
    monkeypatch.setattr(settings, "RECONCILIATION_ACCOUNT_IDS", "34")
    started = datetime.utcnow() - timedelta(hours=2)
    next_at = datetime.utcnow() + timedelta(hours=6)
    row = ReconciliationTask(task_key="join:7:v1", kind="join", source_id=7,
                             account_id=34, created_at=started, available_at=started,
                             verification_due_at=started)
    test_db.add(row)
    await test_db.commit()
    assert await claim_task(test_db, row.task_key, "owner")
    assert await finish_task(test_db, row.task_key, "owner", state="pending",
                             next_verification_at=next_at)
    await test_db.refresh(row)
    assert row.created_at == started and row.verification_due_at == next_at
    cache = SimpleNamespace(get=AsyncMock(return_value=None), zrange=AsyncMock(return_value=[]),
                            set=AsyncMock())
    result = await aggregate(test_db, cache)
    assert result["reconciliation"]["oldest_unfinished_seconds"]["join"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("request_state,scheduled", [("sent", True), ("outcome_unknown", False)])
async def test_only_confirmed_sent_approval_wait_can_schedule_a_new_cycle(
    test_db, request_state, scheduled
):
    from app.core.account.reconciliation_handlers import _execute
    from app.modules.acquisition.models import AutoJoinAttempt

    next_at = datetime.utcnow() + timedelta(hours=6)
    source = AutoJoinAttempt(account_id=34, target_key="username:unused", status="pending",
                             request_state=request_state, reason="join_request_still_pending",
                             reconciliation_next_at=next_at)
    test_db.add(source)
    await test_db.commit()
    outcome = await _execute(test_db, "join", source.id, 34, account_pool=None)
    assert outcome["state"] == "pending" and not outcome.get("observed")
    assert ("next_verification_at" in outcome) is scheduled


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [
    {"result": {"checked": 1, "updated": 1, "details": []}}, None,
    {"error": "execution_failed"},
])
async def test_group_metadata_forwards_exact_target_without_local_session(test_db, monkeypatch, reply):
    from app.core import redis
    from app.core.account import session_exec
    from app.core.group.collection import sync_stale_groups
    from app.modules.acquisition import ad_output_plan
    from tests.unit.test_dynamic_outbound_capacity import account_config, add_member

    account, config = await account_config(test_db)
    config.auto_join_enabled = False
    member, _ = await add_member(test_db, account.id, 81, decision="allowed", review="approved")
    monkeypatch.setattr(ad_output_plan, "ad_output_plan", AsyncMock(
        return_value={"group_deficit": 0, "join_blocker": None}))
    monkeypatch.setattr(redis, "get_redis", AsyncMock(return_value=object()))
    monkeypatch.setattr(session_exec, "owner_id", AsyncMock(return_value="listener-owner"))
    forwarded = AsyncMock(return_value=reply)
    monkeypatch.setattr(session_exec, "forward", forwarded)
    pool = SimpleNamespace(sync_from_db=AsyncMock(), acquire_by_id=AsyncMock())
    result = await sync_stale_groups(test_db, pool=pool, group_id=member.group_id)
    forwarded.assert_awaited_once_with(account.id, f"group_metadata:{member.group_id}")
    pool.sync_from_db.assert_not_awaited()
    pool.acquire_by_id.assert_not_awaited()
    assert result["updated"] == (1 if reply and "result" in reply else 0)
    if not reply or "error" in reply:
        assert result["details"][0]["reason"] == "metadata_session_owner_unavailable"


@pytest.mark.asyncio
async def test_canary_latency_ignores_other_accounts_shadow_inventory(test_db, monkeypatch):
    from app.core.config import settings
    from app.core.metrics import aggregate

    monkeypatch.setattr(settings, "RECONCILIATION_STREAM_ENABLED", True)
    monkeypatch.setattr(settings, "RECONCILIATION_SHADOW_MODE", False)
    monkeypatch.setattr(settings, "RECONCILIATION_ACCOUNT_IDS", "34")
    now = datetime.utcnow()
    for account_id, seconds in ((34, 60), (35, 7200)):
        at = now - timedelta(seconds=seconds)
        test_db.add(ReconciliationTask(task_key=f"join:{account_id}:v1", kind="join",
                    source_id=account_id, account_id=account_id, created_at=at, available_at=at))
    await test_db.commit()
    cache = SimpleNamespace(get=AsyncMock(return_value=None), zrange=AsyncMock(return_value=[]),
                            set=AsyncMock())
    result = await aggregate(test_db, cache)
    assert 60 <= result["reconciliation"]["oldest_unfinished_seconds"]["join"] < 120
    monkeypatch.setattr(settings, "RECONCILIATION_ACCOUNT_IDS", "*")
    result = await aggregate(test_db, cache)
    assert result["reconciliation"]["oldest_unfinished_seconds"]["join"] >= 7200
