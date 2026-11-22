"""Local result projections, flushed by the caller's single result transaction."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.core.account.models import AccountOperationConfig, AccountRiskEvent, TelegramAccount
from app.core.account.risk_guard import AccountRiskAction, AccountRiskGuard
from app.core.automation_settings import get_account_risk_guard_settings
from app.modules.acquisition.models import GroupAdProfile


def risk_record(exc: Exception | None, action: str) -> dict | None:
    if exc is None:
        return None
    return {"reason": AccountRiskGuard.classify_error(exc, action=AccountRiskAction(action), target_type="group"),
            "seconds": int(getattr(exc, "seconds", 3600))}


async def risk_failure(db: Any, account_id: int, action: str, exc: Exception | dict | None,
                       target: int, now: datetime) -> None:
    if exc is None:
        return
    action_enum = AccountRiskAction(action)
    reason = (exc["reason"] if isinstance(exc, dict) else
              AccountRiskGuard.classify_error(exc, action=action_enum, target_type="group"))
    # Group permissions are local facts. They never freeze an entire account.
    if reason not in {"flood_wait", "peer_flood", "account_banned", "account_restricted"}:
        return
    account = await db.get(TelegramAccount, account_id, populate_existing=True)
    if account is None:
        return
    options = await get_account_risk_guard_settings(db)
    lifecycle = options.get("lifecycle", {})
    account.last_risk_event_at = account.last_risk_decay_at = now
    account.risk_reason = reason
    if reason == "account_banned":
        account.risk_level, account.risk_score = "quarantined", 100
        account.status = "banned"
        config = await db.scalar(select(AccountOperationConfig).where(AccountOperationConfig.account_id == account_id))
        if config:
            config.enabled = False
    else:
        if reason == "flood_wait":
            wait = exc.get("seconds", 3600) if isinstance(exc, dict) else getattr(exc, "seconds", 3600)
            seconds = int(wait) + int(lifecycle.get("flood_wait_buffer_seconds", 60))
        else:
            seconds = int(lifecycle.get(reason + "_freeze_seconds", 86400))
        account.risk_pause_until = max(account.risk_pause_until or now, now + timedelta(seconds=seconds))
        account.risk_level = "frozen"
        if reason == "account_restricted":
            if account.status != "restricted":
                account.restriction_previous_status = str(getattr(account.status, "value", account.status))
            account.status = "restricted"
            account.restriction_detected_at = now
    db.add(AccountRiskEvent(account_id=account_id, action=action, status="failure", reason=reason,
                            target_type="group", target_id=str(target)))


async def profile_result(db: Any, group_id: int, target: int, quota: int,
                         now: datetime, observation: str | None = None) -> None:
    profile = await db.scalar(select(GroupAdProfile).where(GroupAdProfile.group_id == group_id))
    if profile is None:
        profile = GroupAdProfile(group_id=group_id, telegram_group_id=target,
                                 survival_count=0, deleted_count=0,
                                 consecutive_survivals=0, consecutive_deletions=0)
        db.add(profile)
    profile.daily_capacity = quota
    if observation == "survived":
        profile.survival_count += 1
        profile.consecutive_survivals += 1
        profile.consecutive_deletions = 0
        profile.last_survived_at = now
    elif observation == "deleted":
        profile.deleted_count += 1
        profile.consecutive_deletions += 1
        profile.consecutive_survivals = 0
        profile.last_deleted_at = now
    if observation:
        profile.tier_changed_at = now
