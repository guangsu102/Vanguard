import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.account.models import (
    AccountOperationConfig,
    AccountOutboundAttempt,
    AccountStatus,
    AccountType,
    TelegramAccount,
)
from app.core.account.outbound_budget import (
    AccountOutboundBudgetService,
    OutboundBudgetBlocked,
)
from app.modules.acquisition.adaptive_frequency import (
    FrequencyService,
    canonical,
    interval_seconds,
    negative_observation,
)
from app.modules.acquisition.models import AdCampaign, AdDeliveryLog, GroupAdFrequencyEvent

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 9, 26, 10)
TARGET = -1000000000123


async def setup(db, quota=1, mature=False):
    account = TelegramAccount(
        identifier="adaptive",
        session_name="adaptive",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        risk_level="normal",
        is_active=True,
        registered_at=NOW - timedelta(days=365),
    )
    db.add(account)
    await db.flush()
    config = AccountOperationConfig(
        account_id=account.id,
        enabled=True,
        auto_ads_enabled=True,
        dynamic_capacity_enabled=True,
        adaptive_ads_enabled=True,
        max_ads_per_day=30,
        max_messages_per_day=62,
        max_verification_messages_per_day=30,
        max_diagnostic_messages_per_day=2,
        message_interval_seconds=600,
    )
    campaign = AdCampaign(name="adaptive", enabled=True)
    db.add_all([config, campaign])
    service = FrequencyService(db)
    state = await service.state(TARGET, create=True, lock=True, now=NOW - timedelta(days=5))
    state.quota, state.mature = quota, mature
    await db.commit()
    return account, config, campaign, service, state


async def delivery(db, account, campaign, state, at, *, status="survived", epoch=None, quota=None):
    fc = FrequencyService.context(state)
    if epoch is not None:
        fc["epoch"] = epoch
    if quota is not None:
        fc["quota"] = quota
    log = AdDeliveryLog(
        account_id=account.id,
        ad_campaign_id=campaign.id,
        telegram_group_id=TARGET,
        status="success",
        telegram_message_id=int(at.timestamp()),
        sent_at=at,
        created_at=at,
        survival_status=status,
        survival_stage="complete" if status == "survived" else "twenty_four_hour",
        survived_two_minute_at=at + timedelta(minutes=2),
        survived_one_hour_at=at + timedelta(hours=1),
        survived_twenty_four_hour_at=at + timedelta(days=1) if status == "survived" else None,
        survival_check_due_at=at + timedelta(days=1) if status == "pending" else None,
        qualification_context_json=json.dumps({"telegram_group_id": TARGET, "frequency": fc}),
    )
    db.add(log)
    await db.flush()
    return log


async def test_three_distinct_24h_successes_promote_once(test_db):
    a, c, camp, svc, state = await setup(test_db)
    for i in range(3):
        log = await delivery(test_db, a, camp, state, NOW - timedelta(days=4 - i))
        await svc.observe(log, "survived", NOW)
        await svc.observe(log, "survived", NOW)
        assert state.quota == (2 if i == 2 else 1)
    assert state.mature and state.epoch == 2
    assert len(list((await test_db.scalars(select(GroupAdFrequencyEvent))).all())) == 3


async def test_checkpoint_before_24h_cannot_promote(test_db):
    a, c, camp, svc, state = await setup(test_db)
    log = await delivery(test_db, a, camp, state, NOW - timedelta(hours=2))
    await svc.observe(log, "survived", NOW)
    assert state.quota == 1
    assert (await test_db.scalar(select(GroupAdFrequencyEvent.id))) is None


async def test_no_same_day_multiple_promotions_or_old_epoch_credit(test_db):
    a, c, camp, svc, state = await setup(test_db, 2, True)
    state.epoch_started_at = NOW - timedelta(hours=23)
    for i in range(3):
        log = await delivery(test_db, a, camp, state, NOW - timedelta(days=2, hours=i), epoch=0)
        await svc.observe(log, "survived", NOW)
    assert state.quota == 2


