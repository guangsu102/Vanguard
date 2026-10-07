import json
from datetime import datetime, timedelta

import pytest
from app.core.account.sync_lending import add_idle_sync_headroom

from app.core.account import rpc_governor as rpc
from app.core.worker_status import TelegramWorkerStatus
from tests.unit.test_ad_read_reserve import empty_usage


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "condition",
    ["idle", "stale", "pending", "sync_wait", "disconnected", "incomplete", "high_demand"],
)
async def test_only_fresh_idle_listener_lends_bounded_sync_capacity(test_db, condition):
    now = datetime.utcnow()
    item = {
        "account_id": 2,
        "state": "connected",
        "connected": True,
        "checkpoint_scope": "durable_session_and_inbox",
        "update_queue_size": 0,
        "update_handler_tasks": 0,
        "raw_journal": {"available": True, "states": {}, "queues": {}, "checkpoint": {"count": 1}},
    }
    if condition == "pending":
        item["raw_journal"]["queues"] = {"awaiting_checkpoint": {"count": 1}}
    elif condition == "sync_wait":
        item["sync_wait_reason"] = "telegram_read_budget"
    elif condition == "disconnected":
        item["connected"] = False
    elif condition == "incomplete":
        del item["checkpoint_scope"]
    test_db.add(
        TelegramWorkerStatus(
            worker_id="loan-test",
            role="growth_user_worker",
            status="online",
            last_heartbeat_at=now - timedelta(minutes=3) if condition == "stale" else now,
            metadata_json=json.dumps({"runtime": {"listeners": [item]}}),
        )
    )
    await test_db.commit()
    limits = rpc.limits_for({}, now)
    budget = {"usage": empty_usage(), "retry_after_seconds": 0, "lanes": {"critical": {}}}
    if condition == "high_demand":
        budget["usage"]["sync_hour"]["used"] = 20
        budget["usage"]["hour"]["used"] = 20
        budget["usage"]["hour"]["ttl_seconds"] = 3540
    await add_idle_sync_headroom(test_db, 2, now, limits, budget)
    if condition == "idle":
        assert budget["sync_lending"]["available"] == {"hour": 24, "day": 240}
        assert limits["sync_hold_hour"] == 24
        assert limits["ad_hour"] == 84 and limits["hour"] == 240
    elif condition == "high_demand":
        assert budget["sync_lending"]["available"]["hour"] == 0
    else:
        assert "sync_lending" not in budget
