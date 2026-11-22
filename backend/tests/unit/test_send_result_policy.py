"""Business acceptance of send-only growth and bounded failure handling."""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.core.group.models import GroupAccountMembership as Member
from app.core.settings_models import SystemSetting
from app.modules.acquisition.models import AdDeliveryLog, GroupAdFrequency
from app.modules.acquisition.simple_limits import PREFIX
from app.modules.acquisition.send_result_policy import classify, EXIT_REASON
from tests.unit.test_simplified_growth import setup, redis


async def due(db, redis, member):
    member.next_ad_allowed_at = datetime.utcnow() - timedelta(seconds=1)
    await db.commit()
    await redis.delete(PREFIX + f"ads:{member.account_id}:due", PREFIX + f"group:{member.telegram_group_id}:due")


async def test_three_group_failures_trigger_one_exit_and_no_review(test_db, monkeypatch, redis):
    svc, account, member, campaign, creative, client = await setup(test_db, monkeypatch, redis)
    client.send_message.side_effect = type("ChatWriteForbiddenError", (Exception,), {})()
    args = account.id, member.id, campaign.id, creative.id, member.telegram_group_id
    for number in range(1, 4):
        await due(test_db, redis, member)
        response = await svc.send(*args)
        assert response["succeeded"] == 0
        assert member.ad_failure_streak == number
        assert member.review_status == "approved"
        if number < 3:
            assert member.ad_status == "active" and member.leave_requested_at is None
        else:
            assert member.ad_status == "blocked" and member.leave_error == EXIT_REASON
    assert client.send_message.await_count == 3
    await svc.send(*args)
    assert client.send_message.await_count == 3
    assert await test_db.scalar(select(func.count(GroupAdFrequency.telegram_group_id))) == 0


async def test_success_clears_streak_and_never_creates_survival_work(test_db, monkeypatch, redis):
    svc, account, member, campaign, creative, client = await setup(test_db, monkeypatch, redis)
    member.ad_failure_streak = 2
    client.send_message.return_value = NS(id=123, ttl_period=604800)
    await test_db.commit()
    result = await svc.send(account.id, member.id, campaign.id, creative.id, member.telegram_group_id)
    assert result["succeeded"] == 1 and member.ad_failure_streak == 0
    log = await test_db.scalar(select(AdDeliveryLog))
    assert log.status == "success" and log.survival_status == "not_required" and log.survival_check_due_at is None
    import json
    data = json.loads(log.qualification_context_json)["attempt_context"]
    await svc.persist_ad_result(data)
    assert await test_db.scalar(select(func.count(AdDeliveryLog.id))) == 1


async def test_unknown_next_slot_can_send_new_operation(test_db, monkeypatch, redis):
    svc, account, member, campaign, creative, client = await setup(test_db, monkeypatch, redis)
    args = account.id, member.id, campaign.id, creative.id, member.telegram_group_id
    client.send_message.side_effect = TimeoutError()
    assert (await svc.send(*args))["outcome"] == "unknown"
    await svc.send(*args)
    assert client.send_message.await_count == 1
    await due(test_db, redis, member)
    client.send_message.side_effect = None
    await svc.send(*args)
    assert client.send_message.await_count == 2
    rows = list((await test_db.scalars(select(AdDeliveryLog).order_by(AdDeliveryLog.id))).all())
    assert [r.status for r in rows] == ["unknown", "success"]
    assert rows[0].reservation_token != rows[1].reservation_token


async def test_pre_rpc_record_and_interrupted_recovery_never_replays(test_db, monkeypatch, redis):
    svc, account, member, campaign, creative, client = await setup(test_db, monkeypatch, redis)
    account_id = account.id
    async def send(*args, **kwargs):
        assert not test_db.in_transaction()
        log = await test_db.scalar(select(AdDeliveryLog))
        assert log.status == "sending"
        await test_db.commit()
        raise TimeoutError()
    client.send_message.side_effect = send
    await svc.send(account_id, member.id, campaign.id, creative.id, member.telegram_group_id)
    log = await test_db.scalar(select(AdDeliveryLog))
    log.status = "sending"  # Simulate restart after the pre-RPC commit, before a result was saved.
    await test_db.commit()
    await svc.recover_interrupted_ads(account_id)
    await svc.recover_interrupted_ads(account_id)
    assert log.status == "unknown"
    assert client.send_message.await_count == 1


