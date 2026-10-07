"""Scoped change work, separate from an enduring qualification grant.

Only local state is touched. A compare-and-clear acknowledges exactly the
generation read by a worker, so a later callback cannot be lost on completion.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from app.core.settings_models import SystemSetting


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
    if data.get("account_generation") != generation:
        data = {**data, "kinds": sorted(set(data.get("kinds", [])) | {"gap"}),
                "account_generation": generation, "queued": False}
    return data if data.get("kinds") else None


async def pending(db: Any, member: Any) -> dict | None:
    row = await db.get(SystemSetting, key(member.id), populate_existing=True)
    marker = await db.get(SystemSetting, account_key(member.account_id), populate_existing=True)
    return pending_value(row.value if row else None, member.joined_at, marker.value if marker else None)


async def request(db: Any, member: Any, kind: str, *, message_ids=(), source_token: str | None = None) -> None:
    """Caller locks membership, including when there is no prior event row."""
    old = await pending(db, member) or {}
    sources = dict(old.get("sources") or {})
    if source_token and sources.get(kind) == source_token and kind in old.get("kinds", []):
        return  # Exact journal fact replay; a later edit has a different token.
    if source_token:
        sources[kind] = source_token
    data = {
        "joined_at": member.joined_at.isoformat() if member.joined_at else None,
        "kinds": sorted(set(old.get("kinds", [])) | {kind}),
        "message_ids": sorted(set(old.get("message_ids", [])) | set(message_ids))[-32:],
        "token": uuid4().hex,
        "queued": False,
        "account_generation": old.get("account_generation"),
        "sources": sources,
    }
    row = await db.get(SystemSetting, key(member.id))
    if row is None:
        db.add(SystemSetting(key=key(member.id), value=json.dumps(data)))
    else:
        row.value = json.dumps(data)
    # Callback writes only its durable cue; it never waits for an audit lock.
    member.review_next_at = datetime.utcnow()


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
    clear = json.dumps({"joined_at": member.joined_at.isoformat() if member.joined_at else None,
                        "kinds": [], "account_generation": generation})
    if row is None:
        db.add(SystemSetting(key=key(member.id), value=clear))
    else:
        row.value = clear
    await db.flush()
    return True


async def account_gap(db: Any, account_id: int) -> None:
    """One marker only. No group scans, blanket invalidation or review fan-out."""
    row = await db.get(SystemSetting, account_key(account_id))
    generation = uuid4().hex
    if row is None:
        db.add(SystemSetting(key=account_key(account_id), value=generation))
    else:
        row.value = generation


async def queue_due_gaps(db: Any, account_id: int, *, limit: int = 2) -> int:
    """Repair at most two actually due ad targets per dispatcher pass."""
    from app.core.group.models import GroupAccountMembership
    from app.modules.acquisition.models import AdDeliveryScheduleState, GroupQualificationAudit
    from app.modules.acquisition.qualification_service import latest_review

    members = (await db.scalars(select(GroupAccountMembership).outerjoin(
        AdDeliveryScheduleState,
        (AdDeliveryScheduleState.group_id == GroupAccountMembership.group_id)
        & (AdDeliveryScheduleState.account_id == GroupAccountMembership.account_id),
    ).where(
        GroupAccountMembership.account_id == account_id,
        GroupAccountMembership.status == "joined",
        GroupAccountMembership.review_status == "approved",
        GroupAccountMembership.ad_status == "active",
        (AdDeliveryScheduleState.id.is_(None)) | (
            (AdDeliveryScheduleState.next_due_at <= datetime.utcnow())
            & AdDeliveryScheduleState.status.in_(["idle", "retry"])),
    ).order_by(AdDeliveryScheduleState.next_due_at, GroupAccountMembership.id).limit(300))).unique().all()
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
            GroupQualificationAudit.state != "running",
        ).with_for_update(of=GroupQualificationAudit, skip_locked=True)
            .execution_options(populate_existing=True))
        if audit is None or audit.decision not in {"allowed", "trial"}:
            continue
        if audit.next_retry_at is not None and audit.next_retry_at > datetime.utcnow():
            continue  # Keep the reader's backoff even if another callback arrived.
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
        audit.state, audit.next_retry_at = "queued", audit.next_retry_at or datetime.utcnow()
        member.review_next_at = audit.next_retry_at
        count += 1
    return count