async def test_group_hard_cap_30_counts_all_accounts_and_campaigns(test_db):
    a, c, camp, svc, state = await setup(test_db, 30, True)
    for i in range(30):
        log = await delivery(test_db, a, camp, state, NOW - timedelta(hours=23, minutes=i))
        log.account_id = 100 + i
    ready, _ = await svc.readiness(TARGET, NOW)
    assert ready.reason == "frequency_group_daily_cap"
    assert ready.next_allowed_at is not None


async def test_mature_pending_not_due_allows_next_send(test_db):
    a, c, camp, svc, state = await setup(test_db, 30, True)
    await delivery(test_db, a, camp, state, NOW - timedelta(minutes=49), status="pending")
    ready, _ = await svc.readiness(TARGET, NOW)
    assert ready.reason is None


async def test_overdue_or_unknown_checkpoint_blocks(test_db):
    a, c, camp, svc, state = await setup(test_db, 30, True)
    log = await delivery(test_db, a, camp, state, NOW - timedelta(hours=25), status="pending")
    assert (await svc.readiness(TARGET, NOW))[0].reason == "frequency_survival_due"
    log.survival_check_due_at = NOW + timedelta(minutes=5)
    log.survival_error = "timeout"
    assert (await svc.readiness(TARGET, NOW))[0].reason == "frequency_survival_unresolved"


async def test_delete_halves_once_per_epoch_and_cannot_fake_low_rate_failure(test_db):
    a, c, camp, svc, state = await setup(test_db, 3, True)
    one = await delivery(test_db, a, camp, state, NOW - timedelta(hours=3))
    two = await delivery(test_db, a, camp, state, NOW - timedelta(hours=2))
    await svc.observe(one, "deleted", NOW)
    assert state.quota == 1 and state.status == "active"
    await svc.observe(two, "deleted", NOW)
    assert state.quota == 1 and state.status == "active" and state.epoch == 2
    new = await delivery(test_db, a, camp, state, NOW + timedelta(days=2))
    await svc.observe(new, "deleted", NOW + timedelta(days=2, minutes=5))
    assert state.status == "exit_pending"


async def test_muted_exits_without_gradual_reduction(test_db):
    a, c, camp, svc, state = await setup(test_db, 30, True)
    log = await delivery(test_db, a, camp, state, NOW - timedelta(hours=2))
    await svc.observe(log, "muted", NOW)
    assert state.status == "exit_pending" and state.reason == "frequency_muted"


async def test_reduced_quota_keeps_previous_rolling_exposures(test_db):
    a, c, camp, svc, state = await setup(test_db, 8, True)
    for i in range(4):
        await delivery(test_db, a, camp, state, NOW - timedelta(hours=i + 1))
    state.quota = 3
    assert (await svc.readiness(TARGET, NOW))[0].reason == "frequency_group_daily_cap"


async def test_stale_reservation_is_rejected(test_db):
    a, c, camp, svc, state = await setup(test_db, 5, True)
    log = await delivery(test_db, a, camp, state, NOW)
    log.status = "pending"
    log.telegram_message_id = None
    log.sent_at = None
    log.reservation_token = "reserved"
    state.epoch += 1
    assert (await svc.readiness(TARGET, NOW, reservation_token="reserved"))[
        0
    ].reason == "frequency_reservation_stale"


async def test_independent_probe_budget_and_mature_capacity(test_db):
    a, c, camp, svc, state = await setup(test_db, 2, True)
    for i in range(30):
        test_db.add(
            AccountOutboundAttempt(
                account_id=a.id,
                attempt_key=f"old{i}",
                category="ad",
                target_key=str(i),
                state="succeeded",
                attempted_at=NOW - timedelta(hours=2),
                context_json="{}",
            )
        )
    await test_db.commit()
    budget = AccountOutboundBudgetService(test_db)
    info = await budget.snapshot(a.id, NOW)
    assert info["ad_lanes"]["probe"]["remaining"] == 0
    assert info["ad_lanes"]["mature"]["remaining"] > 0
    assert info["limits"]["total"] > 62
    await budget._check(a.id, "ad", NOW, context={"frequency": svc.context(state)})
    state.mature = False
    with pytest.raises(OutboundBudgetBlocked, match="outbound_ad_probe_budget"):
        await budget._check(a.id, "ad", NOW, context={"frequency": svc.context(state)})


