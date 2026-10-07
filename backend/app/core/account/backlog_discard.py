"""Discard pending work for accounts excluded from scheduling by risk control.

A quarantined/frozen/restricted account never enters the dispatch account
sets, so its queued reviews, join reconciliations and delivery slots would
otherwise sit forever and inflate the backlog.  When an account loses
scheduling eligibility through risk control, its accumulated work is dropped
here; ``ensure_membership_reviews`` naturally recreates still-relevant
reviews once the account becomes eligible again, so nothing is lost that the
recovered account still needs.

Every step is idempotent: the periodic sweep re-runs safely and only touches
rows that are still pending.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import select, update

from app.core.settings_models import SystemSetting

logger = structlog.get_logger()

REVIEW_WAIT_PREFIX = "qualification.wait."

SWEEP_LOCK_KEY = "vanguard:backlog_discard:last_sweep"

DISCARDABLE_AUDIT_STATES = ("queued", "running", "waiting_ai", "reviewing_ai")

# Hard risk attributes that exclude an account from scheduling.  Transient
# conditions (risk_pause_until, spam re-checks) deliberately do NOT discard
# backlog: their work should simply wait.
RISK_LEVELS = ("limited", "frozen", "quarantined")
STATUSES = ("banned", "restricted", "error")


def ineligible_account_ids(now: datetime):
    """Account ids excluded from scheduling by hard risk attributes."""
    from app.core.account.models import TelegramAccount

    return select(TelegramAccount.id).where(
        (TelegramAccount.risk_level.in_(RISK_LEVELS))
        | (TelegramAccount.status.in_(STATUSES))
    )


async def discard_account_backlog(
    db: Any,
    account_id: int,
    *,
    reason: str,
    now: datetime | None = None,
) -> dict[str, int]:
    """Drop every pending work item of one risk-controlled account."""
    now = now or datetime.utcnow()
    stats = {
        "audits_cancelled": 0,
        "review_waits_cleared": 0,
        "joins_released": 0,
        "joins_reconcile_stopped": 0,
        "delivery_slots_reset": 0,
    }

    from app.modules.acquisition.models import (
        AdDeliveryScheduleState,
        AdScheduleStatus,
        AutoJoinAttempt,
        DeliveryStatus,
        GroupQualificationAudit,
    )

    audits = (
        await db.scalars(
            select(GroupQualificationAudit).where(
                GroupQualificationAudit.account_id == account_id,
                GroupQualificationAudit.state.in_(DISCARDABLE_AUDIT_STATES),
            )
        )
    ).all()
    for audit in audits:
        audit.state = "cancelled"
        audit.reason = f"account_backlog_discarded:{reason}"[:255]
        audit.next_retry_at = None
    stats["audits_cancelled"] = len(audits)

    if audits:
        stale_waits = (
            await db.scalars(
                select(SystemSetting).where(
                    SystemSetting.key.in_(
                        REVIEW_WAIT_PREFIX + str(audit.id) for audit in audits
                    )
                )
            )
        ).all()
        for wait in stale_waits:
            await db.delete(wait)
        stats["review_waits_cleared"] = len(stale_waits)

    reserved = (
        await db.scalars(
            select(AutoJoinAttempt).where(
                AutoJoinAttempt.account_id == account_id,
                AutoJoinAttempt.request_state == "reserved",
                AutoJoinAttempt.telegram_action_attempted.is_(False),
            )
        )
    ).all()
    for attempt in reserved:
        attempt.request_state = "released"
        attempt.reservation_released_at = now
        attempt.reservation_expires_at = None
        attempt.status = DeliveryStatus.SKIPPED.value
        attempt.reason = f"account_backlog_discarded:{reason}"[:255]
    stats["joins_released"] = len(reserved)

    outstanding = await db.execute(
        update(AutoJoinAttempt)
        .where(
            AutoJoinAttempt.account_id == account_id,
            AutoJoinAttempt.reconciliation_status.not_in(["confirmed", "resolved"]),
            AutoJoinAttempt.request_state.in_(["sent", "outcome_unknown"]),
        )
        .values(
            reconciliation_status="resolved",
            reconciliation_next_at=None,
            reconciliation_checked_at=now,
        )
    )
    stats["joins_reconcile_stopped"] = outstanding.rowcount or 0

    slots = await db.execute(
        update(AdDeliveryScheduleState)
        .where(
            AdDeliveryScheduleState.account_id == account_id,
            AdDeliveryScheduleState.status.in_(
                [AdScheduleStatus.PENDING.value, AdScheduleStatus.SENDING.value,
                 AdScheduleStatus.RETRY.value]
            ),
        )
        .values(
            status=AdScheduleStatus.IDLE.value,
            lock_token=None,
            lease_expires_at=None,
            last_reason=f"account_backlog_discarded:{reason}"[:255],
            updated_at=now,
        )
    )
    stats["delivery_slots_reset"] = slots.rowcount or 0

    await db.commit()
    return stats


async def discard_ineligible_account_backlog(
    db: Any,
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> dict[str, int]:
    """Sweep every currently risk-excluded account that still holds backlog.

    Invoked periodically from the dispatch tick so accounts quarantined while
    workers were down, or by operators, are cleaned without manual scripts.
    """
    now = now or datetime.utcnow()
    totals = {
        "accounts_swept": 0,
        "audits_cancelled": 0,
        "review_waits_cleared": 0,
        "joins_released": 0,
        "joins_reconcile_stopped": 0,
        "delivery_slots_reset": 0,
    }
    account_rows = (
        await db.scalars(ineligible_account_ids(now).limit(limit))
    ).all()
    for account_id in account_rows:
        stats = await discard_account_backlog(db, account_id, reason="account_risk_ineligible", now=now)
        if any(stats[key] for key in stats if key != "accounts_swept"):
            totals["accounts_swept"] += 1
        for key in totals:
            if key != "accounts_swept":
                totals[key] += stats.get(key, 0)
    return totals


async def maybe_sweep_ineligible_backlog(
    db: Any,
    client: Any,
    *,
    now: datetime | None = None,
    interval_seconds: int = 600,
) -> dict[str, int]:
    """Rate-limited sweep hook for the dispatch tick; never raises."""
    now = now or datetime.utcnow()
    if client is not None:
        try:
            if not await client.set(SWEEP_LOCK_KEY, "1", nx=True, ex=interval_seconds):
                return {}
        except Exception as exc:
            logger.warning("backlog_sweep_gate_failed", error_type=type(exc).__name__)
            return {}
    try:
        totals = await discard_ineligible_account_backlog(db, now=now)
    except Exception as exc:
        await db.rollback()
        logger.warning(
            "backlog_sweep_failed", error_type=type(exc).__name__, error=str(exc)[:200]
        )
        return {}
    if totals.get("accounts_swept"):
        logger.info("backlog_swept_for_risk_accounts", **totals)
    return totals
