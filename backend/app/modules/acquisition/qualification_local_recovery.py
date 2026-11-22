"""Local, fail-closed review completion before network budget admission."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import lazyload

CLAIM_GRACE_SECONDS = 90
ACTIVE_STATES = frozenset({"running", "reviewing_ai", "waiting_ai"})


def payload(value: str | None) -> dict | None:
    try:
        result = json.loads(value or "{}")
    except (TypeError, ValueError):
        return None
    return result if isinstance(result, dict) else None


def technical_end(snapshot: dict, now: datetime) -> bool:
    from app.modules.acquisition.evidence_progress import date

    if snapshot.get("technical_recovery_exhausted") is True:
        return True
    failures = snapshot.get("technical_failures", 0)
    if type(failures) is int and failures >= 3:
        return True
    started = date(snapshot.get("technical_recovery_started_at")) or date(snapshot.get("checked_at"))
    lease = date(snapshot.get("lease_wait_started_at"))
    return bool((started and now >= started + timedelta(hours=24))
                or (lease and now >= lease + timedelta(hours=24)))


async def finish_local_review(db: Any, audit: Any, now: datetime) -> str | None:
    """Caller owns the audit lock; revalidate current scope under membership lock.

    This cannot grant, acknowledge an event, reset a failure counter or perform
    a network read. A new technical event is left durable for the normal bounded
    event-admission path, including its operational backoff checks.
    """
    from app.core.account.models import TelegramAccount
    from app.core.group.models import Group
    from app.core.group.models import GroupAccountMembership as Member
    from app.core.settings_models import SystemSetting
    from app.modules.acquisition.evidence_progress import date
    from app.modules.acquisition.group_qualification import POLICY_VERSION
    from app.modules.acquisition.models import GroupQualificationAudit as Audit
    from app.modules.acquisition.qualification_ai import profile_fingerprint
    from app.modules.acquisition.qualification_due import current_clause
    from app.modules.acquisition.qualification_events import pending, recovery_event_key, same_evidence_event
    from app.modules.acquisition.qualification_service import scope_matches
    from app.modules.acquisition.review_waits import BUDGET_WAIT_REASONS, clear, event_backoff

    raw = payload(audit.evidence_json)
    if raw is None:
        return None
    effective = raw.get("pending_collection") or raw
    if not isinstance(effective, dict):
        return None
    active = audit.state in ACTIVE_STATES
    terminal_ai = audit.decision == "reject" and raw.get("ai_final") is True and raw.get("ai_decision") == "fail"
    terminal_technical = audit.decision == "technical_wait" and technical_end(effective, now)
    ai_candidate = terminal_ai and (active or audit.state == "queued" or (
        audit.state == "completed" and audit.next_retry_at is not None and audit.next_retry_at <= now))
    if not terminal_technical and not ai_candidate:
        return None
    if active and (audit.next_retry_at is None or audit.next_retry_at > now - timedelta(seconds=CLAIM_GRACE_SECONDS)):
        return "claim_grace"
    if audit.state not in ACTIVE_STATES | {"queued", "completed"}:
        return None
    current = await db.scalar(select(Audit.id).where(Audit.id == audit.id, current_clause()))
    if current is None:
        return "scope_changed"
    member = await db.scalar(select(Member).options(lazyload(Member.account)).where(
        Member.id == audit.membership_id,
    ).with_for_update(of=Member, skip_locked=True).execution_options(populate_existing=True))
    if member is None:
        return "membership_busy"
    if (member.account_id != audit.account_id or member.group_id != audit.group_id
            or member.joined_at != audit.membership_joined_at or audit.policy_version != POLICY_VERSION
            or audit.content_scope != "text_profile" or member.status not in {"joined", "pending", "rejected", "leave_failed"}
            or member.left_at is not None):
        return "scope_changed"
    if member.ad_status == "paused" or member.review_status in {
        "manual_required", "owned_group_excluded", "exit_pending", "leave_failed", "membership_reconciliation",
    }:
        return "operator_hold"
    group = await db.get(Group, audit.group_id, populate_existing=True)
    if group is None:
        return "scope_changed"
    material_scope_valid = scope_matches(audit, group, member)
    # A resumed collection is separate persisted material. Both its scope and
    # the outer verdict must belong to this membership before either can close
    # a review locally; missing legacy scope remains for normal collection.
    if effective is not raw:
        view = SimpleNamespace(**{name: getattr(audit, name) for name in (
            "account_id", "group_id", "membership_id", "membership_joined_at", "policy_version", "content_scope",
        )}, evidence_json=json.dumps(effective, ensure_ascii=False, default=str))
        material_scope_valid = material_scope_valid and scope_matches(view, group, member)
    if any(name in snapshot and date(snapshot[name]) != member.joined_at
           for snapshot in (raw, effective) for name in ("joined_at", "membership_joined_at")):
        material_scope_valid = False
    # Scope may have acquired a newer audit while waiting for the membership.
    if await db.scalar(select(Audit.id).where(Audit.id == audit.id, current_clause())) is None:
        return "scope_changed"
    if audit.next_retry_at is not None and audit.next_retry_at > now and await event_backoff(db, audit, now):
        setting = await db.get(SystemSetting, f"qualification.wait.{audit.id}", populate_existing=True)
        wait = payload(setting.value if setting else None)
        # A platform/lease/AI timer is not a local quota forecast. Preserve it
        # for terminal AI as well as technical recovery; new evidence must not
        # erase its provenance or preempt an operational execution wait.
        if not wait or wait.get("reason") not in BUDGET_WAIT_REASONS:
            return "operational_backoff"
    if not material_scope_valid:
        # The current audit belongs to this membership, but its saved material
        # does not. Keep that material durable while explicitly scheduling a
        # full fresh collection. Never repair evidence by fabricating scope.
        controls = {"collection_needs_refresh": True, "collection_complete": False,
                    "scope_collection_refresh": True, "read_plan_requires_full": True,
                    "review_trigger": "snapshot_scope_refresh"}
        raw.update(controls)
        raw.pop("collection_operation_key", None)
        if isinstance(raw.get("pending_collection"), dict):
            raw["pending_collection"] = {**raw["pending_collection"], **controls}
            raw["pending_collection"].pop("collection_operation_key", None)
        audit.evidence_json = json.dumps(raw, ensure_ascii=False, default=str)
        audit.state, audit.decision = "queued", "observe"
        audit.next_retry_at = max(audit.next_retry_at or now, now + timedelta(seconds=60))
        member.review_next_at = audit.next_retry_at
        await clear(db, audit.id)
        return "snapshot_scope_requeued"
    event = await pending(db, member)
    if terminal_ai:
        from app.modules.acquisition.evidence_progress import evidence_maturity

        account = await db.get(TelegramAccount, audit.account_id, populate_existing=True)
        if account is None:
            return "scope_changed"
        maturity = date(raw.get("next_evidence_maturity_at"))
        try:
            inferred = evidence_maturity(raw, now)
        except (TypeError, ValueError, AttributeError, OverflowError):
            inferred = now  # Invalid material needs collection, never terminal reuse.
        if inferred is not None:
            # The maturity helper returns now+1s for a fact already matured
            # while queued. Re-enter collection before a new claim replaces
            # the original due timer and makes it look like a future deadline.
            inferred = now if inferred <= now + timedelta(seconds=1) else inferred
            maturity = min(maturity, inferred) if maturity else inferred
        unchanged = (same_evidence_event(raw.get("collection_event"), event)
                     and raw.get("profile_fingerprint") == profile_fingerprint(account))
        if unchanged and not raw.get("collection_needs_refresh") and (maturity is None or maturity > now):
            audit.state, audit.next_retry_at = "completed", maturity
            action = "terminal_ai_closed"
        else:
            # Changed scope or matured evidence must re-enter collection. Keep
            # the durable AI verdict/material so this never grants or retries
            # the same provider request merely because a worker disappeared.
            audit.state, audit.decision = "queued", "observe"
            audit.next_retry_at = max(audit.next_retry_at or now, now + timedelta(seconds=60))
            raw.update(collection_needs_refresh=True, collection_complete=False,
                       review_trigger="stale_claim_evidence_refresh")
            raw.pop("collection_operation_key", None)
            if isinstance(raw.get("pending_collection"), dict):
                raw["pending_collection"] = {**raw["pending_collection"],
                    "collection_needs_refresh": True, "collection_complete": False,
                    "review_trigger": "stale_claim_evidence_refresh"}
                raw["pending_collection"].pop("collection_operation_key", None)
            audit.evidence_json = json.dumps(raw, ensure_ascii=False, default=str)
            action = "terminal_ai_requeued"
        member.review_next_at = audit.next_retry_at
        await clear(db, audit.id)
        return action
    from app.modules.acquisition.evidence_progress import evidence_maturity, progress_retry
    try:
        if evidence_maturity(effective, now) is not None or progress_retry(effective, now) is not None:
            return None  # Real maturation/unfinished evidence still owns its read gate.
    except (TypeError, ValueError, AttributeError, OverflowError):
        return None  # Invalid evidence cannot prove a safe local completion.
    if (member.status not in {"joined", "pending"}
            or member.review_status not in {"review_2h", "initial_pending"}
            or member.ad_status not in {"warming", "blocked"}):
        return "operator_hold"
    prior_key = effective.get("technical_recovery_event_key") or recovery_event_key(effective.get("collection_event"))
    fresh = recovery_event_key(event)
    fresh = fresh is not None and fresh != prior_key
    raw.update(technical_recovery_exhausted=True, review_trigger="new_evidence_required")
    if isinstance(raw.get("pending_collection"), dict):
        raw["pending_collection"] = {**raw["pending_collection"], "technical_recovery_exhausted": True,
                                     "review_trigger": "new_evidence_required"}
    audit.evidence_json = json.dumps(raw, ensure_ascii=False, default=str)
    audit.state, audit.next_retry_at = "completed", None
    member.review_next_at = None
    await clear(db, audit.id)
    return "technical_new_event_pending" if fresh else "technical_exhausted"


async def finish_local_reviews(db: Any, now: datetime, *, limit: int = 32) -> int:
    """Bounded rotating maintenance even when no review ticket is due."""
    from app.core.settings_models import SystemSetting
    from app.modules.acquisition.models import GroupQualificationAudit as Audit
    from app.modules.acquisition.qualification_due import current_clause

    key = "qualification.local_terminal_scan"
    cursor = await db.get(SystemSetting, key, populate_existing=True)
    try:
        after = max(0, int(cursor.value)) if cursor else 0
    except (ValueError, TypeError, OverflowError):
        after = 0
    query = select(Audit).where(current_clause(), or_(
        ((Audit.decision == "reject") & or_(
            (Audit.state.in_(ACTIVE_STATES) & (Audit.next_retry_at <= now - timedelta(seconds=CLAIM_GRACE_SECONDS))),
            (Audit.state.in_(["queued", "completed"]) & Audit.next_retry_at.is_not(None)),
        )),
        ((Audit.decision == "technical_wait") & or_(
            (Audit.state.in_(["queued", "completed"]) & Audit.next_retry_at.is_not(None)),
            (Audit.state.in_(ACTIVE_STATES) & (Audit.next_retry_at <= now - timedelta(seconds=CLAIM_GRACE_SECONDS))),
        )),
    )).order_by(Audit.id).with_for_update(of=Audit, skip_locked=True).limit(max(1, min(100, limit)))
    rows = list((await db.scalars(query.where(Audit.id > after))).all())
    if not rows and after:
        rows = list((await db.scalars(query)).all())
    count = 0
    for audit in rows:
        action = await finish_local_review(db, audit, now)
        count += int(action in {"terminal_ai_closed", "terminal_ai_requeued", "technical_exhausted",
                               "technical_new_event_pending", "snapshot_scope_requeued"})
    if rows:
        if cursor is None:
            db.add(SystemSetting(key=key, value=str(rows[-1].id)))
        else:
            cursor.value = str(rows[-1].id)
    return count
