"""Persist original request/response correlation before SDK response conversion."""

import hashlib
import json
from contextvars import ContextVar
from datetime import datetime, timedelta
from typing import Any

from app.core.settings_models import SystemSetting

receipt_scope: ContextVar[tuple[int, str] | None] = ContextVar("outbound_receipt", default=None)
RECHECK_PREFIX = "outbound.receipt.recheck."
MANUAL_EVIDENCE_STATE = "manual_evidence_required"


def _context(row: Any) -> dict:
    """Decode a delivery context without allowing malformed history to retry."""
    try:
        from app.modules.acquisition.adaptive_frequency import payload

        value = payload(getattr(row, "qualification_context_json", None))
    except Exception:
        value = {}
    return value if isinstance(value, dict) else {}


def reconciliation_state(row: Any) -> str | None:
    value = _context(row).get("send_reconciliation") or {}
    state = value.get("state") if isinstance(value, dict) else None
    return str(state) if state else None


def parked_receipt(row: Any, now: datetime) -> bool:
    """An old unaddressable send needs local evidence, not a Telegram reader."""
    context = _context(row)
    reconciliation = context.get("send_reconciliation") or {}
    local_recheck = (
        reconciliation.get("state") in {"receipt_evidence_required", MANUAL_EVIDENCE_STATE}
        and reconciliation.get("scope") == "account_group"
        and reconciliation.get("automatic_resend") is False
    )
    terminal_manual = reconciliation.get("state") == MANUAL_EVIDENCE_STATE
    return bool(
        row.status in {"pending", "unknown", "reconciliation_required", "failed"}
        and "send_outcome_unknown" in (row.error or "").lower()
        and (terminal_manual or (row.created_at and row.created_at <= now - timedelta(minutes=30)))
        and (row.telegram_message_id is None or terminal_manual)
        and row.survival_status in {"not_required", "check_failed"}
        and row.survival_stage == "complete"
        and (row.survival_check_due_at is None or local_recheck)
        and row.survival_claim_token is None
        and row.survival_claim_expires_at is None
    )


async def mark_manual_evidence_required(
    db: Any,
    row: Any,
    now: datetime,
    *,
    facts: dict | None = None,
    reason: str = "send_outcome_unknown:original_message_unconfirmed",
    verification_attempts: int = 1,
) -> bool:
    """Close an unprovable send exactly once.

    Telegram writes with an unknown outcome cannot be replayed safely.  The
    original outbound ledger therefore stays ``unknown`` and keeps the target
    fenced, while the delivery row leaves the active scheduler queues.  A
    durable terminal marker prevents a later worker from performing another
    search or resend.
    """
    context = _context(row)
    reconciliation = context.get("send_reconciliation")
    if not isinstance(reconciliation, dict):
        reconciliation = {}
    if reconciliation.get("state") == MANUAL_EVIDENCE_STATE:
        return False
    checked_at = now.isoformat()
    reconciliation.update(
        {
            "state": MANUAL_EVIDENCE_STATE,
            "checked_at": checked_at,
            "facts": facts if isinstance(facts, dict) else reconciliation.get("facts") or {},
            "confirmed": False,
            "original_error": row.error,
            "scope": "account_group",
            "automatic_resend": False,
            "verification_attempts": max(1, int(verification_attempts or 1)),
            "verification_terminal": True,
            "next_check_at": None,
        }
    )
    context["send_reconciliation"] = reconciliation
    row.qualification_context_json = json.dumps(context, ensure_ascii=False)
    row.status = "failed"
    row.error = f"{MANUAL_EVIDENCE_STATE}:{reason}"[:2000]
    row.survival_status = "check_failed"
    row.survival_stage = "complete"
    row.survival_check_due_at = None
    row.survival_checked_at = now
    row.survival_error = row.error
    row.survival_claim_token = None
    row.survival_claim_expires_at = None
    schedule = await db.get(SystemSetting, RECHECK_PREFIX + str(row.id))
    if schedule is not None:
        await db.delete(schedule)
    return True


def receipt_key(account_id: int, attempt_key: str) -> str:
    return "outbound.receipt." + hashlib.sha256(f"{account_id}:{attempt_key}".encode()).hexdigest()


def response_id(request: Any, response: Any) -> int | None:
    from telethon import types
    if isinstance(response, types.UpdateShortSentMessage):
        return response.id if response.id > 0 else None
    matches = [update.id for update in getattr(response, "updates", [])
               if isinstance(update, types.UpdateMessageID) and update.random_id == request.random_id]
    return matches[0] if len(matches) == 1 and matches[0] > 0 else None


