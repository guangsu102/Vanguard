"""Scoped change work, separate from an enduring qualification grant.

Only local state is touched. A compare-and-clear acknowledges exactly the
generation read by a worker, so a later callback cannot be lost on completion.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import String, cast, or_, select

from app.core.settings_models import SystemSetting

SOURCE_HISTORY_LIMIT = 64


def _source_identity(kind: str, token: str) -> str:
    return hashlib.sha256(json.dumps([kind, token], separators=(",", ":")).encode()).hexdigest()


def _source_history(value: dict) -> list[str]:
    raw = value.get("source_history")
    return [item for item in raw if isinstance(item, str) and len(item) == 64][-SOURCE_HISTORY_LIMIT:] if isinstance(raw, list) else []


def key(membership_id: int) -> str:
    return f"qualification.event.{membership_id}"


def account_key(account_id: int) -> str:
    return f"qualification.continuity.{account_id}"


def same_evidence_event(left: dict | None, right: dict | None) -> bool:
    """Queue bookkeeping is not new evidence; all scope/version fields remain strict.

    This is for evidence reuse only. Acknowledge still compares the full event
    while holding the membership lock.
    """
    if not isinstance(left, dict) or not isinstance(right, dict):
        return left == right
    return ({key: value for key, value in left.items() if key != "queued"}
            == {key: value for key, value in right.items() if key != "queued"})


def recovery_event_key(event: dict | None) -> str | None:
    """Identify one durable evidence generation, excluding queue bookkeeping."""
    if not isinstance(event, dict):
        return None
    kinds = event.get("kinds")
    if (not isinstance(kinds, list) or not kinds or len(kinds) > 32
            or any(not isinstance(kind, str) or not kind or len(kind) > 64 for kind in kinds)):
        return None
    token, generation = event.get("token"), event.get("account_generation")
    explicit = isinstance(token, str) and 0 < len(token) <= 64
    observed_gap = isinstance(generation, str) and generation.startswith("gap:")
    if not explicit and not observed_gap:
        return None  # A malformed/legacy reconnect marker is not fresh evidence.
    material = {key: value for key, value in event.items() if key != "queued"}
    try:
        return hashlib.sha256(json.dumps(material, sort_keys=True, allow_nan=False).encode()).hexdigest()
    except (ValueError, TypeError, OverflowError):
        return None


async def queue_terminal_technical_events(db: Any, account_id: int, *, limit: int = 2, audit_id: int | None = None) -> int:
    """Wake exhausted reads once per new event; never grant or acknowledge it."""
    from app.modules.acquisition.qualification_engine import enabled_for, queue_pending_events
    if enabled_for(account_id):
        return await queue_pending_events(db, account_id, limit=limit)
    from app.core.group.models import GroupAccountMembership as Member
    from app.modules.acquisition.models import GroupQualificationAudit as Audit
    from app.modules.acquisition.qualification_due import current_clause
    from app.modules.acquisition.qualification_service import latest_review
    from app.modules.acquisition.review_waits import event_backoff

    if limit <= 0:
        return 0
    # This independent bounded scan cannot be hidden behind 300 active grants
    # with no events. Callbacks retain membership-only writes and lock order.
    marker = await db.get(SystemSetting, account_key(account_id), populate_existing=True)
    observed_gap = bool(marker and isinstance(marker.value, str) and marker.value.startswith("gap:"))
    now = datetime.utcnow()
    query = select(Audit).join(Member, Member.id == Audit.membership_id)
    query = (query
        .outerjoin(SystemSetting, SystemSetting.key == "qualification.event." + cast(Member.id, String))
        .where(Audit.account_id == account_id, current_clause(), Audit.state.in_(["completed", "queued"]),
               Audit.decision == "technical_wait", or_(Audit.next_retry_at.is_(None), Audit.next_retry_at <= now),
               Member.status.in_(["joined", "pending"]), Member.left_at.is_(None),
               Member.review_status.in_(["review_2h", "initial_pending"]),
               Member.ad_status.in_(["warming", "blocked"]),
               or_(SystemSetting.key.is_not(None), observed_gap))
        .order_by(Audit.id).limit(300))
    if audit_id is not None:
        query = query.where(Audit.id == audit_id)
    cursor_key = f"qualification.technical_event_scan.{account_id}"
    cursor = await db.get(SystemSetting, cursor_key, populate_existing=True)
    try:
        after = max(0, int(cursor.value)) if cursor else 0
    except (ValueError, TypeError, OverflowError):
        after = 0
    rows = (await db.scalars(query if audit_id is not None else query.where(Audit.id > after))).all()
    if not rows and after and audit_id is None:
        rows = (await db.scalars(query)).all()
    count = 0
    scanned = None
    for candidate in rows:
        if count >= min(2, limit):
            break
        scanned = candidate.id
        # Follow dispatcher audit -> membership lock order, and recheck all
        # filters after each lock. A newer callback must never be swallowed.
        audit = await db.scalar(select(Audit).where(
            Audit.id == candidate.id, current_clause(), Audit.state.in_(["completed", "queued"]),
            Audit.decision == "technical_wait", or_(Audit.next_retry_at.is_(None), Audit.next_retry_at <= now),
        ).with_for_update(of=Audit, skip_locked=True).execution_options(populate_existing=True))
        if audit is None:
            continue
        try:
            evidence = json.loads(audit.evidence_json or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(evidence, dict) or evidence.get("technical_recovery_exhausted") is not True:
            continue
        now = datetime.utcnow()
        if await event_backoff(db, audit, now):
            continue
        member = await db.scalar(select(Member).where(
            Member.id == audit.membership_id, Member.account_id == account_id,
            Member.group_id == audit.group_id, Member.joined_at.is_not_distinct_from(audit.membership_joined_at),
            Member.status.in_(["joined", "pending"]), Member.left_at.is_(None),
            Member.review_status.in_(["review_2h", "initial_pending"]),
            Member.ad_status.in_(["warming", "blocked"]),
        ).with_for_update(of=Member, skip_locked=True).execution_options(populate_existing=True))
        if member is None:
            continue
        latest = await latest_review(db, member.id, include_pending=True)
        if latest is None or latest.id != audit.id:
            continue
        event = await pending(db, member)
        event_key = recovery_event_key(event)
        prior_key = evidence.get("technical_recovery_event_key") or recovery_event_key(evidence.get("collection_event"))
        if event_key is None or event_key == prior_key:
            continue
        stored = await db.get(SystemSetting, key(member.id), populate_existing=True)
        # Do not replace a synthetic account gap's scope with stale JSON.
        data = dict(event)
        data["queued"] = True
        if stored is None:
            db.add(SystemSetting(key=key(member.id), value=json.dumps(data)))
        else:
            stored.value = json.dumps(data)
        controls = dict(
            technical_failures=0, technical_recovery_exhausted=False,
            technical_recovery_started_at=now.isoformat(),
            technical_recovery_deadline_at=(now + timedelta(hours=24)).isoformat(),
            technical_recovery_event_key=event_key, review_trigger="technical_new_evidence",
        )
        evidence.update(controls)
        evidence.pop("lease_wait_started_at", None)
        if isinstance(evidence.get("pending_collection"), dict):
            # The collector resumes this nested snapshot as its previous input.
            # Carry the new bounded window without discarding any evidence.
            evidence["pending_collection"] = {**evidence["pending_collection"], **controls}
            evidence["pending_collection"].pop("lease_wait_started_at", None)
        # Preserve all evidence and business verdicts. Only the bounded read
        # recovery window changes; this event receives at most three failures.
        audit.evidence_json = json.dumps(evidence, ensure_ascii=False)
        audit.state, audit.next_retry_at = "queued", now
        member.review_next_at = now
        count += 1
    if scanned is not None and audit_id is None:
        if cursor is None:
            db.add(SystemSetting(key=cursor_key, value=str(scanned)))
        elif cursor.value != str(scanned):
            cursor.value = str(scanned)
    return count


def _legacy_synthetic_gap(value: str | None, joined_at: datetime | None) -> dict | None:
    """Decode the reconnect-only marker written by pre event-scoped workers."""
    if value is None:
        return None
    try:
        data = json.loads(value)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("joined_at") != (
        joined_at.isoformat() if joined_at else None
    ):
        return None
    if (
        data.get("kinds") == ["gap"]
        and data.get("queued") is True
        and not data.get("sources")
        and not data.get("message_ids")
        and isinstance(data.get("account_generation"), str)
        and len(data["account_generation"]) == 32
        and all(char in "0123456789abcdef" for char in data["account_generation"])
    ):
        return data
    return None


def pending_value(value: str | None, joined_at: datetime | None, generation: str | None) -> dict | None:
    """Decode the same scope checks for individual and batched demand reads."""
    data = {}
    if value is not None:
        try:
            data = json.loads(value)
            if data.get("joined_at") != (joined_at.isoformat() if joined_at else None):
                data = {}
        except (ValueError, TypeError, AttributeError):
            data = {"kinds": ["gap"], "token": None}
    # A reconnect is transport recovery, not qualification evidence.  Older
    # versions persisted a marker-only synthetic gap (no source and no
    # message ids) for every membership; ignore that artifact on read. The
    # local recovery pass also retires the queued audit state. Only rows already
    # marked queued are treated as that legacy artifact; a fresh, explicit
    # event request without a source must remain pending.
    if _legacy_synthetic_gap(value, joined_at):
        data = {"account_generation": data["account_generation"]}
    # An explicitly observed gap must also invalidate a previously acknowledged
    # generation. Old reconnect UUIDs never create work or requeue real events.
    if (isinstance(generation, str) and generation.startswith("gap:")
            and data.get("account_generation") != generation):
        data = {**data, "kinds": sorted(set(data.get("kinds", [])) | {"gap"}),
                "account_generation": generation, "queued": False}
    return data if data.get("kinds") else None


async def retire_legacy_synthetic_gaps(db: Any, account_id: int, *, limit: int = 300) -> int:
    from app.modules.acquisition.qualification_engine import enabled_for
    if enabled_for(account_id):
        return 0  # pending_value already excludes synthetic reconnect markers.
    """Remove reconnect-only events and reopen the grant they accidentally queued.

    This is an idempotent compatibility repair for data written before gap
    events were scoped to an observed Telegram fact.  It never touches a
    marker with a source/message identity or a new ``gap:`` generation.
    """
    from app.core.group.models import GroupAccountMembership as Member
    from app.modules.acquisition.models import GroupQualificationAudit as Audit
    from app.modules.acquisition.qualification_due import current_clause
    from app.modules.acquisition.qualification_lifetime import authorization_current

    candidates = (await db.execute(
        select(Audit.id, Member.joined_at, SystemSetting.value)
        .join(Member, Member.id == Audit.membership_id)
        .join(SystemSetting, SystemSetting.key == "qualification.event." + cast(Member.id, String))
        .where(Audit.account_id == account_id, current_clause(), Audit.state == "queued",
               Audit.decision.in_(["allowed", "trial"]), Member.status == "joined",
               Member.left_at.is_(None), Member.review_status == "approved", Member.ad_status == "active")
        .order_by(Audit.id).limit(max(1, min(limit, 300)))
    )).all()
    repaired = 0
    for audit_id, joined_at, value in candidates:
        if _legacy_synthetic_gap(value, joined_at) is None:
            continue
        # Follow the review writer's audit -> membership lock order, and read
        # the event again after locking so a concurrent listener fact is kept.
        audit = await db.scalar(select(Audit).where(
            Audit.id == audit_id, Audit.state == "queued", current_clause(),
        ).with_for_update(of=Audit, skip_locked=True).execution_options(populate_existing=True))
        if audit is None or not authorization_current(audit, datetime.utcnow()):
            continue
        member = await db.scalar(select(Member).where(
            Member.id == audit.membership_id, Member.account_id == account_id,
            Member.group_id == audit.group_id, Member.joined_at == audit.membership_joined_at,
            Member.status == "joined", Member.left_at.is_(None),
            Member.review_status == "approved", Member.ad_status == "active",
        ).with_for_update(of=Member, skip_locked=True).execution_options(populate_existing=True))
        if member is None:
            continue
        setting = await db.get(SystemSetting, key(member.id), populate_existing=True)
        data = _legacy_synthetic_gap(setting.value if setting else None, member.joined_at)
        if data is None or await pending(db, member):
            continue
        setting.value = json.dumps({
            "joined_at": member.joined_at.isoformat() if member.joined_at else None,
            "kinds": [],
            "account_generation": data["account_generation"],
        })
        # The old reconnect marker was the only trigger for this queued claim.
        # Drop its retry timer as well; retaining the queue timestamp would
        # immediately reclaim the otherwise valid grant and recreate the same
        # review loop. A real Telegram fact arriving after these locks writes a
        # fresh event and is queued by the next recovery pass.
        wait = await db.get(SystemSetting, f"qualification.wait.{audit.id}", populate_existing=True)
        if wait is not None:
            await db.delete(wait)
        audit.state, audit.next_retry_at = "completed", None
        member.review_next_at = None
        repaired += 1
    if repaired:
        await db.flush()
    return repaired


async def pending(db: Any, member: Any) -> dict | None:
    row = await db.get(SystemSetting, key(member.id), populate_existing=True)
    marker = await db.get(SystemSetting, account_key(member.account_id), populate_existing=True)
    return pending_value(row.value if row else None, member.joined_at, marker.value if marker else None)


async def request(
    db: Any, member: Any, kind: str, *, message_ids=(), source_token: str | None = None,
    evidence_refs: dict | None = None,
) -> None:
    """Caller locks membership, including when there is no prior event row."""
    row = await db.get(SystemSetting, key(member.id), populate_existing=True)
    old = await pending(db, member) or {}
    saved = {}
    if row is not None:
        try:
            candidate = json.loads(row.value)
            if isinstance(candidate, dict) and candidate.get("joined_at") == (
                member.joined_at.isoformat() if member.joined_at else None
            ):
                saved = candidate
        except (ValueError, TypeError):
            pass
    generation = old.get("account_generation")
    if generation is None:
        generation = saved.get("account_generation")
    history = _source_history(saved)
    sources = dict(old.get("sources") or {})
    identity = _source_identity(kind, source_token) if source_token else None
    if identity and (identity in history or (
        sources.get(kind) == source_token and kind in old.get("kinds", [])
    )):
        return  # Exact fact replay, including a recently consumed generation.
    if source_token:
        sources[kind] = source_token
        history = [*history, identity][-SOURCE_HISTORY_LIMIT:]
    incoming_ids = set(message_ids)
    merged_ids = sorted(set(old.get("message_ids", [])) | incoming_ids)[-32:]
    incoming_refs = evidence_refs if isinstance(evidence_refs, dict) else {}
    previous_refs = old.get("evidence_refs")
    previous_refs = previous_refs if isinstance(previous_refs, dict) else {}
    # A new fact about the same ID supersedes its prior cache reference even
    # when the new body could not be stored. Missing refs must force live reads.
    refs = {
        str(mid): ref for mid in merged_ids
        if mid not in incoming_ids and isinstance(ref := previous_refs.get(str(mid)), dict)
    }
    fields = {"key", "version", "account_id", "group_id", "joined_at", "message_id"}
    for mid in incoming_ids & set(merged_ids):
        ref = incoming_refs.get(str(mid))
        if (
            isinstance(ref, dict)
            and type(ref.get("message_id")) is int and ref["message_id"] == mid
            and ref.get("account_id") == member.account_id
            and ref.get("joined_at") == (member.joined_at.isoformat() if member.joined_at else None)
            and isinstance(ref.get("key"), str) and 0 < len(ref["key"]) <= 256
            and isinstance(ref.get("version"), str) and 0 < len(ref["version"]) <= 256
            and type(ref.get("group_id")) in {int, str} and len(str(ref["group_id"])) <= 32
        ):
            refs[str(mid)] = {key: value for key, value in ref.items() if key in fields}
    data = {
        "joined_at": member.joined_at.isoformat() if member.joined_at else None,
        "kinds": sorted(set(old.get("kinds", [])) | {kind}),
        "message_ids": merged_ids,
        "token": uuid4().hex,
        "queued": False,
        "account_generation": generation,
        "sources": sources,
    }
    if history:
        data["source_history"] = history
    unscoped_rules = old.get("unscoped_rules_token")
    if kind == "rules" and not incoming_ids:
        unscoped_rules = data["token"]
    if unscoped_rules:
        data["unscoped_rules_token"] = unscoped_rules
    if refs:
        data["evidence_refs"] = refs
    if row is None:
        db.add(SystemSetting(key=key(member.id), value=json.dumps(data)))
    else:
        row.value = json.dumps(data)
    # Callback writes only its durable cue; it never waits for an audit lock.
    member.review_next_at = datetime.utcnow()
    from app.modules.acquisition.qualification_engine import enabled_for, enqueue
    if enabled_for(member.account_id):
        await enqueue(db, member, reason="event:" + kind, event=data)


async def acknowledge(db: Any, member: Any, event: dict | None) -> bool:
    from app.core.group.models import GroupAccountMembership

    # Serialize with callback projection. Scope and generation must still match.
    scope = (member.account_id, member.group_id, member.joined_at)
    locked = await db.scalar(select(GroupAccountMembership).where(
        GroupAccountMembership.id == member.id,
    ).with_for_update(of=GroupAccountMembership).execution_options(populate_existing=True))
    if (locked is None or scope != (locked.account_id, locked.group_id, locked.joined_at)
            or locked.status not in {"joined", "pending"}
            or locked.ad_status == "paused"
            or locked.review_status in {"manual_required", "exit_pending", "owned_group_excluded"}):
        return False
    current = await pending(db, member)
    if current != event:
        return False
    row = await db.get(SystemSetting, key(member.id), populate_existing=True)
    # Use only the generation actually compared above. A reconnect may commit
    # immediately afterwards; reading its newer marker here would swallow it.
    generation = event.get("account_generation") if event else (
        json.loads(row.value).get("account_generation") if row else None
    )
    cleared = {"joined_at": member.joined_at.isoformat() if member.joined_at else None,
               "kinds": [], "account_generation": generation}
    if event:
        # Only the exact acknowledged generation may contribute replay IDs.
        # Older IDs beyond this bounded window remain unknown and can wake work.
        history = _source_history(event)
        for kind, token in (event.get("sources") or {}).items():
            if isinstance(kind, str) and isinstance(token, str) and token:
                identity = _source_identity(kind, token)
                if identity not in history:
                    history.append(identity)
        if history:
            cleared["source_history"] = history[-SOURCE_HISTORY_LIMIT:]
    elif row is not None:
        # A no-event completion must preserve the existing consumed identities.
        history = _source_history(json.loads(row.value))
        if history:
            cleared["source_history"] = history
    clear = json.dumps(cleared)
    if row is None:
        db.add(SystemSetting(key=key(member.id), value=clear))
    else:
        row.value = clear
    await db.flush()
    from app.core.account.review_completions import confirm_event_consumed
    confirm_event_consumed(db, member, event)
    return True


async def account_gap(db: Any, account_id: int) -> None:
    """Record an explicitly observed account continuity gap.

    Callers must invoke this only after a real Telegram difference/recovery
    failure.  Merely replacing or reconnecting a client is not evidence that
    any group's authorization changed.
    """
    row = await db.get(SystemSetting, account_key(account_id))
    # Prefix new markers so old reconnect-only UUIDs can be retired without
    # losing the ability to fan out an explicitly observed account gap.
    generation = "gap:" + uuid4().hex
    if row is None:
        db.add(SystemSetting(key=account_key(account_id), value=generation))
    else:
        row.value = generation


async def queue_due_gaps(db: Any, account_id: int, *, limit: int = 2) -> int:
    """Queue at most two changed grants, independently of their ad schedules."""
    from app.modules.acquisition.qualification_engine import enabled_for, queue_pending_events
    if enabled_for(account_id):
        return await queue_pending_events(db, account_id, limit=limit)
    from app.core.group.models import GroupAccountMembership
    from app.modules.acquisition.models import GroupQualificationAudit
    from app.modules.acquisition.qualification_due import current_clause
    from app.modules.acquisition.qualification_service import latest_review
    from app.modules.acquisition.review_waits import event_backoff

    members = (await db.scalars(select(GroupAccountMembership).where(
        GroupAccountMembership.account_id == account_id,
        GroupAccountMembership.status == "joined",
        GroupAccountMembership.review_status == "approved",
        GroupAccountMembership.ad_status == "active",
    ).order_by(GroupAccountMembership.review_next_at, GroupAccountMembership.id).limit(300))).all()
    count = 0
    for member in members:
        if count >= max(1, min(2, limit)):
            break
        event = await pending(db, member)
        if not event or event.get("queued"):
            continue
        audit = await latest_review(db, member.id, include_pending=True)
        if audit is None:
            continue
        audit = await db.scalar(select(GroupQualificationAudit).where(
            GroupQualificationAudit.id == audit.id,
            GroupQualificationAudit.state.in_(["queued", "completed"]),
            current_clause(),
        ).with_for_update(of=GroupQualificationAudit, skip_locked=True)
            .execution_options(populate_existing=True))
        if audit is None or audit.decision not in {"allowed", "trial"}:
            continue
        now = datetime.utcnow()
        if await event_backoff(db, audit, now):
            continue
        member = await db.scalar(select(GroupAccountMembership).where(
            GroupAccountMembership.id == member.id,
        ).with_for_update(of=GroupAccountMembership, skip_locked=True)
            .execution_options(populate_existing=True))
        if (member is None or member.account_id != account_id
                or member.status != "joined" or member.review_status != "approved"
                or member.ad_status != "active"):
            continue
        if event != await pending(db, member):
            continue
        if audit.membership_joined_at != member.joined_at:
            continue
        latest = await latest_review(db, member.id, include_pending=True)
        if latest is None or latest.id != audit.id:
            continue
        if not event.get("token"):
            await request(db, member, "gap")
            await db.flush()
        stored = await db.get(SystemSetting, key(member.id), populate_existing=True)
        data = json.loads(stored.value)
        data["queued"] = True
        stored.value = json.dumps(data)
        # A concrete event must preempt the old periodic renewal timer.  The
        # event_backoff check above has already filtered out real operational
        # waits (budget, FloodWait, lease contention, etc.); the remaining
        # future timer is the legacy/periodic maintenance horizon and must not
        # hide newly observed authorization evidence.
        # Keep an already-due timestamp stable.  It is useful forensic state
        # for the scheduler and avoids making a fresh callback look like a
        # new retry attempt.  A future periodic timer is still preempted by
        # the concrete event and moves to the current queue time.
        queue_at = audit.next_retry_at if audit.next_retry_at is not None and audit.next_retry_at <= now else now
        audit.state, audit.next_retry_at = "queued", queue_at
        member.review_next_at = audit.next_retry_at
        count += 1
    return count
