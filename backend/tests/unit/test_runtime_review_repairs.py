"""Regression evidence for budget scheduling, metrics, and capacity projections."""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select, update

from app.core.account.models import AccountOperationConfig, TelegramAccount
from app.core.account.read_schedule import read_wait
from app.core.account.rpc_governor import RpcDeferred
from app.core.settings_models import SystemSetting
from app.modules.acquisition.automation import AcquisitionAutomationService
from app.modules.acquisition.capacity_reads import CapacityReads
from app.modules.acquisition.delivery_metrics import pre_request_deferred
from app.modules.acquisition.dynamic_frequency import AccountDynamicFrequencyService
from app.modules.acquisition.models import AdCampaign, AdDeliveryLog, AutoJoinAttempt


@pytest.mark.parametrize(
    "error,message_id,expected",
    [
        ("unknown:telegram_read_budget: retry_after_seconds=3600", None, True),
        ("telegram_read_budget: retry_after_seconds=3600", 123, False),
        ("send_outcome_unknown:telegram_read_budget: retry_after_seconds=3600", None, False),
        ("FLOOD_WAIT_300", None, False),
        (None, None, False),
    ],
)
def test_only_unattempted_local_waits_are_excluded(error, message_id, expected):
    assert pre_request_deferred(error, message_id) is expected


async def test_delivery_health_excludes_local_wait_but_retains_real_failure(test_db):
    now = datetime.utcnow()
    account = TelegramAccount(identifier="metrics", session_name="metrics")
    campaign = AdCampaign(name="metrics")
    test_db.add_all([account, campaign])
    await test_db.flush()
    test_db.add_all(
        [
            AdDeliveryLog(
                account_id=account.id,
                ad_campaign_id=campaign.id,
                telegram_group_id=123,
                status=status,
                error=error,
                telegram_message_id=receipt,
            )
            for status, error, receipt in [
                ("success", None, 1),
                ("failed", "unknown:telegram_read_budget: retry_after_seconds=600", None),
                ("failed", "transient:network_failure", None),
                (
                    "failed",
                    "send_outcome_unknown:telegram_read_budget: retry_after_seconds=600",
                    None,
                ),
            ]
        ]
    )
    await test_db.commit()
    metrics = AccountDynamicFrequencyService(test_db)
    result = await metrics.ad_delivery_metrics(account.id, now + timedelta(seconds=1))
    assert (result["success"], result["failed"], result["deferred"]) == (1, 2, 1)
    assert result["success_rate"] == pytest.approx(1 / 3)
    quality = await metrics.account_join_quality_metrics(account.id, now + timedelta(seconds=1))
    assert quality["ad_deferred_24h"] == 1


async def test_capacity_cache_has_fresh_result_consumers_and_request_isolation(test_db):
    test_db.add_all(
        [
            SystemSetting(key="projection.a", value="one"),
            SystemSetting(key="projection.b", value="two"),
        ]
    )
    await test_db.commit()
    reader = CapacityReads(test_db)

    def query(key):
        return select(SystemSetting.value).where(SystemSetting.key == key)

    statements = []

    def count(*args):
        statements.append(args[2])

    engine = test_db.bind.sync_engine
    event.listen(engine, "before_cursor_execute", count)
    try:
        assert await reader.scalar(query("projection.a")) == "one"
        assert await reader.scalar(query("projection.a")) == "one"
        assert (await reader.scalars(query("projection.b"))).all() == ["two"]
        assert len(statements) == 2
    finally:
        event.remove(engine, "before_cursor_execute", count)
    await test_db.execute(
        update(SystemSetting).where(SystemSetting.key == "projection.a").values(value="new")
    )
    await test_db.commit()
    assert await CapacityReads(test_db).scalar(query("projection.a")) == "new"
    with pytest.raises(RuntimeError):
        await reader.execute(update(SystemSetting).values(value="bad"))
    with pytest.raises(RuntimeError):
        await reader.scalar(select(SystemSetting).with_for_update())


async def test_known_wait_does_not_add_failure_or_fake_check_time(test_db, monkeypatch):
    now = datetime.utcnow()
    monkeypatch.setattr(
        "app.core.account.read_schedule.check_read_ready",
        AsyncMock(side_effect=RpcDeferred("telegram_read_budget", 600)),
    )
    reason, due = await read_wait(test_db, 2, now, purpose="join_request_reconciliation")
    assert due == now + timedelta(seconds=601)
    attempt = AutoJoinAttempt(
        account_id=2,
        status="pending",
        attempted_at=now - timedelta(days=1),
        reconciliation_failure_count=3,
        reconciliation_checked_at=now - timedelta(hours=3),
        reconciliation_next_at=now + timedelta(hours=2),
        telegram_action_attempted=True,
    )
    test_db.add(attempt)
    await test_db.commit()
    checked = attempt.reconciliation_checked_at
    await AcquisitionAutomationService(
        test_db, account_pool=SimpleNamespace()
    )._defer_join_reconciliation(attempt, now, reason, read_wait_until=due)
    assert attempt.reconciliation_failure_count == 3
    assert attempt.reconciliation_checked_at == checked
    assert due < attempt.reconciliation_next_at <= due + timedelta(seconds=7)
    assert attempt.telegram_action_attempted is True and attempt.status == "pending"


async def test_fresh_worker_heartbeat_does_not_imply_connected_listener(test_db, monkeypatch):
    from app.core.runtime_health import operational_snapshot
    from app.core.worker_status import TelegramWorkerStatus

    now = datetime.utcnow()
    account = TelegramAccount(identifier="listen", session_name="listen")
    test_db.add(account)
    await test_db.flush()
    test_db.add(AccountOperationConfig(account_id=account.id, enabled=True, auto_ads_enabled=True))
    worker = TelegramWorkerStatus(
        worker_id="test",
        role="growth_user_worker",
        last_heartbeat_at=now,
        metadata_json=json.dumps(
            {
                "runtime": {
                    "listeners": [
                        {
                            "account_id": account.id,
                            "state": "deferred",
                            "connected": False,
                            "reason": "telegram_read_budget",
                        }
                    ]
                }
            }
        ),
    )
    test_db.add(worker)
    await test_db.commit()
    monkeypatch.setattr(
        "app.core.redis.get_redis",
        AsyncMock(
            return_value=SimpleNamespace(
                get=AsyncMock(return_value=None), info=AsyncMock(return_value={})
            )
        ),
    )
    monkeypatch.setattr(
        "app.core.account.rpc_governor.snapshot",
        AsyncMock(return_value={"state": "ready", "lanes": {}}),
    )
    result = await operational_snapshot(test_db)
    assert result["workers"]["growth_user_worker"]["fresh"] is True
    assert result["accounts"][0]["listener"]["connected"] is False
    assert "listener_not_connected" in {item["code"] for item in result["alerts"]}
    worker.metadata_json = "{}"
    await test_db.commit()
    result = await operational_snapshot(test_db)
    assert "listener_status_unknown" in {item["code"] for item in result["alerts"]}
