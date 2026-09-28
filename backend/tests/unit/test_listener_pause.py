"""Offline regression for pausing producers, retained cursors and budget reserves."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock, Mock

import pytest
from telethon import TelegramClient
from telethon.sessions import MemorySession
from telethon.tl.functions.updates import GetDifferenceRequest

from app.core.account import rpc_governor as rpc
from app.core.account.listener_pause import ListenerPause
from app.core.account.models import AccountStatus
from app.core.account.pool import AccountPool
from app.core.account.rpc_budget_policy import read_lane, window_plan

pytestmark = pytest.mark.asyncio


async def account_case():
    pool = AccountPool()
    wrapper = await pool.add_account(
        account_id=991,
        phone="+10000000991",
        session_name="offline-paused-listener",
        country_code="US",
        api_id="1",
        api_hash="offline-test-placeholder",
    )
    wrapper.keep_connected = True
    wrapper.status = AccountStatus.IDLE
    pool._assert_proxy_policy_current = AsyncMock()
    pool._ensure_proxy = AsyncMock()
    return pool, wrapper


async def test_sdk_native_wait_stops_producer_without_fatal_update_or_rpc():
    pool, wrapper = await account_case()
    client = TelegramClient(MemorySession(), 1, "offline-test-placeholder")
    connected = [True]
    client.is_connected = Mock(side_effect=lambda: connected[0])
    client._message_box = Mock(
        is_empty=Mock(return_value=False),
        get_difference=Mock(
            return_value=GetDifferenceRequest(pts=11, date=datetime.utcnow(), qts=1)
        ),
    )
    client._call = original = AsyncMock()
    rpc.install_governor(
        client,
        Obj(
            before=AsyncMock(side_effect=rpc.RpcDeferred("telegram_read_budget", 3600)),
            failed=AsyncMock(),
            succeeded=AsyncMock(),
        ),
    )
    wrapper.client = client
    client._vanguard_listener_pause = pause = ListenerPause(
        client, lambda: pool.pause_listener(wrapper, client)
    )
    checkpoint = Obj(pts=11)
    client.session.set_update_state(0, checkpoint)

    async def disconnect():
        connected[0] = False
        client._updates_handle.cancel()
        await client._updates_handle

    client.disconnect = AsyncMock(side_effect=disconnect)
    client._updates_queue.put_nowait(Obj())
    client._updates_handle = asyncio.create_task(client._update_loop())
    await asyncio.wait_for(client._updates_handle, 2)
    await pause.task
    original.assert_not_awaited()
    assert not connected[0] and client._updates_error is None
    assert wrapper.client is client and client.session.get_update_state(0) is checkpoint
    assert client._updates_queue.qsize() == 1
    assert pause.snapshot()["read_wait_seconds"] >= 0


async def test_pause_waits_for_inflight_business_and_handler_then_disconnects():
    pool, wrapper = await account_case()
    handler_done = asyncio.Event()
    handler = asyncio.create_task(handler_done.wait())
    updates = asyncio.create_task(asyncio.Event().wait())
    client = Obj(disconnect=AsyncMock(), _event_handler_tasks={handler}, _updates_handle=updates)
    wrapper.client = client
    wrapper.status = AccountStatus.WORKING
    task = asyncio.create_task(pool.pause_listener(wrapper, client))
    await asyncio.sleep(0.01)
    client.disconnect.assert_not_awaited()
    assert updates.cancelled()
    wrapper.status = AccountStatus.IDLE
    await asyncio.sleep(0.01)
    client.disconnect.assert_not_awaited()
    handler_done.set()
    await asyncio.wait_for(task, 2)
    client.disconnect.assert_awaited_once()
    assert wrapper.client is client


async def test_resume_reuses_client_session_and_handlers_after_deadline():
    pool, wrapper = await account_case()
    client = Obj(
        is_connected=Mock(return_value=False), connect=AsyncMock(), _event_handler_tasks=set()
    )
    wrapper.client = client
    pause = client._vanguard_listener_pause = ListenerPause(client, AsyncMock())
    pause.reason = "telegram_read_budget"
    pause.resume_at = datetime.utcnow() + timedelta(hours=1)
    pool._create_client = AsyncMock()
    with pytest.raises(rpc.RpcDeferred):
        await pool.connect_by_id(wrapper.account_id, require_session=False)
    client.connect.assert_not_awaited()
    pause.resume_at = datetime.utcnow() - timedelta(seconds=1)
    result = await pool.connect_by_id(wrapper.account_id, require_session=False)
    assert result.client is client and client._catch_up is True and pause.reason is None
    pool._create_client.assert_not_awaited()
    client.connect.assert_awaited_once()


async def test_business_acquire_cannot_replace_paused_listener_or_take_lease():
    pool, wrapper = await account_case()
    client = Obj()
    wrapper.client = client
    pause = client._vanguard_listener_pause = ListenerPause(client, AsyncMock())
    pause.reason = "telegram_read_budget"
    pause.resume_at = datetime.utcnow() - timedelta(seconds=1)
    pool._claim_operation_lease = AsyncMock()
    with pytest.raises(rpc.RpcDeferred, match="telegram_read_budget"):
        await pool.acquire_by_id(wrapper.account_id, require_session=False)
    pool._claim_operation_lease.assert_not_awaited()
    assert wrapper.client is client and wrapper.status == AccountStatus.IDLE


async def test_worker_retains_paused_session_and_checks_sync_before_connect(monkeypatch):
    from app.workers import telegram_worker as module

    @asynccontextmanager
    async def db():
        yield object()

    monkeypatch.setattr(module, "get_db_session", db)
    ready = AsyncMock(side_effect=rpc.RpcDeferred("telegram_read_budget", 300))
    monkeypatch.setattr(rpc, "check_read_ready", ready)
    worker = module.TelegramWorker(module.TelegramWorkerRole.GROWTH_USER)
    pool, wrapper = await account_case()
    worker._account_pool = pool
    client = Obj(
        is_connected=Mock(return_value=False),
        _updates_queue=asyncio.Queue(),
        _event_handler_tasks=set(),
    )
    wrapper.client = client
    pause = client._vanguard_listener_pause = ListenerPause(client, AsyncMock())
    pause.reason = "telegram_read_budget"
    pause.resume_at = datetime.utcnow() - timedelta(seconds=1)
    worker._growth_listener_sessions[wrapper.account_id] = wrapper.session_name
    pool.connect_by_id = AsyncMock()
    pool.set_offline = AsyncMock()
    result = await worker._ensure_growth_listeners(
        [Obj(id=wrapper.account_id, session_name=wrapper.session_name)]
    )
    assert result["active_listeners"] == 0 and result["listeners_deferred"] == 1
    assert result["listeners"][0]["connected"] is False
    pool.set_offline.assert_not_awaited()
    pool.connect_by_id.assert_not_awaited()
    assert ready.await_args.kwargs["requires_bootstrap"] is True
    assert wrapper.client is client


async def test_scans_and_critical_work_cannot_spend_listener_reserve():
    limits = rpc.limits_for({}, datetime.utcnow())
    plans = {
        lane: {key: cap for key, _, cap in window_plan(limits, lane)}
        for lane in ("background", "sync", "routine", "critical")
    }
    assert [plans[lane]["day"] for lane in plans] == [2400] * 4
    assert [plans[lane][lane + "_day"] for lane in plans] == [360, 1200, 240, 600]
    assert read_lane(["updates.GetStateRequest"], "group_qualification") == "routine"
    assert read_lane(["updates.GetDifferenceRequest"], "growth_listener") == "sync"
    assert plans["sync"]["sync_day"] == 1200


@pytest.mark.parametrize("age,expected_hours", [(3, 2), (24, 6), (72, 12)])
async def test_confirmed_old_approval_backs_off_original_record(age, expected_hours):
    from app.modules.acquisition.automation import AcquisitionAutomationService

    db = Obj(execute=AsyncMock(), commit=AsyncMock())
    service = AcquisitionAutomationService(db)
    now = datetime.utcnow()
    original = now - timedelta(hours=age)
    attempt = Obj(
        id=9,
        account_id=2,
        request_sent_at=original,
        attempted_at=original,
        reconciliation_failure_count=0,
        reconciliation_next_at=None,
        status="pending",
        request_state="sent",
        telegram_action_attempted=True,
    )
    await service._defer_join_reconciliation(attempt, now, "join_request_still_pending")
    assert attempt.reconciliation_next_at == now + timedelta(hours=expected_hours)
    assert attempt.attempted_at == original and attempt.request_sent_at == original
    assert attempt.status == "pending" and attempt.reconciliation_failure_count == 0


async def test_old_unknown_request_does_not_get_approval_backoff():
    from app.modules.acquisition.automation import AcquisitionAutomationService

    db = Obj(execute=AsyncMock(), commit=AsyncMock())
    service = AcquisitionAutomationService(db)
    now = datetime.utcnow()
    attempt = Obj(
        id=9,
        account_id=2,
        request_sent_at=now - timedelta(days=10),
        attempted_at=now - timedelta(days=10),
        reconciliation_failure_count=0,
        reconciliation_next_at=None,
    )
    await service._defer_join_reconciliation(
        attempt, now, "join_reconciliation_check_failed:TimeoutError", technical=True
    )
    assert attempt.reconciliation_next_at == now + timedelta(hours=2)
    assert attempt.reconciliation_failure_count == 1


async def test_resume_bootstrap_failure_closes_partially_open_transport():
    pool, wrapper = await account_case()
    client = Obj(
        is_connected=Mock(return_value=False),
        connect=AsyncMock(side_effect=rpc.RpcDeferred("telegram_read_budget", 600)),
        disconnect=AsyncMock(),
    )
    wrapper.client = client
    pause = client._vanguard_listener_pause = ListenerPause(client, AsyncMock())
    pause.reason = "telegram_read_budget"
    with pytest.raises(rpc.RpcDeferred):
        await pool.connect_by_id(wrapper.account_id, require_session=False)
    client.disconnect.assert_awaited_once()
    assert wrapper.client is client and pause.reason == "telegram_read_budget"
    assert wrapper.status != AccountStatus.ERROR


async def test_paused_client_is_not_selected_by_generic_pool():
    pool, wrapper = await account_case()
    client = Obj()
    wrapper.client = client
    client._vanguard_listener_pause = ListenerPause(client, AsyncMock())
    client._vanguard_listener_pause.reason = "telegram_read_budget"
    assert not wrapper.is_available
    assert pool._get_available_accounts(require_session=False) == []


async def test_proxy_rotation_retains_paused_client_and_its_session():
    pool, wrapper = await account_case()
    pool._ensure_proxy = AccountPool._ensure_proxy.__get__(pool, AccountPool)
    pool._proxy_required_for_account = Mock(return_value=True)
    pool._has_valid_proxy = Mock(return_value=False)
    proxy = Obj(protocol="socks5", host="offline.invalid", port=1080, username=None, password=None)
    pool._acquire_proxy = AsyncMock(return_value=proxy)
    client = Obj(is_connected=Mock(return_value=False), set_proxy=Mock())
    wrapper.client = client
    pause = client._vanguard_listener_pause = ListenerPause(client, AsyncMock())
    pause.reason = "telegram_read_budget"
    await pool._ensure_proxy(wrapper)
    assert wrapper.client is client and wrapper.current_proxy is proxy
    client.set_proxy.assert_called_once_with(("socks5", "offline.invalid", 1080, True, None, None))


async def test_joined_renewal_runs_before_older_unqualified_review(test_db, monkeypatch):
    from app.core.group.models import Group, GroupAccountMembership
    from app.modules.acquisition import qualification_service as module
    from app.modules.acquisition.models import GroupQualificationAudit
    from tests.unit.test_qualification_service import setup

    _, _, member, renewal = await setup(test_db)
    now = datetime.utcnow()
    renewal.next_retry_at = now - timedelta(minutes=1)
    group = Group(id=41, group_id=1234567891, title="unqualified")
    member2 = GroupAccountMembership(
        id=61,
        group_id=41,
        telegram_group_id=group.group_id,
        account_id=2,
        status="joined",
        joined_at=now - timedelta(days=3),
    )
    low = GroupQualificationAudit(
        batch_id="older-unqualified",
        membership_id=61,
        account_id=2,
        group_id=41,
        policy_version=renewal.policy_version,
        content_scope=renewal.content_scope,
        state="queued",
        decision="unknown",
        membership_joined_at=member2.joined_at,
        next_retry_at=now - timedelta(days=1),
    )
    test_db.add_all([group, member2, low])
    await test_db.commit()
    monkeypatch.setattr(module, "priority_account_ids", AsyncMock(return_value=set()))
    monkeypatch.setattr("app.core.account.read_schedule.check_read_ready", AsyncMock())
    picked = []

    async def assess(actor, account_id, target, *, row):
        picked.append(row.id)
        row.decision, row.state = "observe", "completed"
        row.next_retry_at = now + timedelta(hours=2)
        return Obj()

    monkeypatch.setattr(module, "assess", assess)
    result = await module.run_reviews(Obj(db=test_db), limit=1)
    assert result["processed"] == 1 and picked == [renewal.id]
    assert low.attempts == 0


async def test_fresh_heartbeat_reports_high_memory_backlog_and_pause_failure(test_db, monkeypatch):
    import json

    from app.core.runtime_health import operational_snapshot
    from app.core.worker_status import TelegramWorkerStatus

    test_db.add(
        TelegramWorkerStatus(
            worker_id="pressure-test",
            role="growth_user_worker",
            last_heartbeat_at=datetime.utcnow(),
            metadata_json=json.dumps(
                {
                    "runtime": {
                        "process_memory": {"rss_bytes": 3 * 1024**3},
                        "listeners": [
                            {
                                "account_id": 2,
                                "update_queue_size": 12,
                                "queue_nonempty_seconds": 121,
                                "pause_error": "OSError",
                            }
                        ],
                    }
                }
            ),
        )
    )
    await test_db.commit()
    monkeypatch.setattr(
        "app.core.redis.get_redis",
        AsyncMock(
            return_value=Obj(get=AsyncMock(return_value=None), info=AsyncMock(return_value={}))
        ),
    )
    result = await operational_snapshot(test_db)
    codes = {item["code"] for item in result["alerts"]}
    assert {"growth_memory_high", "listener_update_backlog", "listener_pause_failed"} <= codes
    assert result["workers"]["growth_user_worker"]["process_memory"]["rss_bytes"] == 3 * 1024**3


async def test_unknown_original_without_membership_keeps_short_cadence():
    from app.modules.acquisition.automation import AcquisitionAutomationService

    db = Obj(execute=AsyncMock(), commit=AsyncMock())
    now = datetime.utcnow()
    attempt = Obj(
        id=9,
        account_id=2,
        request_state="outcome_unknown",
        request_sent_at=now - timedelta(days=10),
        attempted_at=now - timedelta(days=10),
        reconciliation_failure_count=0,
        reconciliation_next_at=None,
    )
    await AcquisitionAutomationService(db)._defer_join_reconciliation(
        attempt, now, "join_request_still_pending"
    )
    assert attempt.reconciliation_next_at == now + timedelta(hours=2)
    assert attempt.request_state == "outcome_unknown"