async def test_account_cooldown_still_blocks_both_lanes(test_db):
    a, c, camp, svc, state = await setup(test_db, 2, True)
    a.risk_pause_until = NOW + timedelta(hours=1)
    await test_db.commit()
    with pytest.raises(OutboundBudgetBlocked, match="account_capacity_unavailable"):
        await AccountOutboundBudgetService(test_db)._check(
            a.id, "ad", NOW, context={"frequency": svc.context(state)}
        )


async def test_namespace_and_intervals():
    assert canonical(123) is None
    assert canonical(123, {"group_type": "supergroup"}) == TARGET
    assert canonical(-123) == -123
    assert interval_seconds(30) == 2880 and interval_seconds(1) == 86400


async def test_deletion_needs_two_successful_reads_no_ttl_or_transport_failure():
    facts = {
        "group_accessible": True,
        "account_readable": True,
        "member": True,
        "can_send": True,
        "exists": False,
        "ttl_period": 0,
        "errors": [],
    }
    previous = [{"checked_at": (NOW - timedelta(minutes=5)).isoformat(), "facts": facts.copy()}]
    assert negative_observation(facts, [], NOW) is None
    assert negative_observation(facts, previous, NOW) == "deleted"
    assert negative_observation({**facts, "errors": ["FloodWait"]}, previous, NOW) is None
    assert negative_observation({**facts, "ttl_period": 60}, previous, NOW) is None
    assert negative_observation({**facts, "member_muted": True}, [], NOW) == "muted"


async def test_rule_frequency_limit_caps_current_scheduling_and_ignores_member_claims(test_db):
    from app.modules.acquisition.adaptive_frequency import rule_quota

    context = {
        "evidence": [{"sender_role": "group_metadata", "text": "本群允许广告，每天最多 2 条。"}]
    }
    assert rule_quota(context) == 2
    assert rule_quota({"evidence": [{"sender_role": "member", "text": "广告每天最多 1 条"}]}) == 30
    assert rule_quota({"evidence": [{"verified_admin": True, "text": "广告间隔至少十二小时"}]}) == 2
    a, c, camp, svc, state = await setup(test_db, 30, True)
    await delivery(test_db, a, camp, state, NOW - timedelta(hours=3))
    assert (await svc.readiness(TARGET, NOW, context=context))[
        0
    ].reason == "frequency_group_interval"


async def test_group_stays_at_thirty_and_does_not_skip_freeze(test_db):
    a, c, camp, svc, state = await setup(test_db, 30, True)
    for i in range(4):
        log = await delivery(test_db, a, camp, state, NOW - timedelta(days=2, hours=i))
        await svc.observe(log, "survived", NOW)
    assert state.quota == 30
    state.quota = 3
    state.promote_after = NOW + timedelta(hours=72)
    log = await delivery(test_db, a, camp, state, NOW - timedelta(days=1))
    await svc.observe(log, "survived", NOW)
    assert state.quota == 3


async def test_real_reservation_checks_epoch_again_before_rpc(test_db):
    a, c, camp, svc, state = await setup(test_db, 3, True)
    log = await delivery(test_db, a, camp, state, NOW)
    log.status = "pending"
    log.telegram_message_id = None
    log.sent_at = None
    log.reservation_token = "one"
    await test_db.commit()
    budget = AccountOutboundBudgetService(test_db)
    context = {"frequency": svc.context(state)}
    reserved = await budget.reserve(
        a.id, attempt_key="ad:one", category="ad", target_key=str(TARGET), context=context, now=NOW
    )
    assert reserved.state == "reserved"
    state.quota = 1
    state.epoch += 1
    await test_db.commit()
    with pytest.raises(OutboundBudgetBlocked, match="frequency_reservation_stale"):
        await budget.mark_attempted("ad:one", account_id=a.id, now=NOW)
    assert reserved.attempted_at is None


