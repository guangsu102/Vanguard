"""Apply an exact Telegram deletion fact without performing another read."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import select

from app.core.group.models import GroupAccountMembership
from app.modules.acquisition.adaptive_frequency import (
    DAY,
    FrequencyService,
    canonical,
    frequency_context,
    payload,
)
from app.modules.acquisition.models import AdDeliveryLog, GroupAdFrequencyEvent


async def apply_confirmed_deletion(db: Any, log: AdDeliveryLog, now: datetime) -> bool:
    """Caller holds the log lock and has matched account, peer namespace and ID.

    Queue durable exit intent; the existing exit worker still checks protections
    and reconciles uncertain leave results before performing external actions.
    """
    context = payload(log.qualification_context_json)
    fc = frequency_context(log)
    key = canonical(log.telegram_group_id, context)
    if (
        not fc
        or key is None
        or log.status != "success"
        or not log.sent_at
        or not log.telegram_message_id
    ):
        return False
    state = await FrequencyService(db).state(key, context, lock=True)
    if state is None:
        return False
    existing = await db.scalar(
        select(GroupAdFrequencyEvent.id).where(
            GroupAdFrequencyEvent.log_id == log.id,
            GroupAdFrequencyEvent.kind.in_(["deleted", "daily_deleted"]),
        )
    )
    if existing is not None:
        return True
    member = await db.scalar(
        select(GroupAccountMembership)
        .where(
            GroupAccountMembership.account_id == log.account_id,
            GroupAccountMembership.group_id == log.group_id,
        )
        .with_for_update(of=GroupAccountMembership)
        .execution_options(populate_existing=True)
    )
    # A delayed fact can describe an old message, but cannot punish a rejoin or
    # advance a newer frequency cycle. Record the message fact independently.
    current = bool(
        state.status == "active"
        and fc.get("epoch") == state.epoch
        and member is not None
        and member.status == "joined"
        and member.left_at is None
        and member.joined_at is not None
        and log.sent_at >= member.joined_at
    )
    old_quota = state.quota
    if current:
        trial = fc.get("lane") == "probe" or not state.mature
        state.quota = max(1, old_quota // 2)
        state.reason = "frequency_daily_deleted"
        if trial or old_quota == 1:
            state.status = "exit_pending"
            state.reason = "frequency_probe_deleted" if trial else "frequency_deleted_at_minimum"
            member.ad_status, member.review_status = "blocked", "exit_pending"
            member.leave_requested_at = member.leave_requested_at or now
            member.review_next_at = now
        state.epoch += 1
        state.epoch_started_at = now
        state.promote_after = now + DAY
        state.daily_review_due_at = None
        state.daily_review_retry_at = None
        state.daily_review_error = None
        state.daily_review_token = state.daily_review_expires_at = None
        state.daily_review_checked_at = now
        state.updated_at = now
    context["deletion_event"] = {
        "source": "telegram_delete_update",
        "account_id": log.account_id,
        "telegram_group_id": key,
        "message_id": log.telegram_message_id,
        "observed_at": now.isoformat(),
        "applied_to_cycle": current,
    }
    log.qualification_context_json = json.dumps(context, ensure_ascii=False)
    log.survival_status, log.survival_stage = "deleted", "complete"
    log.survival_checked_at = now
    log.survival_error = "telegram_deletion_event"
    log.survival_check_due_at = None
    log.survival_claim_token = log.survival_claim_expires_at = None
    log.survival_version = (log.survival_version or 0) + 1
    db.add(
        GroupAdFrequencyEvent(
            telegram_group_id=key,
            log_id=log.id,
            kind="deleted",
            epoch=int(fc["epoch"]),
            old_quota=old_quota,
            new_quota=state.quota,
            created_at=now,
        )
    )
    await db.flush()
    return True