async def record_request(account_id: int, requests: list, response: Any = None) -> None:
    scope = receipt_scope.get()
    if not scope or scope[0] != account_id:
        return
    writes = [item for item in requests if type(item).__name__ in {"SendMessageRequest", "SendMediaRequest"}]
    if not writes:
        return
    if len(writes) != 1:
        raise RuntimeError("outbound_receipt_requires_single_request")
    request = writes[0]
    from app.core.database import get_db_session
    from app.modules.acquisition.qualification_identity import entity_identity
    async with get_db_session() as db:
        key = receipt_key(*scope)
        row = await db.get(SystemSetting, key, with_for_update=True)
        data = json.loads(row.value) if row else {}
        if data and data["random_id"] != request.random_id:
            raise RuntimeError("outbound_original_request_changed")
        data.update(random_id=request.random_id, account_id=account_id,
                    peer=entity_identity(request.peer), at=data.get("at") or datetime.utcnow().isoformat())
        if response is not None:
            message_id = response_id(request, response)
            if message_id:
                data["message_id"] = message_id
        if row is None:
            db.add(SystemSetting(key=key, value=json.dumps(data)))
        else:
            row.value = json.dumps(data)


async def recover_missing_receipts(db: Any, now: datetime, account_id: int | None, limit: int) -> dict:
    """Recover exact known IDs; unprovable sends remain isolated without retries."""
    from sqlalchemy import String, and_, cast, exists, func, literal, or_, select

    from app.core.account.models import AccountOutboundAttempt
    from app.modules.acquisition.adaptive_frequency import payload
    from app.modules.acquisition.models import AdDeliveryLog
    from app.modules.acquisition.qualification_identity import peer_identity
    proof_exists = exists(select(AccountOutboundAttempt.id).where(
        AccountOutboundAttempt.account_id == AdDeliveryLog.account_id,
        AccountOutboundAttempt.attempt_key == literal("ad:") + AdDeliveryLog.reservation_token,
        AccountOutboundAttempt.message_id.is_not(None),
        AccountOutboundAttempt.message_id > 0,
    ))
    logs = (await db.execute(select(AdDeliveryLog, SystemSetting).outerjoin(
        SystemSetting, SystemSetting.key == literal(RECHECK_PREFIX) + cast(AdDeliveryLog.id, String),
    ).where(
        or_(
            AdDeliveryLog.status.in_(
                ["pending", "unknown", "sending", "reconciliation_required"]
            ),
            and_(
                AdDeliveryLog.status == "failed",
                AdDeliveryLog.qualification_context_json.ilike("%manual_evidence_required%"),
                proof_exists,
            ),
        ),
        AdDeliveryLog.telegram_message_id.is_(None),
        # Only a write whose outcome is explicitly unknown may enter this
        # recovery lane.  A fresh, not-yet-sent reservation must never be
        # converted into a manual failure by a maintenance sweep.
        or_(
            func.lower(AdDeliveryLog.error).contains("send_outcome_unknown"),
            AdDeliveryLog.status.in_(["unknown", "reconciliation_required"]),
            AdDeliveryLog.qualification_context_json.ilike("%receipt_evidence_required%"),
        ),
        or_(
            # The independent receipt timer remains authoritative after the
            # overloaded survival timer has been cleared by the first check.
            SystemSetting.value <= now.isoformat(),
            and_(AdDeliveryLog.survival_check_due_at.is_(None), SystemSetting.value.is_(None)),
            and_(
                AdDeliveryLog.survival_check_due_at <= now,
                or_(SystemSetting.value.is_(None), SystemSetting.value <= now.isoformat()),
            ),
            # Legacy rows used the survival timer before the receipt timer
            # namespace existed.  Once the reconciliation marker is present,
            # the old timer must not hide the row from local evidence recovery.
            and_(
                SystemSetting.value.is_(None),
                AdDeliveryLog.qualification_context_json.ilike("%receipt_evidence_required%"),
            ),
        ),
        AdDeliveryLog.survival_claim_token.is_(None),
        AdDeliveryLog.survival_claim_expires_at.is_(None),
        AdDeliveryLog.created_at < now - timedelta(minutes=10),
        True if account_id is None else AdDeliveryLog.account_id == account_id,
    ).order_by(AdDeliveryLog.id).limit(limit))).all()
    recovered = waiting = manual = 0
    for log, schedule in logs:
        attempt_key = "ad:" + str(log.reservation_token or "")
        attempt = await db.scalar(select(AccountOutboundAttempt).where(
            AccountOutboundAttempt.account_id == log.account_id,
            AccountOutboundAttempt.attempt_key == attempt_key)) if log.reservation_token else None
        receipt = await db.get(SystemSetting, receipt_key(log.account_id, attempt_key)) if attempt else None
        proof = payload(receipt.value) if receipt else {}
        message_id = attempt.message_id if attempt else None
        if not message_id and proof.get("peer") == list(peer_identity(log.telegram_group_id) or ()):
            message_id = proof.get("message_id")
        context = _context(log)
        if type(message_id) is int and message_id > 0:
            log.telegram_message_id, log.survival_check_due_at = message_id, now
            was_manual = reconciliation_state(log) == MANUAL_EVIDENCE_STATE
            if was_manual:
                # Manual evidence is terminal for Telegram reads, but a later
                # local ledger proof can safely restore the original delivery.
                log.status = "success"
                log.sent_at = log.sent_at or log.created_at or now
                log.error = None
                log.survival_status = "pending"
                log.survival_stage = "two_minute"
                log.survival_error = None
                log.survival_retry_count = 0
                try:
                    from app.modules.acquisition.daily_frequency import DailyFrequencyService

                    await DailyFrequencyService(db).note_sent(log)
                except Exception:
                    pass
            recovered += 1
            state = "exact_receipt_recovered"
            if schedule is not None:
                await db.delete(schedule)
            next_check = None
            context["send_reconciliation"] = {
                "state": "exact_receipt_recovered",
                "checked_at": now.isoformat(),
                "next_check_at": None,
                "scope": "account_group",
                "automatic_resend": False,
                "verification_attempts": 1 if was_manual else 0,
                "verification_terminal": True,
            }
            log.qualification_context_json = json.dumps(context, ensure_ascii=False)
            if was_manual and log.reservation_token:
                try:
                    from app.core.account.outbound_budget import AccountOutboundBudgetService

                    if attempt is not None and attempt.attempted_at is None:
                        # The exact Telegram message ID is durable proof that
                        # the external write happened; repair legacy ledgers
                        # that missed their attempted_at timestamp before CAS.
                        attempt.attempted_at = log.sent_at or log.created_at or now
                    await AccountOutboundBudgetService(db).reconcile(
                        "ad:" + str(log.reservation_token),
                        account_id=log.account_id,
                        state="succeeded",
                        message_id=message_id,
                        confirmed=True,
                    )
                except Exception:
                    # The exact receipt and target fence remain durable even if
                    # budget reconciliation is temporarily unavailable.
                    pass
        else:
            changed = await mark_manual_evidence_required(
                db,
                log,
                now,
                reason="send_outcome_unknown:message_id_unproven",
                verification_attempts=1,
            )
            manual += int(changed)
            waiting += 0
    await db.commit()
    return {
        "receipt_ids_recovered": recovered,
        "receipt_evidence_required": waiting,
        "manual_evidence_required": manual,
        "abandoned_pending_closed": await _close_abandoned_pending(db, now, account_id, limit),
    }