async def test_forged_mature_context_cannot_bypass_trial_receipt(test_db):
    a, c, camp, svc, state = await setup(test_db)
    log = await delivery(test_db, a, camp, state, NOW)
    log.status = "pending"
    log.telegram_message_id = None
    log.sent_at = None
    log.reservation_token = "one"
    await test_db.commit()
    context = {"frequency": {**svc.context(state), "lane": "mature"}}
    with pytest.raises(OutboundBudgetBlocked, match="frequency_reservation_mismatch"):
        await AccountOutboundBudgetService(test_db).reserve(
            a.id,
            attempt_key="ad:one",
            category="ad",
            target_key=str(TARGET),
            context=context,
            now=NOW,
        )


async def test_new_mature_send_does_not_wait_for_previous_24h_at_rpc_boundary(test_db):
    a, c, camp, svc, state = await setup(test_db, 30, True)
    previous = await delivery(test_db, a, camp, state, NOW - timedelta(hours=2), status="pending")
    log = await delivery(test_db, a, camp, state, NOW)
    log.status = "pending"
    log.telegram_message_id = None
    log.sent_at = None
    log.reservation_token = "next"
    await test_db.commit()
    budget = AccountOutboundBudgetService(test_db)
    await budget.reserve(
        a.id,
        attempt_key="ad:next",
        category="ad",
        target_key=str(TARGET),
        context={"frequency": svc.context(state)},
        now=NOW,
    )
    row = await budget.mark_attempted("ad:next", account_id=a.id, now=NOW)
    assert row.state == "attempted"
    await budget.finish("ad:next", account_id=a.id, state="unknown", now=NOW)
    previous.survival_status = "survived"
    previous.survival_check_due_at = None
    log.status = "unknown"
    await test_db.commit()
    with pytest.raises(OutboundBudgetBlocked, match="frequency_group_inflight|reconciliation"):
        other = await delivery(test_db, a, camp, state, NOW)
        other.status = "pending"
        other.telegram_message_id = None
        other.sent_at = None
        other.reservation_token = "other"
        await budget.reserve(
            a.id,
            attempt_key="ad:other",
            category="ad",
            target_key=str(TARGET),
            context={"frequency": svc.context(state)},
            now=NOW + timedelta(days=2),
        )


async def test_late_deleted_event_reopens_check_and_unknown_peer_is_ignored(test_db):
    from app.modules.acquisition.adaptive_frequency import queue_deleted_observation

    a, c, camp, svc, state = await setup(test_db, 5, True)
    log = await delivery(test_db, a, camp, state, NOW - timedelta(days=2))
    await svc.observe(log, "survived", NOW)
    await test_db.commit()
    await queue_deleted_observation(
        test_db, a.id, SimpleNamespace(chat_id=None, deleted_ids=[log.telegram_message_id])
    )
    assert log.survival_status == "survived"
    await queue_deleted_observation(
        test_db, a.id, SimpleNamespace(chat_id=TARGET, deleted_ids=[log.telegram_message_id])
    )
    assert log.survival_status == "pending"
    assert log.survival_error == "deletion_event_requires_confirmation"
    assert (await svc.readiness(TARGET, NOW + timedelta(days=1)))[
        0
    ].reason == "frequency_survival_unresolved"


