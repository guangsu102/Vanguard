"""Bounded, native observation gate; never sends advertising or replays work."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.operating_time import operating_day_start

from app.core.account.models import AccountOperationConfig, TelegramAccount
from app.core.settings_models import SystemSetting
from app.modules.acquisition.models import AdDeliveryLog, AutoJoinAttempt


async def observe(db: AsyncSession, account_id: int) -> None:
    if account_id not in {2, 33, 34, 35}:
        return
    canary = await db.get(SystemSetting, f"growth.canary.account.{account_id}")
    if not canary:
        return
    policy = json.loads(canary.value)
    if not policy.get("enabled") or not policy.get("send_result_started_at"):
        return
    now = datetime.utcnow()
    started = datetime.fromisoformat(policy["send_result_started_at"])
    if now < started + timedelta(hours=24):
        return
    marker = await db.get(SystemSetting, "growth.send_result.cutover.v1", with_for_update=True)
    if not marker:
        return
    state = json.loads(marker.value)
    account_state = state if account_id == 2 else state.get("account_observations", {}).get(str(account_id), {})
    if account_state.get("observation_last_check") and datetime.fromisoformat(account_state["observation_last_check"]) > now - timedelta(minutes=5):
        return
    account = await db.get(TelegramAccount, account_id)
    config = await db.scalar(select(AccountOperationConfig).where(AccountOperationConfig.account_id == account_id))
    groups = await db.scalar(select(func.count(func.distinct(AdDeliveryLog.group_id))).where(
        AdDeliveryLog.account_id == account_id, AdDeliveryLog.status == "success", AdDeliveryLog.created_at >= started)) or 0
    interrupted = await db.scalar(select(func.count(AdDeliveryLog.id)).where(
        AdDeliveryLog.account_id == account_id, AdDeliveryLog.status == "sending",
        AdDeliveryLog.created_at < now - timedelta(minutes=5))) or 0
    from collections import Counter
    from app.core.operating_time import operating_date
    records = list((await db.scalars(select(AdDeliveryLog).where(
        AdDeliveryLog.account_id == account_id, AdDeliveryLog.created_at >= operating_day_start(started)))).all())
    seen = set()
    duplicate = False
    usage = Counter()
    for row in records:
        if row.status in {"success", "unknown", "sending"}:
            usage[operating_date(row.created_at)] += 1
        if row.status == "success" and row.telegram_message_id:
            key = (row.telegram_group_id, row.telegram_message_id)
            duplicate |= key in seen
            seen.add(key)
    # Join admission uses UTC days; advertising uses the configured operating day.
    join_records = list((await db.scalars(select(AutoJoinAttempt.attempted_at).where(
        AutoJoinAttempt.account_id == account_id, AutoJoinAttempt.telegram_action_attempted.is_(True),
        AutoJoinAttempt.attempted_at >= started.replace(hour=0, minute=0, second=0, microsecond=0)))).all())
    join_usage = Counter(at.date() for at in join_records)
    reason = None
    if not config or not config.enabled or not config.auto_ads_enabled:
        reason = "manual_or_operation_pause"
    elif groups < 3:
        reason = "need_success_in_three_groups"
    elif duplicate or any(value > int(policy.get("ad_daily", 10)) for value in usage.values()):
        reason = "duplicate_or_observation_quota_exceeded"
    elif any(value > int(policy.get("join_daily", 10)) for value in join_usage.values()):
        reason = "observation_join_quota_exceeded"
    elif interrupted:
        reason = "unsettled_send_records"
    elif account.spam_check_status != "clear" or not account.spam_checked_at or account.spam_checked_at < now - timedelta(hours=24):
        reason = "need_fresh_official_clear"
    elif account.status in {"restricted", "banned", "error"} or account.risk_level not in {"normal", "watch"}:
        reason = "account_risk_active"
    elif account.last_risk_event_at and account.last_risk_event_at > started:
        reason = "new_account_risk_event"
    elif account.risk_reason != policy.get("observed_risk_reason", state.get("observed_risk_reason")) and account.risk_reason not in {None, "", "spambot_clear", "official_clear"}:
        reason = "risk_reason_changed"
    elif account.risk_pause_until and account.risk_pause_until > now:
        reason = "account_risk_pause"
    elif account.risk_recovery_until and account.risk_recovery_until > now:
        reason = "account_recovery_window_active"
    account_state = state if account_id == 2 else state.setdefault("account_observations", {}).setdefault(str(account_id), {})
    account_state.update(observation_last_check=now.isoformat(), observation_blocker=reason)
    if reason is None:
        policy.update(enabled=False, completed_at=now.isoformat())
        canary.value = json.dumps(policy)
        if account.risk_level == "watch":
            account.risk_level = "normal"
            account.risk_recovery_until = None
        account_state["observation_completed_at"] = now.isoformat()
    policy.update(blocker=reason, distinct_success_groups=groups)
    canary.value = json.dumps(policy)
    marker.value = json.dumps(state)
    await db.commit()
    if reason == "need_fresh_official_clear":
        from app.modules.account_spam.service import enqueue_automatic_spam_check
        await enqueue_automatic_spam_check(db, account_id=account_id)
