"""Checkpoint deadlines are distinct from retry scheduling times."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select

from app.modules.acquisition.models import AdDeliveryLog


def stage_seconds(capacity: dict[str, Any]) -> dict[str, int]:
    return {
        "two_minute": max(120, int(capacity.get("survival_check_delay_seconds") or 120)),
        "one_hour": max(3600, int(capacity.get("survival_one_hour_seconds") or 3600)),
        "twenty_four_hour": max(
            86400, int(capacity.get("survival_twenty_four_hour_seconds") or 86400)
        ),
    }


def overdue_clause(now: datetime, capacity: dict[str, Any]):
    log = AdDeliveryLog
    return or_(
        *(
            and_(
                func.coalesce(log.survival_stage, "two_minute") == stage,
                log.sent_at <= now - timedelta(seconds=seconds),
            )
            for stage, seconds in stage_seconds(capacity).items()
        )
    )


def eligible_check(now: datetime, capacity: dict[str, Any]):
    log = AdDeliveryLog
    # A local budget deferral may become runnable after a policy/window change.
    # Re-evaluate its governor; Telegram FloodWait and technical backoff retain
    # their original deadlines, and no read bypasses the shared budget.
    return or_(
        log.survival_check_due_at <= now,
        and_(log.survival_error == "telegram_read_budget", overdue_clause(now, capacity)),
    )


async def backlog(db: Any, account_id: int, now: datetime) -> dict[str, Any]:
    from app.core.automation_settings import get_ad_capacity_settings

    seconds = stage_seconds(await get_ad_capacity_settings(db))
    log = AdDeliveryLog
    rows = (
        await db.execute(
            select(log.sent_at, log.survival_stage, log.survival_check_due_at).where(
                log.account_id == account_id,
                log.status == "success",
                log.telegram_message_id.is_not(None),
                log.survival_status == "pending",
            )
        )
    ).all()
    overdue = []
    for row in rows:
        delay = seconds.get(row.survival_stage or "two_minute")
        if (
            row.sent_at is not None
            and delay is not None
            and row.sent_at + timedelta(seconds=delay) <= now
        ):
            overdue.append((row, int((now - row.sent_at).total_seconds()) - delay))
    attempts = [row.survival_check_due_at for row, _ in overdue if row.survival_check_due_at]
    return {
        "survival_pending": len(rows),
        "survival_overdue": len(overdue),
        "survival_deferred_overdue": sum(
            bool(row.survival_check_due_at and row.survival_check_due_at > now)
            for row, _ in overdue
        ),
        "survival_oldest_overdue_seconds": max((age for _, age in overdue), default=0),
        "survival_next_attempt_at": min(attempts).isoformat() if attempts else None,
    }