async def test_pending_live_mute_has_exit_evidence_and_blocks_rejoin(test_db):
    from telethon.tl.types import PeerChannel

    from app.core.group.models import Group, GroupAccountMembership
    from app.core.settings_models import SystemSetting
    from app.modules.acquisition.adaptive_frequency import record_live_mute
    from app.modules.acquisition.qualification_join_gate import qualification_join_gate

    a, c, camp, svc, state = await setup(test_db)
    group = Group(group_id=TARGET)
    test_db.add(group)
    await test_db.flush()
    member = GroupAccountMembership(
        account_id=a.id,
        group_id=group.id,
        telegram_group_id=TARGET,
        status="joined",
        joined_at=NOW - timedelta(days=4),
    )
    test_db.add(member)
    log = await delivery(test_db, a, camp, state, NOW)
    log.group_id = group.id
    log.status = "pending"
    log.telegram_message_id = None
    log.sent_at = None
    test_db.add(SystemSetting(key="automation.group_qualification", value='{"enabled":true}'))
    await test_db.commit()
    await record_live_mute(test_db, a.id, TARGET, {})
    assert state.status == "exit_pending"
    assert await svc.exit_reason(a.id, group, member) == "frequency_muted"
    assert await qualification_join_gate(test_db, PeerChannel(123)) == "frequency_rejoin_blocked"


async def test_exit_unknown_must_reconcile_before_repeating(test_db, monkeypatch):
    from unittest.mock import AsyncMock

    from app.core.group.models import Group, GroupAccountMembership
    from app.modules.acquisition import qualification_actions, qualification_service
    from app.modules.acquisition.adaptive_frequency import run_frequency_exits

    a, c, camp, svc, state = await setup(test_db)
    group = Group(group_id=TARGET)
    test_db.add(group)
    await test_db.flush()
    member = GroupAccountMembership(
        account_id=a.id,
        group_id=group.id,
        telegram_group_id=TARGET,
        status="joined",
        joined_at=NOW - timedelta(days=4),
    )
    test_db.add(member)
    log = await delivery(test_db, a, camp, state, NOW - timedelta(days=1))
    log.group_id = group.id
    await svc.observe(log, "deleted", NOW)
    await test_db.commit()
    monkeypatch.setattr(
        qualification_service,
        "authorize_leave",
        AsyncMock(return_value=(True, "frequency_deleted_at_minimum")),
    )
    leave = AsyncMock(return_value="leave_outcome_unknown")
    automation = SimpleNamespace(
        db=test_db, _leave_group=leave, _discovered_group_from_model=lambda g: g
    )
    await run_frequency_exits(automation)
    assert (
        leave.await_count == 1
        and member.status == "joined"
        and member.review_status == "exit_pending"
    )
    member.leave_retry_at = None
    await test_db.commit()
    reconcile = AsyncMock(return_value="unknown")
    monkeypatch.setattr(qualification_actions, "reconcile_exit", reconcile)
    await run_frequency_exits(automation)
    assert leave.await_count == 1 and reconcile.await_count == 1
    member.leave_retry_at = None
    await test_db.commit()
    reconcile.return_value = "left"
    await run_frequency_exits(automation)
    assert leave.await_count == 1 and member.status == "left"


async def test_old_epoch_overdue_evidence_blocks_promotion(test_db):
    a, c, camp, svc, state = await setup(test_db, 3, True)
    await delivery(test_db, a, camp, state, NOW - timedelta(days=2), status="pending", epoch=0)
    for i in range(3):
        log = await delivery(test_db, a, camp, state, NOW - timedelta(days=1, hours=i))
        await svc.observe(log, "survived", NOW)
    assert state.quota == 3


async def test_rule_limited_group_can_mature_without_increasing_rate(test_db):
    a, c, camp, svc, state = await setup(test_db)
    for i in range(3):
        log = await delivery(test_db, a, camp, state, NOW - timedelta(days=4 - i))
        context = json.loads(log.qualification_context_json)
        context["frequency_rule_quota"] = 1
        log.qualification_context_json = json.dumps(context)
        await svc.observe(log, "survived", NOW)
    assert state.quota == 1 and state.mature


