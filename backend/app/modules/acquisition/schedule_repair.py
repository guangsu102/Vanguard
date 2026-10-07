"""Bounded, explicit repair of the legacy positive-peer 24-hour fallback.

No commits, configuration changes or Telegram calls. The caller owns the
transaction and must retain the returned before/after journal before applying.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select

from app.core.account.models import AccountOperationConfig
from app.modules.acquisition.adaptive_frequency import (
    FrequencyService,
    canonical,
    interval_seconds,
    payload,
    rule_quota,
)
from app.modules.acquisition.automation import AcquisitionAutomationService
from app.modules.acquisition.models import (
    AccountAdBinding,
    AdDeliveryLog,
    AdDeliveryScheduleState,
)


async def legacy_schedule_plan(
    db: Any, account_id: int, now: datetime, *, lock: bool = False,
) -> list[dict]:
    config = await db.scalar(select(AccountOperationConfig).where(
        AccountOperationConfig.account_id == account_id,
    ).execution_options(populate_existing=True))
    if not (config and config.enabled and config.auto_ads_enabled
            and config.dynamic_capacity_enabled and config.adaptive_ads_enabled
            and config.operation_mode == "growth"):
        return []
    query = select(AdDeliveryScheduleState).where(
        AdDeliveryScheduleState.account_id == account_id,
        AdDeliveryScheduleState.telegram_group_id > 0,
        AdDeliveryScheduleState.status == "idle",
        AdDeliveryScheduleState.next_due_at > now,
    ).order_by(AdDeliveryScheduleState.id).limit(301).execution_options(populate_existing=True)
    if lock:
        query = query.with_for_update(of=AdDeliveryScheduleState)
    schedules = list((await db.scalars(query)).unique().all())
    if len(schedules) > 300:
        raise ValueError("schedule repair exceeds bounded scope")
    service = AcquisitionAutomationService(db, account_pool=SimpleNamespace())
    frequency_service = FrequencyService(db)
    result = []
    for schedule in schedules:
        sent = schedule.last_success_at
        if (not sent or sent > now or schedule.lock_token or schedule.lease_expires_at
                or schedule.last_reason or (schedule.last_attempt_at and schedule.last_attempt_at > sent)
                or schedule.next_due_at != sent + timedelta(hours=24)):
            continue
        campaign = schedule.campaign
        if (not campaign.enabled or campaign.delivery_policy != "growth"
                or (campaign.start_at and campaign.start_at > now)
                or (campaign.end_at and campaign.end_at < now)):
            continue
        binding = await db.scalar(select(AccountAdBinding.id).where(
            AccountAdBinding.account_id == account_id,
            AccountAdBinding.ad_campaign_id == campaign.id,
            AccountAdBinding.enabled.is_(True),
        ).limit(1))
        if binding is None:
            continue
        context, reason = await service._qualified_ad_context(account_id, schedule.telegram_group_id, now)
        if context is None or reason is not None:
            continue
        frequency = await frequency_service.state(schedule.telegram_group_id, context, lock=lock)
        if not (frequency and frequency.mature and frequency.status == "active"):
            continue
        quota = min(frequency.quota, rule_quota(context))
        if quota <= 1:
            continue
        logs = list((await db.scalars(select(AdDeliveryLog).where(
            AdDeliveryLog.account_id == account_id,
            AdDeliveryLog.group_id == schedule.group_id,
            AdDeliveryLog.ad_campaign_id == campaign.id,
            AdDeliveryLog.status == "success",
            AdDeliveryLog.sent_at == sent,
            AdDeliveryLog.telegram_message_id.is_not(None),
        ))).all())
        if len(logs) != 1:
            continue
        log = logs[0]
        if canonical(log.telegram_group_id, payload(log.qualification_context_json)) != frequency.telegram_group_id:
            continue
        ready, _ = await frequency_service.readiness(schedule.telegram_group_id, now, context=context)
        if ready.reason not in {None, "frequency_group_interval", "frequency_group_daily_cap"}:
            continue
        due = max(sent + timedelta(seconds=interval_seconds(quota)), ready.next_allowed_at or sent)
        if due >= schedule.next_due_at:
            continue
        result.append({
            "schedule_id": schedule.id, "account_id": account_id,
            "group_id": schedule.group_id, "campaign_id": campaign.id,
            "frequency_peer_id": frequency.telegram_group_id, "quota": quota,
            "receipt_id": log.id, "last_success_at": sent.isoformat(),
            "old_due": schedule.next_due_at.isoformat(), "new_due": due.isoformat(),
            "old_updated_at": schedule.updated_at.isoformat(),
        })
    return result


async def apply_legacy_schedule_plan(
    db: Any, account_id: int, now: datetime, expected: list[dict],
) -> int:
    actual = await legacy_schedule_plan(db, account_id, now, lock=True)
    if actual != expected:
        raise ValueError("schedule repair plan changed; preview again")
    for item in actual:
        schedule = await db.get(AdDeliveryScheduleState, item["schedule_id"])
        schedule.next_due_at = datetime.fromisoformat(item["new_due"])
        schedule.updated_at = now
    await db.flush()
    return len(actual)


async def rollback_legacy_schedule_plan(
    db: Any, journal: list[dict], applied_at: datetime,
) -> int:
    rows = []
    for item in journal:
        schedule = await db.scalar(select(AdDeliveryScheduleState).where(
            AdDeliveryScheduleState.id == item["schedule_id"],
        ).with_for_update(of=AdDeliveryScheduleState).execution_options(populate_existing=True))
        if (schedule is None or schedule.status != "idle" or schedule.lock_token
                or schedule.lease_expires_at or schedule.last_reason
                or schedule.account_id != item["account_id"]
                or schedule.group_id != item["group_id"]
                or schedule.campaign_id != item["campaign_id"]
                or schedule.last_success_at != datetime.fromisoformat(item["last_success_at"])
                or schedule.next_due_at != datetime.fromisoformat(item["new_due"])
                or schedule.updated_at != applied_at):
            raise ValueError("schedule changed after repair; preserve live state")
        rows.append((schedule, item))
    for schedule, item in rows:
        schedule.next_due_at = datetime.fromisoformat(item["old_due"])
        schedule.updated_at = datetime.fromisoformat(item["old_updated_at"])
    await db.flush()
    return len(rows)
