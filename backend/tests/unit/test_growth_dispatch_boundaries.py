import asyncio
from datetime import datetime
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.account import event_inbox
from app.core.worker_status import TelegramEventInbox
from tests.integration.test_growth_account_scheduler import add_accounts

pytestmark = pytest.mark.asyncio


async def test_inbox_round_robin_cannot_be_dominated_by_one_busy_account(test_db):
    await add_accounts(test_db)
    for account in range(1, 31):
        for event in range(100 if account == 1 else 1):
            test_db.add(TelegramEventInbox(account_id=account, event_key=f"{account}:{event}",
                kind="listener_fact", chat_id=account, payload_json="{}", state="pending",
                next_attempt_at=datetime.utcnow()))
    await test_db.commit()
    seen, cursor = [], 0
    for _ in range(30):
        row = await event_inbox.claim(test_db, list(range(1, 31)), after_account_id=cursor,
                                      local_only=True)
        seen.append(row.account_id)
        cursor = row.account_id
        await event_inbox.finish(test_db, row.id)
        await test_db.commit()
    assert seen == list(range(1, 31))


async def test_local_fact_pump_continues_while_business_handler_is_blocked():
    from app.workers.telegram_worker import TelegramWorker
    worker = object.__new__(TelegramWorker)
    worker._running = True
    worker._project_growth_listener_facts = AsyncMock()
    business_started = asyncio.Event()
    fact_finished = asyncio.Event()
    never = asyncio.Event()
    async def process(*, local_only):
        if not local_only:
            business_started.set()
            await never.wait()
        else:
            await business_started.wait()
            fact_finished.set()
        return False
    worker._process_growth_inbox_once = process
    task = asyncio.create_task(worker._growth_inbox_loop())
    try:
        await asyncio.wait_for(fact_finished.wait(), timeout=1)
    finally:
        worker._running = False
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_periodic_wakeup_is_coalesced_before_broker_publish(monkeypatch):
    from app.core.scheduler import bounded_beat
    from celery.beat import PersistentScheduler
    from app.core.scheduler.growth_dispatch import TICK_TASK
    depth = 0
    def llen(queue):
        return depth if queue == "growth_dispatch" else 0
    broker = Mock(llen=llen)
    broker.__enter__ = Mock(return_value=broker)
    broker.__exit__ = Mock()
    monkeypatch.setattr(bounded_beat.Redis, "from_url", Mock(return_value=broker))
    def publish(*args, **kwargs):
        nonlocal depth
        depth += 1
    monkeypatch.setattr(PersistentScheduler, "apply_async", publish)
    beat = object.__new__(bounded_beat.BoundedGrowthScheduler)
    beat.reserve = Mock()
    entry = Obj(task=TICK_TASK)
    for _ in range(10000):
        beat.apply_async(entry)
    assert depth == 1
    assert beat.reserve.call_count == 9999
    depth = 0
    beat.apply_async(entry)
    assert depth == 1


async def test_each_execution_stage_preserves_account_scope(test_db, monkeypatch):
    from app.core.scheduler import growth_dispatch as scheduler
    from app.core import automation_settings
    from app.modules.acquisition import automation, qualification_service
    await add_accounts(test_db, 1)
    actor = Obj(run_auto_join=AsyncMock(return_value={}), run_ad_delivery=AsyncMock(return_value={}))
    monkeypatch.setattr(automation, "AcquisitionAutomationService", lambda db, account_pool=None: actor)
    monkeypatch.setattr(automation_settings, "get_auto_join_scheduler_settings", AsyncMock(return_value={
        "enabled": True, "scan_interval_minutes": 5}))
    monkeypatch.setattr(automation_settings, "get_ad_delivery_execution_settings", AsyncMock(return_value={
        "enabled": True, "dispatcher_interval_seconds": 60}))
    await scheduler.execute(test_db, "join", 1)
    await scheduler.execute(test_db, "ads", 1)
    assert actor.run_auto_join.await_args.kwargs["account_id"] == 1
    assert actor.run_ad_delivery.await_args.kwargs == {"account_id": 1, "max_deliveries": 1}
    monkeypatch.setattr(automation_settings, "get_auto_join_scheduler_settings", AsyncMock(return_value={"enabled": False}))
    result, _ = await scheduler.execute(test_db, "join", 1)
    assert result["reason"] == "scheduler_disabled"
    assert actor.run_auto_join.await_count == 1
