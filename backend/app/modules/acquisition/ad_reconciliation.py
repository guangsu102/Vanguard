"""Durable fencing for advertisement sends with an unknown Telegram outcome.

The outbound ledger is account-wide.  A single ``attempted``/``unknown`` ad
write therefore has to fence every matching campaign schedule until the
ledger is explicitly reconciled.  Keeping this small piece of state handling
outside the dispatcher makes the fence available to both the normal delivery
path and the receipt-recovery path.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.core.account.models import AccountOutboundAttempt
from app.core.group.identity import telegram_group_id_aliases
from app.modules.acquisition.models import (
    AdCampaign,
    AdDeliveryLog,
    AdDeliveryScheduleState,
    AdScheduleStatus,
)

RECONCILIATION_REASON_PREFIX = "outbound_target_reconciliation_required"


def is_reconciliation_reason(reason: str | None) -> bool:
    return str(reason or "").startswith(RECONCILIATION_REASON_PREFIX)


def _target_keys(value: int | str | None) -> set[str]:
    if value is None:
        return set()
    try:
        aliases = telegram_group_id_aliases(int(value))
    except (TypeError, ValueError):
        return {str(value)}
    return {str(item) for item in aliases} or {str(value)}


async def unresolved_ad_attempt_id(
    db: Any,
    account_id: int,
    telegram_group_id: int,
) -> int | None:
    """Return the oldest unresolved ad ledger row for this account/target."""

    keys = _target_keys(telegram_group_id)
    if not keys:
        return None
    return await db.scalar(
        select(AccountOutboundAttempt.id)
        .where(
            AccountOutboundAttempt.account_id == account_id,
            AccountOutboundAttempt.category == "ad",
            AccountOutboundAttempt.target_key.in_(keys),
            AccountOutboundAttempt.state.in_(["attempted", "unknown"]),
        )
        .order_by(AccountOutboundAttempt.id)
        .limit(1)
    )


async def pause_unresolved_ad_schedules(
    db: Any,
    now: datetime,
    *,
    account_id: int | None = None,
) -> int:
    """Pause every schedule fenced by an unresolved outbound ledger row.

    This is idempotent and deliberately clears a stale schedule lease.  The
    account-wide ledger fence remains the source of truth, so clearing a lease
    cannot permit a second Telegram write.
    """

    filters = [
        AccountOutboundAttempt.category == "ad",
        AccountOutboundAttempt.state.in_(["attempted", "unknown"]),
    ]
    if account_id is not None:
        filters.append(AccountOutboundAttempt.account_id == account_id)
    attempts = (
        await db.execute(
            select(AccountOutboundAttempt.id, AccountOutboundAttempt.account_id,
                   AccountOutboundAttempt.target_key)
            .where(*filters)
            .order_by(AccountOutboundAttempt.id)
        )
    ).all()

    changed = 0
    seen: set[tuple[int, str]] = set()
    for attempt_id, attempt_account_id, target_key in attempts:
        target_keys = _target_keys(target_key)
        if not target_keys:
            continue
        dedupe_key = (int(attempt_account_id), ",".join(sorted(target_keys)))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        states = (
            await db.execute(
                select(AdDeliveryScheduleState)
                .where(
                    AdDeliveryScheduleState.account_id == attempt_account_id,
                    AdDeliveryScheduleState.telegram_group_id.in_(
                        [int(item) for item in target_keys if item.lstrip("-").isdigit()]
                    ),
                )
                .with_for_update(of=AdDeliveryScheduleState)
            )
        ).scalars().all()
        for state in states:
            desired_reason = f"{RECONCILIATION_REASON_PREFIX}:{int(attempt_id)}"
            if (
                state.status == AdScheduleStatus.PAUSED.value
                and is_reconciliation_reason(state.last_reason)
                and state.last_reason == desired_reason
                and state.lock_token is None
                and state.lease_expires_at is None
            ):
                continue
            state.status = AdScheduleStatus.PAUSED.value
            state.next_due_at = now
            state.lock_token = None
            state.lease_expires_at = None
            state.last_reason = desired_reason[:255]
            state.updated_at = now
            changed += 1
    if changed:
        await db.commit()
    return changed


async def wake_schedule_after_reconciliation(
    db: Any,
    *,
    attempt_key: str,
    account_id: int,
    outcome: str,
    now: datetime,
) -> int:
    """Wake schedules after a confirmed terminal ledger outcome.

    A successful original write receives the normal next schedule deadline;
    a confirmed failed write is retried at the dispatcher interval.  Unknown
    outcomes never enter this function and therefore remain fenced.
    """

    if outcome not in {"succeeded", "failed"} or not attempt_key.startswith("ad:"):
        return 0
    token = attempt_key[3:]
    log = await db.scalar(
        select(AdDeliveryLog)
        .where(
            AdDeliveryLog.account_id == account_id,
            AdDeliveryLog.reservation_token == token,
        )
        .order_by(AdDeliveryLog.id.desc())
        .limit(1)
    )
    target_keys = _target_keys(log.telegram_group_id if log else None)
    if not target_keys:
        attempt = await db.scalar(
            select(AccountOutboundAttempt).where(
                AccountOutboundAttempt.account_id == account_id,
                AccountOutboundAttempt.attempt_key == attempt_key,
            )
        )
        target_keys = _target_keys(attempt.target_key if attempt else None)
    if not target_keys:
        return 0

    states = (
        await db.execute(
            select(AdDeliveryScheduleState)
            .where(
                AdDeliveryScheduleState.account_id == account_id,
                AdDeliveryScheduleState.telegram_group_id.in_(
                    [int(item) for item in target_keys if item.lstrip("-").isdigit()]
                ),
                *(
                    [AdDeliveryScheduleState.campaign_id == log.ad_campaign_id]
                    if log is not None
                    else []
                ),
            )
            .with_for_update(of=AdDeliveryScheduleState)
        )
    ).scalars().all()
    blocked = [state for state in states if is_reconciliation_reason(state.last_reason)]
    if not blocked:
        return 0

    due_by_campaign: dict[int, datetime] = {}
    if outcome == "succeeded":
        from app.modules.acquisition.automation import AcquisitionAutomationService

        service = AcquisitionAutomationService(db)
        for state in blocked:
            campaign = await db.get(AdCampaign, state.campaign_id)
            if campaign is not None:
                due_by_campaign[state.campaign_id] = await service._next_ad_schedule_due_at(
                    campaign, now
                )
    else:
        from app.core.automation_settings import get_ad_delivery_execution_settings

        execution = await get_ad_delivery_execution_settings(db)
        retry_at = now + timedelta(
            seconds=max(30, int(execution.get("dispatcher_interval_seconds") or 60))
        )
        due_by_campaign = {state.campaign_id: retry_at for state in blocked}

    changed = 0
    for state in blocked:
        state.status = (
            AdScheduleStatus.IDLE.value
            if outcome == "succeeded"
            else AdScheduleStatus.RETRY.value
        )
        state.next_due_at = due_by_campaign.get(state.campaign_id, now)
        state.lock_token = None
        state.lease_expires_at = None
        state.last_reason = None if outcome == "succeeded" else "reconciliation_resolved_failed"
        state.updated_at = now
        if outcome == "succeeded" and log is not None:
            state.last_success_at = log.sent_at or now
        changed += 1
    await db.commit()
    return changed