async def test_exit_stops_after_three_failures(test_db, monkeypatch, redis):
    svc, account, member, *_ = await setup(test_db, monkeypatch, redis)
    member.leave_error, member.leave_attempts = EXIT_REASON, 0
    member.leave_requested_at = member.leave_retry_at = datetime.utcnow()
    member.ad_status = "blocked"
    await test_db.commit()
    rpc = AsyncMock(side_effect=OSError("offline"))
    rpc.get_entity.return_value = NS(id=123)
    svc.pool.acquire_by_id.return_value.client = rpc
    for _ in range(3):
        member.leave_retry_at = datetime.utcnow()
        await test_db.commit()
        assert await svc.exits(account.id, 4) == 0
    assert member.leave_attempts == 3 and member.leave_retry_at is None
    await svc.exits(account.id, 4)
    assert rpc.await_count == 3


@pytest.mark.parametrize("handover", [False, True])
async def test_exit_preserves_other_account_ownership(test_db, monkeypatch, redis, handover):
    from app.core.account.models import TelegramAccount
    from app.core.group.models import Group
    from sqlalchemy import update
    svc, account, member, *_ = await setup(test_db, monkeypatch, redis)
    other = TelegramAccount(identifier="other-owner", session_name="other-owner")
    test_db.add(other)
    await test_db.flush()
    group = await test_db.get(Group, member.group_id)
    group.ad_delivery_account_id = account.id
    member.leave_error, member.leave_attempts = EXIT_REASON, 0
    member.leave_requested_at = member.leave_retry_at = datetime.utcnow()
    member.ad_status = "blocked"
    await test_db.commit()
    async def leave(*args, **kwargs):
        if handover:
            await test_db.execute(update(Group).where(Group.id == group.id).values(
                ad_delivery_account_id=other.id).execution_options(synchronize_session=False))
            await test_db.commit()
    rpc = AsyncMock(side_effect=leave)
    rpc.get_entity.return_value = NS(id=123)
    svc.pool.acquire_by_id.return_value.client = rpc
    assert await svc.exits(account.id, 4) == 1
    await test_db.refresh(group)
    assert member.status == "left"
    assert group.ad_delivery_account_id == (other.id if handover else None)


@pytest.mark.parametrize("name,result", [("FloodWaitError", "rate_limited"),
    ("SlowModeWaitError", "rate_limited"), ("MessageTooLongError", "content_invalid"),
    ("PeerFloodError", "failed"), ("UserNotParticipantError", "not_member")])
def test_non_group_errors_are_not_counted(name, result):
    assert classify(type(name, (Exception,), {})()) == result


async def test_cutover_preview_apply_and_repeat(test_db, monkeypatch, redis, tmp_path):
    from scripts import activate_send_result as migration
    svc, account, member, campaign, creative, client = await setup(test_db, monkeypatch, redis)
    @asynccontextmanager
    async def session():
        yield test_db
    monkeypatch.setattr(migration, "get_db_session", session)
    from app.core.worker_status import TelegramEventInbox as Inbox
    old = datetime.utcnow() - timedelta(days=2)
    first = Inbox(account_id=account.id, chat_id=900, event_key="old-pending", kind="message", payload_json='{"original":true}',
        state="pending", external_attempted=False, received_at=old)
    second = Inbox(account_id=account.id, chat_id=901, event_key="old-unknown", kind="message", payload_json='{"original":true}',
        state="reconciliation_required", external_attempted=True, received_at=old)
    test_db.add_all([first, second])
    member.ad_failure_streak = 2
    member.leave_error = "deleted_at_minimum"
    member.leave_requested_at = member.leave_retry_at = datetime.utcnow()
    await test_db.commit()
    assert "preview" in await migration.cutover()
    assert member.ad_failure_streak == 2
    snapshot = tmp_path / "cutover.json"
    assert "applied" in await migration.cutover(apply=True, snapshot=snapshot)
    assert snapshot.exists() and member.ad_failure_streak == 0 and member.leave_requested_at is None
    assert first.state == "expired" and second.state == "archived_unknown"
    assert second.external_attempted and second.payload_json == '{"original":true}'
    member.ad_failure_streak = 1
    await test_db.commit()
    assert (await migration.cutover(apply=True, snapshot=snapshot))["already_applied"]
    assert member.ad_failure_streak == 1
    client.send_message.assert_not_awaited()