async def _close_abandoned_pending(db: Any, now: datetime, account_id: int | None, limit: int) -> int:
    """Close pending rows whose send never reached the outbound ledger.

    A worker crash between the delivery-log insert and the ledger reservation
    leaves a pending row with no error marker, no receipt timer and no ledger
    attempt.  The unknown-outcome lanes deliberately ignore it, so without this
    sweep it would stay pending forever.  Once clearly stale (one hour) and
    provably never attempted (no ledger row for its token), close it.
    """
    from sqlalchemy import exists as sa_exists, literal, or_, select

    from app.core.account.models import AccountOutboundAttempt
    from app.modules.acquisition.models import AdDeliveryLog

    ledger_attempt = sa_exists(select(AccountOutboundAttempt.id).where(
        AccountOutboundAttempt.account_id == AdDeliveryLog.account_id,
        AccountOutboundAttempt.attempt_key == literal("ad:") + AdDeliveryLog.reservation_token,
    ))
    rows = (await db.execute(
        select(AdDeliveryLog).where(
            AdDeliveryLog.status == "pending",
            AdDeliveryLog.telegram_message_id.is_(None),
            or_(AdDeliveryLog.error.is_(None), AdDeliveryLog.error == ""),
            AdDeliveryLog.reservation_token.is_not(None),
            AdDeliveryLog.created_at < now - timedelta(hours=1),
            ~ledger_attempt,
            True if account_id is None else AdDeliveryLog.account_id == account_id,
        ).order_by(AdDeliveryLog.id).limit(limit)
    )).scalars().all()
    closed = 0
    for log in rows:
        context = _context(log)
        context["send_reconciliation"] = {
            "state": "abandoned_before_attempt",
            "checked_at": now.isoformat(),
            "scope": "account_group",
            "automatic_resend": False,
            "verification_terminal": True,
        }
        log.qualification_context_json = json.dumps(context, ensure_ascii=False)
        log.status = "failed"
        log.error = "send_abandoned_before_attempt:worker_crash"[:2000]
        log.survival_status = "not_required"
        log.survival_stage = "complete"
        log.survival_check_due_at = None
        log.survival_checked_at = now
        closed += 1
    if closed:
        await db.commit()
    return closed
