"""Exercise real authorization and namespace resolution for legacy schedules."""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.account.models import AccountOperationConfig
from app.modules.acquisition.adaptive_frequency import FrequencyService
from app.modules.acquisition.automation import AcquisitionAutomationService
from app.modules.acquisition.models import AdCampaign, AdDeliveryScheduleState
from tests.unit.test_qualification_service import setup

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("case,hours", [
    ("verified", 12), ("unknown", 24), ("expired", 24), ("rule_cap", 24),
    ("chat_namespace", 24), ("disabled", 24),
])
async def test_success_schedule_resolves_current_authorization(test_db, case, hours):
    account, group, member, review = await setup(test_db)
    now = datetime.utcnow()
    campaign = AdCampaign(name="growth", enabled=True, delivery_policy="growth")
    config = AccountOperationConfig(
        account_id=account.id, enabled=True, auto_ads_enabled=True,
        dynamic_capacity_enabled=True, adaptive_ads_enabled=case != "disabled",
        operation_mode="growth",
    )
    test_db.add_all([campaign, config])
    frequency = await FrequencyService(test_db).state(
        group.group_id, {"group_type": "supergroup"}, create=True, now=now,
    )
    frequency.quota, frequency.mature = 2, True
    evidence = json.loads(review.evidence_json)
    if case == "unknown":
        evidence.pop("group_type")
    elif case == "chat_namespace":
        evidence["group_type"] = "basic_group"
    elif case == "rule_cap":
        evidence["frequency_rule_quota"] = 1
    elif case == "expired":
        review.expires_at = now - timedelta(seconds=1)
    review.evidence_json = json.dumps(evidence)
    await test_db.flush()
    schedule = AdDeliveryScheduleState(
        campaign_id=campaign.id, account_id=account.id, group_id=group.id,
        telegram_group_id=group.group_id, status="sending", lock_token="receipt-owner",
        next_due_at=now, lease_expires_at=now + timedelta(minutes=5),
    )
    test_db.add(schedule)
    await test_db.commit()
    service = AcquisitionAutomationService(test_db, account_pool=SimpleNamespace())
    await service._finish_ad_schedule_state(
        schedule.id, "receipt-owner", campaign=campaign,
        succeeded=True, reason=None, completed_at=now,
    )
    assert schedule.next_due_at == now + timedelta(hours=hours)
    assert schedule.last_success_at == now and schedule.status == "idle"
    assert schedule.lock_token is None and schedule.lease_expires_at is None


async def test_schedule_deadline_waits_for_both_lease_and_due_time():
    from app.modules.acquisition.ad_readiness import schedule_readiness

    now = datetime.utcnow()
    for lease, due in [(2, 5), (5, 2)]:
        schedule = SimpleNamespace(
            status="sending", lease_expires_at=now + timedelta(hours=lease),
            next_due_at=now + timedelta(hours=due),
        )
        ready = schedule_readiness(schedule, now)
        assert ready.reason == "delivery_tuple_inflight"
        assert ready.next_allowed_at == now + timedelta(hours=5)


async def repair_case(db):
    from app.modules.acquisition.models import AccountAdBinding, AdCreative, AdDeliveryLog

    account, group, member, review = await setup(db)
    now = datetime.utcnow()
    sent = now - timedelta(hours=13)
    campaign = AdCampaign(name="growth", enabled=True, delivery_policy="growth")
    creative = AdCreative(name="profile", content="profile", enabled=True)
    config = AccountOperationConfig(
        account_id=account.id, enabled=True, auto_ads_enabled=True,
        dynamic_capacity_enabled=True, adaptive_ads_enabled=True, operation_mode="growth",
    )
    db.add_all([campaign, creative, config])
    frequency = await FrequencyService(db).state(group.group_id, {"group_type": "supergroup"}, create=True, now=sent)
    frequency.quota, frequency.mature = 2, True
    await db.flush()
    db.add(AccountAdBinding(account_id=account.id, ad_campaign_id=campaign.id, creative_id=creative.id, enabled=True))
    log = AdDeliveryLog(
        account_id=account.id, ad_campaign_id=campaign.id, group_id=group.id,
        telegram_group_id=group.group_id, status="success", telegram_message_id=42,
        sent_at=sent, created_at=sent, survival_stage="daily", survival_status="not_required",
        qualification_context_json=json.dumps({"group_type": "supergroup", "frequency": FrequencyService.context(frequency)}),
    )
    schedule = AdDeliveryScheduleState(
        campaign_id=campaign.id, account_id=account.id, group_id=group.id,
        telegram_group_id=group.group_id, status="idle", last_success_at=sent,
        last_attempt_at=sent, next_due_at=sent + timedelta(hours=24), updated_at=sent,
    )
    db.add_all([log, schedule])
    await db.commit()
    return now, config, review, frequency, log, schedule


