"""Resolve old ad ledgers from exact local receipts, without Telegram writes."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.core.account.models import AccountOutboundAttempt
from app.core.account.send_receipts import (
    mark_manual_evidence_required,
    receipt_key,
    recover_missing_receipts,
)
from app.core.settings_models import SystemSetting
from app.modules.acquisition.qualification_identity import peer_identity


async def reconcile_ad_ledger(db: Any, log: Any, *, now: datetime) -> dict | None:
    if not log.reservation_token:
        return None
    from app.core.account.outbound_budget import AccountOutboundBudgetService

    # Keep the same account -> ledger lock order as outbound mutations. A
    # worker may already hold an old ORM instance, so locking alone cannot
    # protect the terminal-state guard without refreshing its loaded values.
    budget = AccountOutboundBudgetService(db)
    await budget._account(log.account_id, lock=True)
    attempt = await db.scalar(
        select(AccountOutboundAttempt)
        .where(
            AccountOutboundAttempt.account_id == log.account_id,
            AccountOutboundAttempt.attempt_key == "ad:" + str(log.reservation_token),
            AccountOutboundAttempt.category == "ad",
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if attempt is None or attempt.state not in {"attempted", "unknown"}:
        return None
    if attempt.created_at > now - timedelta(minutes=10):
        return {"state": "pending", "error": "outbound_attempt_in_progress", "retry_after": 60}
    receipt = await db.get(
        SystemSetting, receipt_key(log.account_id, attempt.attempt_key), populate_existing=True
    )
    try:
        proof = json.loads(receipt.value) if receipt else {}
    except (ValueError, TypeError):
        proof = {}
    if not isinstance(proof, dict):
        proof = {}
    target = peer_identity(attempt.target_key)
    log_target = peer_identity(log.telegram_group_id)
    # A raw positive ID has no namespace. Only the correlated log/receipt can
    # supply it; a contradiction is never resolved by guessing a peer type.
    same_log = bool(
        target
        and log_target
        and target[0] == log_target[0]
        and (target[1] is None or target[1] == log_target[1])
    )
    candidates = {
        value
        for value in [attempt.message_id, log.telegram_message_id if same_log else None]
        if type(value) is int and value > 0
    }
    recorded_peer = proof.get("peer")
    receipt_matches = bool(
        target
        and same_log
        and isinstance(recorded_peer, list)
        and len(recorded_peer) == 2
        and recorded_peer[0] == target[0]
        and recorded_peer[1] in {"chat", "channel"}
        and target[1] in {None, recorded_peer[1]}
        and log_target[1] in {None, recorded_peer[1]}
        and proof.get("account_id") == log.account_id
    )
    if receipt_matches and type(proof.get("message_id")) is int and proof["message_id"] > 0:
        candidates.add(proof["message_id"])
    facts = {
        "attempt_id": attempt.id,
        "delivery_log_id": log.id,
        "original_state": attempt.state,
        "exact_receipt_found": bool(candidates),
        "automatic_resend": False,
    }
    if same_log and len(candidates) == 1:
        message_id = candidates.pop()
        attempt.attempted_at = attempt.attempted_at or log.sent_at or attempt.created_at
        await budget.reconcile(
            attempt.attempt_key,
            account_id=log.account_id,
            state="succeeded",
            message_id=message_id,
            confirmed=True,
            now=now,
        )
        await recover_missing_receipts(
            db, now, log.account_id, 1, delivery_log_id=log.id, independent=True
        )
        return {"state": "succeeded", "observed": True, "evidence": facts}
    reason = (
        "outbound_receipt_conflict"
        if len(candidates) > 1 or not same_log
        else "manual_evidence_required"
    )
    await mark_manual_evidence_required(
        db, log, now, facts=facts, reason="send_outcome_unknown:" + reason
    )
    await db.commit()
    return {"state": "manual_required", "error": reason, "observed": True, "evidence": facts}
