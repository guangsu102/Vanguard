"""Fault-injection coverage for boundaries missed by the initial cutover."""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import json

import pytest
from sqlalchemy import select

from app.core.account.models import AccountOperationConfig, TelegramAccount
from app.core.group.models import Group
from app.core.settings_models import SystemSetting
from app.modules.acquisition import simple_growth
from app.modules.acquisition.models import AdDeliveryLog, AutoJoinAttempt
from app.modules.acquisition.simple_runtime import recover_result_journals
from app.modules.acquisition.send_result_observation import observe
from tests.unit.test_simplified_growth import setup, redis


@pytest.mark.parametrize("revoked", ["verification", "account", "creative", "binding", "protection"])
async def test_revocation_during_admission_prevents_rpc(test_db, monkeypatch, redis, revoked):
    svc, account, member, campaign, creative, client = await setup(test_db, monkeypatch, redis)
    account_id = account.id
    take = simple_growth.take_ad
    async def admission(*args, **kwargs):
        result = await take(*args, **kwargs)
        if revoked == "verification":
            member.verification_status, member.review_status, member.ad_status = "pending", "pending", "blocked"
        elif revoked == "account":
            config = await test_db.scalar(select(AccountOperationConfig).where(AccountOperationConfig.account_id == account.id))
            config.auto_ads_enabled = False
        elif revoked == "creative":
            creative.enabled = False
        elif revoked == "binding":
            from app.modules.acquisition.models import AccountAdBinding
            binding = await test_db.scalar(select(AccountAdBinding))
            binding.enabled = False
        else:
            member.ad_status = "paused"
        await test_db.commit()
        return result
    monkeypatch.setattr(simple_growth, "take_ad", admission)
    try:
        await svc.send(account.id, member.id, campaign.id, creative.id, member.telegram_group_id)
    except simple_growth.Deferred:
        pass
    client.send_message.assert_not_awaited()
    from app.modules.acquisition.simple_limits import PREFIX
    assert await redis.hget(PREFIX + f"ads:{account_id}:count", "used") == "0"
    assert await test_db.scalar(select(AdDeliveryLog.id)) is None


async def test_paused_account_interrupted_attempt_recovers_without_journal(test_db, monkeypatch, redis):
    svc, account, member, campaign, creative, client = await setup(test_db, monkeypatch, redis)
    client.send_message.side_effect = TimeoutError()
    await svc.send(account.id, member.id, campaign.id, creative.id, member.telegram_group_id)
    log = await test_db.scalar(select(AdDeliveryLog))
    log.status, log.created_at = "sending", datetime.utcnow() - timedelta(minutes=10)
    config = await test_db.scalar(select(AccountOperationConfig).where(AccountOperationConfig.account_id == account.id))
    config.enabled = False
    await test_db.commit()
    for _ in range(8):
        await recover_result_journals(test_db, redis)
    await test_db.refresh(log)
    assert log.status == "unknown"
    assert client.send_message.await_count == 1


async def test_live_owner_excludes_global_interrupted_recovery(test_db, monkeypatch, redis):
    from app.modules.acquisition.simple_limits import lease
    svc, account, member, campaign, creative, client = await setup(test_db, monkeypatch, redis)
    client.send_message.side_effect = TimeoutError()
    await svc.send(account.id, member.id, campaign.id, creative.id, member.telegram_group_id)
    log = await test_db.scalar(select(AdDeliveryLog))
    log.status, log.created_at = "sending", datetime.utcnow() - timedelta(minutes=10)
    await test_db.commit()
    async with lease(redis, f"account:{account.id}"):
        await recover_result_journals(test_db, redis)
    await test_db.refresh(log)
    assert log.status == "sending"
    await recover_result_journals(test_db, redis)
    await test_db.refresh(log)
    assert log.status == "unknown" and client.send_message.await_count == 1


async def test_bounded_recovery_preserves_receipts_beyond_current_page(test_db, monkeypatch, redis):
    from copy import deepcopy
    from app.modules.acquisition.simple_limits import PREFIX
    svc, account, member, campaign, creative, client = await setup(test_db, monkeypatch, redis)
    client.send_message.side_effect = TimeoutError()
    await svc.send(account.id, member.id, campaign.id, creative.id, member.telegram_group_id)
    original = await test_db.scalar(select(AdDeliveryLog))
    context = json.loads(original.qualification_context_json)
    old = datetime.utcnow() - timedelta(minutes=10)
    for index in range(5):
        data = deepcopy(context["attempt_context"])
        data.update(attempt="receipt-page-" + str(index), sent=old.isoformat(), outcome="success", message_id=100+index)
        test_db.add(AdDeliveryLog(account_id=account.id, group_id=member.group_id, telegram_group_id=member.telegram_group_id,
            ad_campaign_id=campaign.id, creative_id=creative.id, reservation_token=data["attempt"], status="sending",
            created_at=old, qualification_context_json=json.dumps({"policy": context["policy"], "attempt_context": data})))
        await redis.hset(PREFIX + f"results:{account.id}", data["attempt"], json.dumps(data))
    await test_db.delete(original)
    await test_db.commit()
    await recover_result_journals(test_db, redis)
    states = list((await test_db.scalars(select(AdDeliveryLog.status))).all())
    assert "unknown" not in states
    for _ in range(8):
        await recover_result_journals(test_db, redis)
    assert set((await test_db.scalars(select(AdDeliveryLog.status))).all()) == {"success"}
    assert client.send_message.await_count == 1