async def test_retention_requires_checkpoint_and_keeps_unknown(test_db, monkeypatch, redis):
    from app.core.worker_status import TelegramEventInbox as Inbox
    from app.modules.acquisition.send_result_retention import clean_completed, WATERMARK
    svc, account, *_ = await setup(test_db, monkeypatch, redis)
    old = datetime.utcnow() - timedelta(days=10)
    for key, state, attempted in [("closed", "completed", False), ("unknown", "archived_unknown", True)]:
        test_db.add(Inbox(account_id=account.id, chat_id=555, event_key=key, kind="listener_fact",
                          payload_json="{}", state=state, external_attempted=attempted,
                          received_at=old, completed_at=old))
    await test_db.commit()
    assert await clean_completed(test_db) == 0
    import json
    test_db.add(SystemSetting(key=WATERMARK + str(account.id), value=json.dumps({
        "protocol_checkpoint_safe": True, "before": datetime.utcnow().isoformat()})))
    await test_db.commit()
    assert await clean_completed(test_db) == 1
    assert await test_db.scalar(select(Inbox.state)) == "archived_unknown"


async def test_metrics_publish_send_outcomes(test_db, monkeypatch, redis):
    from app.modules.acquisition.send_result_metrics import aggregate
    from app.core.metrics import render
    svc, account, member, campaign, creative, client = await setup(test_db, monkeypatch, redis)
    await svc.send(account.id, member.id, campaign.id, creative.id, member.telegram_group_id)
    await aggregate(test_db, redis)
    body = (await render(redis)).decode()
    assert 'vanguard_send_result_today{outcome="success"} 1.0' in body
    assert 'vanguard_send_result_survival_enabled 0.0' in body


async def test_resolution_failure_counts_without_sending(test_db, monkeypatch, redis):
    svc, account, member, campaign, creative, client = await setup(test_db, monkeypatch, redis)
    client.get_input_entity.side_effect = type("ChannelPrivateError", (Exception,), {})()
    await svc.send(account.id, member.id, campaign.id, creative.id, member.telegram_group_id)
    assert member.ad_failure_streak == 1
    client.send_message.assert_not_awaited()
    assert await redis.hget(PREFIX + f"ads:{account.id}:count", "used") is None


async def test_observed_mute_does_not_create_a_review_or_early_exit(test_db, monkeypatch, redis):
    from app.modules.acquisition.member_permissions import Permission, apply_permission
    svc, account, member, campaign, creative, client = await setup(test_db, monkeypatch, redis)
    apply_permission(member, Permission(True, False, "permanent_mute", None, True), datetime.utcnow())
    await test_db.commit()
    assert member.review_status == "approved" and member.leave_requested_at is None
    client.send_message.side_effect = type("ChatWriteForbiddenError", (Exception,), {})()
    await svc.send(account.id, member.id, campaign.id, creative.id, member.telegram_group_id)
    assert member.ad_failure_streak == 1 and member.leave_requested_at is None


@pytest.mark.parametrize("manual_pause", [False, True])
async def test_observation_gate_preserves_manual_control(test_db, monkeypatch, redis, manual_pause):
    import json
    from app.core.account.models import TelegramAccount, AccountOperationConfig
    from app.core.group.models import Group
    from app.modules.acquisition.send_result_observation import observe
    test_db.add(TelegramAccount(identifier="dummy", session_name="dummy"))
    await test_db.commit()
    svc, account, member, campaign, creative, client = await setup(test_db, monkeypatch, redis)
    assert account.id == 2
    now = datetime.utcnow()
    started = now - timedelta(hours=25)
    account.risk_level, account.spam_check_status, account.spam_checked_at = "watch", "clear", now
    config = await test_db.scalar(select(AccountOperationConfig).where(AccountOperationConfig.account_id == 2))
    config.auto_ads_enabled = not manual_pause
    test_db.add(SystemSetting(key="growth.canary.account.2", value=json.dumps({"enabled": True,
        "send_result_started_at": started.isoformat()})))
    test_db.add(SystemSetting(key="growth.send_result.cutover.v1", value=json.dumps({"observed_risk_reason": account.risk_reason})))
    for i in range(3):
        group = Group(group_id=-1000000001000-i, title="observed")
        test_db.add(group)
        await test_db.flush()
        test_db.add(AdDeliveryLog(account_id=2, group_id=group.id, telegram_group_id=group.group_id,
            status="success", ad_campaign_id=campaign.id, telegram_message_id=i+1, created_at=now-timedelta(hours=1)))
    await test_db.commit()
    await observe(test_db, 2)
    policy = json.loads((await test_db.get(SystemSetting, "growth.canary.account.2")).value)
    assert policy["enabled"] is manual_pause
    assert config.auto_ads_enabled is not manual_pause
    assert account.risk_level == ("watch" if manual_pause else "normal")
