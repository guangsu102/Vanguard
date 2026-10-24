"""One read-only warmup decision for inventory, selection and the write boundary."""

from datetime import datetime, timedelta
from typing import Any

from app.core.account.models import TelegramAccount
from app.core.account.warmup import (
    account_warmup_block_reason,
    account_warmup_context,
    account_warmup_days,
)
from app.core.automation_constants import AD_MIN_WARMUP_DAYS
from app.core.automation_settings import get_account_warmup_policy_settings
from app.modules.acquisition.ad_readiness import AdReadiness


def readiness(policy: dict, account: Any, member: Any, now: datetime) -> AdReadiness:
    context = account_warmup_context(policy, account, now, action="ad_delivery")
    managed_started_at = getattr(account, "managed_started_at", None)
    reason = (
        account_warmup_block_reason(context, "ad_delivery")
        if managed_started_at is not None else None
    )
    # A missing managed start is legacy data, not evidence that operation began
    # at the row creation time.  The write boundary can enforce the configured
    # account warmup once the durable managed_started_at marker exists.
    account_due = (
        managed_started_at + timedelta(days=context.warmup_days)
        if managed_started_at else now
    )
    # Use the original membership start, never the time of this evaluation.
    start = member.interaction_started_at or member.joined_at or member.created_at
    configured_group_days = max(AD_MIN_WARMUP_DAYS, account_warmup_days(policy, account))
    configured_group_due = None
    if start is not None:
        configured_group_due = start + timedelta(days=configured_group_days)
    group_due = member.first_ad_allowed_at
    if configured_group_days == 0:
        # A policy reduction to zero days must take effect for existing rows as
        # well.  ``first_ad_allowed_at`` is a cached deadline from the former
        # policy; retaining a future value here would make a zero-day release
        # appear successful in settings while still blocking the send gate.
        group_due = configured_group_due or now
    elif group_due is None:
        if start is None:
            return AdReadiness("ad_warmup_start_unknown")
        group_due = configured_group_due
    if reason or group_due > now:
        due = max(account_due, group_due)
        return AdReadiness(reason or "ad_warmup_period", due if due > now else None)
    return AdReadiness()


async def warmup_readiness(db: Any, account_id: int, member: Any, now: datetime) -> AdReadiness:
    account = await db.get(TelegramAccount, account_id, populate_existing=True)
    return readiness(await get_account_warmup_policy_settings(db), account, member, now)
