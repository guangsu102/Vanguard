"""Adaptive advertisements are reviewed once per group cycle, by the daily worker."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select

from app.modules.acquisition.adaptive_frequency import (
    FrequencyService,
    enabled,
    frequency_context,
    payload,
)
from app.modules.acquisition.models import AdDeliveryLog

LOCAL_WAITS = {
    "telegram_read_budget",
    "AccountOperationLeaseBusy",
    "AccountOperationLeaseUnavailable",
    "account_unavailable",
}
MAINTENANCE_GRACE = timedelta(hours=24)


def may_continue(state: Any, due_at: datetime, now: datetime, log: Any = None) -> bool:
    """Keep the approved rate during bounded local scheduling waits; never promote."""
    evidence_clear = log is None or (
        log.survival_status not in {"deleted", "check_failed"}
        and log.survival_error in {None, *LOCAL_WAITS}
    )
    return bool(
        evidence_clear
        and state.mature
        and state.status == "active"
        and state.daily_review_error in {None, *LOCAL_WAITS}
        and now < due_at + MAINTENANCE_GRACE
    )


def use_daily_review(log: Any, *, mature: bool = True) -> None:
    """Retire the per-message job without inventing a survival observation."""
    context = payload(log.qualification_context_json)
    context["survival_policy"] = "daily_last_ad" if mature else "probe_cycle"
    if log.survival_error:
        context["superseded_checkpoint_error"] = log.survival_error
    log.qualification_context_json = json.dumps(context, ensure_ascii=False)
    log.survival_status = "not_required"
    log.survival_stage = "daily"
    log.survival_check_due_at = None
    log.survival_error = None
    log.survival_version = (log.survival_version or 0) + 1
    log.survival_claim_token = log.survival_claim_expires_at = None


async def retire_mature_checkpoints(
    db: Any, now: datetime, *, limit: int = 300, include_probes: bool = False
) -> int:
    """Drain legacy jobs under row locks; an active reader keeps its claim."""
    logs = list(
        (
            await db.scalars(
                select(AdDeliveryLog)
                .where(
                    AdDeliveryLog.status == "success",
                    AdDeliveryLog.survival_status == "pending",
                    or_(
                        AdDeliveryLog.survival_claim_token.is_(None),
                        AdDeliveryLog.survival_claim_expires_at <= now,
                    ),
                )
                .order_by(AdDeliveryLog.id)
                .limit(limit)
                .with_for_update(of=AdDeliveryLog, skip_locked=True)
            )
        ).all()
    )
    count = 0
    enabled_accounts = {}
    for log in logs:
        if not frequency_context(log):
            continue
        if log.account_id not in enabled_accounts:
            enabled_accounts[log.account_id] = await enabled(db, log.account_id)
        if not enabled_accounts[log.account_id]:
            continue
        state = await FrequencyService(db).state(
            log.telegram_group_id, payload(log.qualification_context_json)
        )
        if state and (state.mature or include_probes) and state.status == "active":
            # Negative/uncertain message observations remain for reconciliation.
            if (
                log.survival_error
                and log.survival_error not in LOCAL_WAITS
                and not log.survival_error.startswith("survival_read_unknown:")
            ):
                continue
            use_daily_review(log, mature=state.mature)
            count += 1
    await db.flush()
    return count