async def test_dispatcher_reservation_and_next_schedule_use_mature_group_quota(
    test_db, monkeypatch
):
    from unittest.mock import AsyncMock

    from app.core.group.models import Group
    from app.core.settings_models import SystemSetting
    from app.modules.acquisition import automation as module
    from app.modules.acquisition.automation import AcquisitionAutomationService
    from app.modules.acquisition.models import AdCreative, AdDeliveryScheduleState

    a, c, camp, svc, state = await setup(test_db, 3, True)
    group = Group(group_id=TARGET)
    creative = AdCreative(name="mature", content="查看简介", enabled=True)
    test_db.add_all(
        [
            group,
            creative,
            SystemSetting(key="automation.group_qualification", value='{"enabled":true}'),
        ]
    )
    await test_db.flush()
    service = AcquisitionAutomationService(test_db, account_pool=SimpleNamespace())
    context = {
        "rollout_phase": "dynamic",
        "account_id": a.id,
        "group_type": "supergroup",
        "telegram_group_id": TARGET,
    }
    service._qualified_ad_context = AsyncMock(return_value=(context, None))
    monkeypatch.setattr(module, "_now", lambda: NOW)
    log, reason = await service._claim_growth_campaign_daily_quota(
        campaign=camp, account_id=a.id, group=group, creative=creative
    )
    assert reason is None and log.reservation_token
    frequency = json.loads(log.qualification_context_json)["frequency"]
    assert frequency["lane"] == "mature" and frequency["quota"] == 3
    budget = AccountOutboundBudgetService(test_db)
    key = "ad:" + log.reservation_token
    await budget.reserve(
        a.id,
        attempt_key=key,
        category="ad",
        target_key=str(TARGET),
        context={"frequency": frequency},
        now=NOW,
    )
    await budget.mark_attempted(key, account_id=a.id, now=NOW)
    await budget.finish(key, account_id=a.id, state="succeeded", message_id=998, now=NOW)
    await service._finalize_ad_delivery_log(
        log,
        module.DeliveryStatus.SUCCESS,
        telegram_message_id=998,
        sent_at=NOW,
        survival_required=True,
    )
    schedule = AdDeliveryScheduleState(
        campaign_id=camp.id,
        account_id=a.id,
        group_id=group.id,
        telegram_group_id=TARGET,
        status="sending",
        lock_token="lock",
        next_due_at=NOW,
    )
    test_db.add(schedule)
    await test_db.commit()
    await service._finish_ad_schedule_state(
        schedule.id, "lock", campaign=camp, succeeded=True, reason=None, completed_at=NOW
    )
    assert schedule.next_due_at == NOW + timedelta(hours=8)
    assert log.survival_status == "pending" and log.survival_check_due_at >= NOW + timedelta(
        minutes=2
    )


async def test_survival_worker_requires_two_confirmed_reads_before_downshift(test_db, monkeypatch):
    from unittest.mock import AsyncMock

    from app.modules.acquisition import automation as module
    from app.modules.acquisition.automation import AcquisitionAutomationService

    account, config, campaign, service, state = await setup(test_db, 8, True)
    log = await delivery(
        test_db, account, campaign, state, NOW - timedelta(days=1), status="pending"
    )
    await test_db.commit()
    pool = SimpleNamespace(acquire_by_id=AsyncMock(return_value=object()), release=AsyncMock())
    worker = AcquisitionAutomationService(test_db, account_pool=pool)
    worker._inspect_ad_survival_facts = AsyncMock(
        return_value={
            "group_accessible": True,
            "account_readable": True,
            "member": True,
            "can_send": True,
            "exists": False,
            "ttl_period": 0,
            "errors": [],
        }
    )
    monkeypatch.setattr(module, "_now", lambda: NOW)
    assert await worker._check_one_ad_survival(log, NOW) == "retry_scheduled"
    assert state.quota == 8 and state.status == "active"
    assert (await service.readiness(TARGET, NOW))[0].reason == "frequency_survival_unresolved"
    again = log.survival_check_due_at
    monkeypatch.setattr(module, "_now", lambda: again)
    assert await worker._check_one_ad_survival(log, again) == "deleted"
    assert state.quota == 4 and state.epoch == 2 and log.survival_status == "deleted"
    assert state.pause_until == again + timedelta(days=1)
    assert worker._inspect_ad_survival_facts.await_count == 2
