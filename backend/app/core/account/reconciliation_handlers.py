"""Adapters from outbox tasks to existing read-only reconciliation services."""

from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime
from typing import Any

verification_only: ContextVar[bool] = ContextVar("reconciliation_verification_only", default=False)


async def execute_task(
    db: Any,
    task_id: int,
    owner: str,
    account_id: int,
    *,
    account_pool: Any,
    kind: str | None = None,
    source_id: int | None = None,
    growth_verifier: Any = None,
) -> dict:
    from app.core.account.control_models import ReconciliationTask

    row = await db.get(ReconciliationTask, task_id)
    from app.core.account.reconciliation_stream import active_for

    if (
        row is None
        or row.lease_owner != owner
        or row.account_id != account_id
        or row.state != "leased"
        or not row.lease_expires_at
        or row.lease_expires_at <= datetime.utcnow()
        or not active_for(account_id)
    ):
        return {"state": "pending", "error": "reconciliation_lease_lost", "retry_after": 30}
    await db.commit()
    token = verification_only.set(True)
    try:
        return await _execute(
            db,
            row.kind,
            row.source_id,
            account_id,
            account_pool=account_pool,
            growth_verifier=growth_verifier,
        )
    finally:
        verification_only.reset(token)


async def _execute(
    db: Any,
    kind: str,
    source_id: int,
    account_id: int,
    *,
    account_pool: Any,
    growth_verifier: Any = None,
) -> dict:
    if kind == "growth":
        from app.modules.acquisition.p2_outbox import (
            read_only_growth_verifier,
            reconcile_growth_operation,
        )

        verifier = growth_verifier
        if verifier is None:
            async def verifier(operation):
                return await read_only_growth_verifier(operation, account_pool=account_pool)

        return await reconcile_growth_operation(
            db,
            source_id,
            verifier=verifier,
        )
    if kind == "join":
        from app.modules.acquisition.automation import AcquisitionAutomationService
        from app.modules.acquisition.models import AutoJoinAttempt

        source = await db.get(AutoJoinAttempt, source_id)
        if source is None or source.account_id != account_id:
            return {"state": "cancelled"}
        if source.status == "success" or source.reconciliation_status in {"confirmed", "resolved"}:
            return {"state": "succeeded"}
        if source.request_state not in {"sent", "outcome_unknown"}:
            return {"state": "cancelled"}
        now = datetime.utcnow()
        if source.reconciliation_next_at and source.reconciliation_next_at > now:
            return {
                "state": "pending",
                "retry_after": max(1, int((source.reconciliation_next_at - now).total_seconds())),
                **({"next_verification_at": source.reconciliation_next_at.isoformat()}
                   if source.request_state == "sent" and source.reason == "join_request_still_pending"
                   else {}),
            }
        result = await AcquisitionAutomationService(
            db,
            account_pool=account_pool,
        ).reconcile_join_requests(
            account_id=account_id, attempt_id=source_id, limit=1, independent=True
        )
        details = [
            item
            for item in result.get("details", [])
            if int(item.get("attempt_id") or -1) == source_id
        ]
        pending = not details or any(
            str(item.get("status")) in {"pending", "unknown"} for item in details
        )
        await db.refresh(source)
        wait = (
            max(30, int((source.reconciliation_next_at - datetime.utcnow()).total_seconds()))
            if source.reconciliation_next_at
            else 60
        )
        return {
            "state": "pending" if pending else "succeeded",
            "retry_after": wait,
            **({"next_verification_at": source.reconciliation_next_at.isoformat()}
               if pending and source.request_state == "sent"
               and source.reason == "join_request_still_pending" and source.reconciliation_next_at
               else {}),
            "observed": bool(
                source.reconciliation_checked_at and source.reconciliation_checked_at >= now
            ),
        }
    if kind == "ad":
        from app.core.account.send_receipts import (
            MANUAL_EVIDENCE_STATE,
            reconciliation_state,
            recover_missing_receipts,
        )
        from app.modules.acquisition.automation import AcquisitionAutomationService
        from app.modules.acquisition.models import AdDeliveryLog

        source = await db.get(AdDeliveryLog, source_id)
        if source is None or source.account_id != account_id:
            return {"state": "cancelled"}
        if source.status == "success":
            return {"state": "succeeded"}
        if source.status not in {"pending", "unknown", "sending", "reconciliation_required"}:
            return {"state": "manual_required" if "unknown" in (source.error or "") else "cancelled"}
        if reconciliation_state(source) == MANUAL_EVIDENCE_STATE:
            return {"state": "manual_required", "error": "manual_evidence_required"}
        service = AcquisitionAutomationService(db, account_pool=account_pool)
        # Recover only an exact recorded receipt; never search message content.
        await recover_missing_receipts(
            db, datetime.utcnow(), account_id, 1, delivery_log_id=source_id, independent=True
        )
        await db.refresh(source)
        if source.status == "success":
            return {"state": "succeeded", "observed": True}
        if source.status == "failed" and "send_abandoned_before_attempt" in (source.error or ""):
            return {"state": "cancelled", "observed": True}
        if reconciliation_state(source) == MANUAL_EVIDENCE_STATE:
            return {
                "state": "manual_required",
                "error": "manual_evidence_required",
                "observed": True,
            }
        if not source.telegram_message_id:
            return {"state": "pending", "retry_after": 60}
        if source.survival_check_due_at and source.survival_check_due_at > datetime.utcnow():
            return {
                "state": "pending",
                "retry_after": max(
                    1, int((source.survival_check_due_at - datetime.utcnow()).total_seconds())
                ),
            }
        from app.core.account.models import TelegramAccount

        await service._sync_account_pool([await db.get(TelegramAccount, account_id)])
        result = await service._reconcile_one_ad_delivery(source, datetime.utcnow())
        return {
            "state": "manual_required"
            if result == "manual_evidence_required"
            else "succeeded"
            if result == "send_reconciled"
            else "pending",
            "error": result if result not in {"send_reconciled"} else None,
            "retry_after": 60,
            "observed": result in {"send_reconciled", "manual_evidence_required"},
        }
    from app.core.worker_status import TelegramEventInbox

    event = await db.get(TelegramEventInbox, source_id, with_for_update=True)
    if event is None or event.state in {"completed", "reconciled"}:
        return {"state": "succeeded"}
    # A durable external attempt has no safe automatic replay. Keep the source
    # in the existing operator evidence lane and close only the outbox task.
    event.state = "reconciliation_required"
    event.last_error = "independent_reconciliation_manual_evidence"
    await db.commit()
    return {"state": "manual_required", "error": "external_attempt_requires_evidence"}