async def test_repair_preview_apply_idempotency_and_rollback(test_db):
    from app.modules.acquisition.ad_readiness import due_membership_query
    from app.modules.acquisition.schedule_repair import (
        apply_legacy_schedule_plan,
        legacy_schedule_plan,
        rollback_legacy_schedule_plan,
    )

    now, config, review, frequency, log, schedule = await repair_case(test_db)
    old_due = schedule.next_due_at
    plan = await legacy_schedule_plan(test_db, config.account_id, now)
    assert len(plan) == 1 and not test_db.dirty
    assert schedule.next_due_at == old_due
    assert not (await test_db.scalars(due_membership_query(config.account_id, schedule.campaign_id, now))).all()
    assert await apply_legacy_schedule_plan(test_db, config.account_id, now, plan) == 1
    await test_db.commit()
    assert schedule.next_due_at == log.sent_at + timedelta(hours=12)
    assert len((await test_db.scalars(due_membership_query(config.account_id, schedule.campaign_id, now))).all()) == 1
    assert await legacy_schedule_plan(test_db, config.account_id, now) == []
    with pytest.raises(ValueError, match="plan changed"):
        await apply_legacy_schedule_plan(test_db, config.account_id, now, plan)
    assert await rollback_legacy_schedule_plan(test_db, plan, now) == 1
    await test_db.commit()
    assert schedule.next_due_at == old_due
    assert await legacy_schedule_plan(test_db, config.account_id, now) == plan


@pytest.mark.parametrize("case", [
    "paused", "sending", "retry", "lease", "missing_receipt", "unknown_receipt",
    "unknown_namespace", "expired", "disabled", "rule_cap", "different_due",
    "later_attempt", "frequency_blocked", "daily_review_unknown", "probe",
])
async def test_repair_preserves_other_blocks(test_db, case):
    from app.modules.acquisition.schedule_repair import legacy_schedule_plan

    now, config, review, frequency, log, schedule = await repair_case(test_db)
    if case in {"paused", "sending", "retry"}:
        schedule.status = case
    elif case == "lease":
        schedule.lock_token = "another-worker"
    elif case == "missing_receipt":
        log.telegram_message_id = None
    elif case == "unknown_receipt":
        log.status = "unknown"
    elif case == "unknown_namespace":
        review.evidence_json = json.dumps({**json.loads(review.evidence_json), "group_type": None})
    elif case == "expired":
        review.expires_at = now - timedelta(seconds=1)
    elif case == "disabled":
        config.auto_ads_enabled = False
    elif case == "rule_cap":
        review.evidence_json = json.dumps({**json.loads(review.evidence_json), "frequency_rule_quota": 1})
    elif case == "different_due":
        schedule.next_due_at += timedelta(seconds=1)
    elif case == "later_attempt":
        schedule.last_attempt_at = now
    elif case == "frequency_blocked":
        frequency.status = "blocked"
    elif case == "daily_review_unknown":
        frequency.daily_review_due_at = now - timedelta(hours=1)
        frequency.daily_review_error = "survival_read_unknown"
    elif case == "probe":
        frequency.mature = False
    await test_db.commit()
    assert await legacy_schedule_plan(test_db, config.account_id, now) == []


async def test_repair_rejects_stale_plan_and_rollback_after_new_activity(test_db):
    from app.modules.acquisition.schedule_repair import (
        apply_legacy_schedule_plan,
        legacy_schedule_plan,
        rollback_legacy_schedule_plan,
    )

    now, config, review, frequency, log, schedule = await repair_case(test_db)
    plan = await legacy_schedule_plan(test_db, config.account_id, now)
    frequency.quota = 4
    await test_db.commit()
    with pytest.raises(ValueError, match="plan changed"):
        await apply_legacy_schedule_plan(test_db, config.account_id, now, plan)
    plan = await legacy_schedule_plan(test_db, config.account_id, now)
    await apply_legacy_schedule_plan(test_db, config.account_id, now, plan)
    schedule.last_success_at = now
    await test_db.commit()
    with pytest.raises(ValueError, match="preserve live state"):
        await rollback_legacy_schedule_plan(test_db, plan, now)