async def observation_case(db, monkeypatch, redis):
    db.add(TelegramAccount(identifier="omission-dummy", session_name="omission-dummy"))
    await db.commit()
    svc, account, member, campaign, creative, client = await setup(db, monkeypatch, redis)
    now = datetime.utcnow()
    started = now - timedelta(hours=25)
    account.risk_level, account.spam_check_status, account.spam_checked_at = "watch", "clear", now
    account.risk_reason = "existing_recovery"
    db.add(SystemSetting(key="growth.canary.account.2", value=json.dumps({"enabled": True,
        "send_result_started_at": started.isoformat(), "ad_daily": 10, "join_daily": 10})))
    db.add(SystemSetting(key="growth.send_result.cutover.v1", value=json.dumps({"observed_risk_reason": account.risk_reason})))
    for i in range(3):
        group = Group(group_id=-1000000002000-i, title="observation-proof")
        db.add(group)
        await db.flush()
        db.add(AdDeliveryLog(account_id=2, group_id=group.id, ad_campaign_id=campaign.id,
            telegram_group_id=group.group_id, status="success", telegram_message_id=i+1,
            created_at=now-timedelta(hours=1)))
    await db.commit()
    return account, member, started


async def test_observation_preserves_unexpired_recovery_limit(test_db, monkeypatch, redis):
    account, _, _started = await observation_case(test_db, monkeypatch, redis)
    until = datetime.utcnow() + timedelta(days=7)
    account.risk_recovery_until = until
    await test_db.commit()
    await observe(test_db, 2)
    policy = json.loads((await test_db.get(SystemSetting, "growth.canary.account.2")).value)
    assert policy["enabled"] is True
    assert account.risk_recovery_until == until


async def test_observation_rejects_join_quota_violation(test_db, monkeypatch, redis):
    account, member, _started = await observation_case(test_db, monkeypatch, redis)
    for _ in range(11):
        test_db.add(AutoJoinAttempt(account_id=2, group_id=member.group_id, status="success",
            telegram_action_attempted=True, attempted_at=datetime.utcnow()))
    await test_db.commit()
    await observe(test_db, 2)
    policy = json.loads((await test_db.get(SystemSetting, "growth.canary.account.2")).value)
    assert policy["enabled"] is True


async def test_failed_observation_can_be_explicitly_restarted(test_db, monkeypatch, redis, tmp_path):
    from scripts import activate_send_result as operations
    account, _, started = await observation_case(test_db, monkeypatch, redis)
    account.last_risk_event_at = started + timedelta(hours=1)
    await test_db.commit()
    @asynccontextmanager
    async def session():
        yield test_db
    monkeypatch.setattr(operations, "get_db_session", session)
    await operations.begin_observation(2, tmp_path / "restart.json", restart=True)
    policy = json.loads((await test_db.get(SystemSetting, "growth.canary.account.2")).value)
    assert datetime.fromisoformat(policy["send_result_started_at"]) > account.last_risk_event_at
    assert json.loads((tmp_path / "restart.json").read_text())["restart"] is True


async def test_observation_start_is_idempotent_and_restart_preserves_manual_pause(test_db, monkeypatch, redis, tmp_path):
    from scripts import activate_send_result as operations
    account, _, started = await observation_case(test_db, monkeypatch, redis)
    config = await test_db.scalar(select(AccountOperationConfig).where(AccountOperationConfig.account_id == 2))
    config.auto_ads_enabled = False
    await test_db.commit()
    @asynccontextmanager
    async def session():
        yield test_db
    monkeypatch.setattr(operations, "get_db_session", session)
    await operations.begin_observation(2, tmp_path / "unused.json")
    policy = json.loads((await test_db.get(SystemSetting, "growth.canary.account.2")).value)
    assert datetime.fromisoformat(policy["send_result_started_at"]) == started
    assert not (tmp_path / "unused.json").exists()
    await operations.begin_observation(2, tmp_path / "restart.json", restart=True)
    assert config.auto_ads_enabled is False


async def test_live_age_metrics_exclude_terminal_history(test_db, monkeypatch, redis):
    from app.core.worker_status import TelegramEventInbox as Inbox
    from app.modules.acquisition.send_result_metrics import aggregate
    svc, account, member, campaign, creative, client = await setup(test_db, monkeypatch, redis)
    client.send_message.side_effect = TimeoutError()
    await svc.send(account.id, member.id, campaign.id, creative.id, member.telegram_group_id)
    log = await test_db.scalar(select(AdDeliveryLog))
    log.status, log.created_at = "sending", datetime.utcnow() - timedelta(minutes=10)
    live = Inbox(account_id=account.id, event_key="live", kind="message", payload_json="{}",
                 state="pending", received_at=datetime.utcnow() - timedelta(minutes=5))
    test_db.add_all([live, Inbox(account_id=account.id, event_key="old", kind="message", payload_json="{}",
        state="archived_unknown", received_at=datetime.utcnow() - timedelta(days=30))])
    await test_db.commit()
    snapshot = await aggregate(test_db, redis)
    values = {g["name"]: g["value"] for g in snapshot["gauges"] if not g["labels"]}
    assert 300 <= values["vanguard_send_result_inbox_oldest_age_seconds"] < 305
    assert 600 <= values["vanguard_send_result_sending_oldest_age_seconds"] < 605
    log.status, live.state = "unknown", "completed"
    await test_db.commit()
    snapshot = await aggregate(test_db, redis)
    assert all(g["value"] == 0 for g in snapshot["gauges"] if g["name"].endswith("_oldest_age_seconds"))
