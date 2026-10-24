"""Reconcile completed join approvals without making Telegram requests."""

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.core.config import settings
from app.core.group.identity import telegram_group_id_aliases
from app.core.group.models import GroupAccountMembership
from app.modules.acquisition.models import (
    AutoJoinAttempt,
    DeliveryStatus,
    GroupQualificationAudit,
)

JOIN_REQUEST_PENDING_TIMEOUT_DAYS = 7

_RECONCILIATION_TERMINAL_STATES = ("confirmed", "resolved")
_COUNTED_REQUEST_STATES = ("sent", "outcome_unknown")


async def joined_membership_for_attempt(
    db: AsyncSession,
    attempt: AutoJoinAttempt,
    *,
    lock: bool = False,
) -> GroupAccountMembership | None:
    """Return a live local membership that can prove a request succeeded.

    A stale request may be observed after the membership sync has already
    recorded ``joined``.  Releasing that request would discard a real group
    and free a slot that is still occupied.  Match by the stable database
    group when available, fall back to Telegram ID aliases for older rows,
    and require an observation at or after the original request timestamp.
    """

    predicates = [
        GroupAccountMembership.account_id == attempt.account_id,
        GroupAccountMembership.status == "joined",
        GroupAccountMembership.left_at.is_(None),
    ]
    if attempt.group_id is None and attempt.telegram_group_id is None:
        return None

    async def lookup(extra: ColumnElement[bool]) -> GroupAccountMembership | None:
        query = select(GroupAccountMembership).where(*predicates, extra).order_by(
            GroupAccountMembership.joined_at.desc().nullslast(),
            GroupAccountMembership.id.desc(),
        ).limit(1)
        if lock:
            query = query.with_for_update(of=GroupAccountMembership, skip_locked=True)
        return await db.scalar(query)

    # Prefer the stable database identity.  Older attempts can retain a stale
    # or missing group FK after a group record was merged/deleted, so fall back
    # to the Telegram ID aliases when the FK lookup finds nothing.
    membership = (
        await lookup(GroupAccountMembership.group_id == attempt.group_id)
        if attempt.group_id is not None
        else None
    )
    if membership is None and attempt.telegram_group_id is not None:
        membership = await lookup(
            GroupAccountMembership.telegram_group_id.in_(
                telegram_group_id_aliases(attempt.telegram_group_id)
            )
        )
    if membership is None:
        return None

    reference = attempt.request_sent_at or attempt.attempted_at
    observations = [value for value in (membership.joined_at, membership.last_checked_at) if value]
    if not observations or (reference and max(observations) < reference):
        return None
    return membership


def _read_successful_join_audit(note: str | None) -> bool:
    if not note:
        return False
    decoder = json.JSONDecoder()
    source = note.lstrip()
    try:
        audit, _ = decoder.raw_decode(source)
        return isinstance(audit, dict) and audit.get("passed") is True
    except (TypeError, json.JSONDecodeError):
        pass

    # The old formatter cut JSON at exactly 4000 characters. Recover only a
    # complete, typed decision header when truncation is inside later evidence;
    # never infer success by searching arbitrary text for a 'passed' substring.
    if len(note) != 4000 or not source.startswith("{"):
        return False
    fields: dict[str, Any] = {}
    offset = 1
    while offset < len(source):
        try:
            offset += len(source[offset:]) - len(source[offset:].lstrip())
            key, offset = decoder.raw_decode(source, offset)
            if not isinstance(key, str) or key in fields:
                return False
            offset += len(source[offset:]) - len(source[offset:].lstrip())
            if source[offset : offset + 1] != ":":
                return False
            offset += 1
            offset += len(source[offset:]) - len(source[offset:].lstrip())
            try:
                value, offset = decoder.raw_decode(source, offset)
            except json.JSONDecodeError as exc:
                truncated_tail = exc.pos >= len(source) - 1 or exc.msg.startswith(
                    "Unterminated string"
                )
                return (
                    truncated_tail
                    and key in {"ad_rule_details", "verification_details", "leave_error"}
                    and fields.get("passed") is True
                    and "reason" in fields
                    and fields["reason"] is None
                    and fields.get("can_send_messages") is True
                    and isinstance(fields.get("permission_reason"), str)
                    and type(fields.get("message_count")) is int
                    and type(fields.get("text_messages")) is int
                    and type(fields.get("chinese_messages")) is int
                    and type(fields.get("should_leave")) is bool
                    and "verification_action" in fields
                )
            fields[key] = value
            offset += len(source[offset:]) - len(source[offset:].lstrip())
            if source[offset : offset + 1] != ",":
                return False
            offset += 1
        except (TypeError, json.JSONDecodeError):
            return False
    return False


async def expire_stale_join_requests(
    db: AsyncSession,
    *,
    now: datetime,
    limit: int = 100,
    account_id: int | None = None,
) -> dict[str, Any]:
    """Release known pending approvals using only local timestamps.

    A Telegram read is useful for confirming an approval, but it must not be
    required to retire a request that has exceeded the local approval SLA.
    This lane only closes local accounting and pending membership state; it
    never sends, cancels, or replays a Telegram request.
    """
    cutoff = now - timedelta(days=JOIN_REQUEST_PENDING_TIMEOUT_DAYS)
    budget = max(1, min(int(limit), 100))
    request_started_at = func.coalesce(
        AutoJoinAttempt.request_sent_at,
        AutoJoinAttempt.attempted_at,
    )
    query = (
        select(AutoJoinAttempt)
        .where(
            AutoJoinAttempt.status != DeliveryStatus.SUCCESS.value,
            AutoJoinAttempt.telegram_action_attempted.is_(True),
            # An unknown write keeps its target fenced until facts resolve it;
            # this local expiry lane only handles a confirmed sent request.
            AutoJoinAttempt.request_state == "sent",
            AutoJoinAttempt.reconciliation_status.not_in(_RECONCILIATION_TERMINAL_STATES),
            request_started_at <= cutoff,
        )
        .order_by(request_started_at, AutoJoinAttempt.id)
        .with_for_update(of=AutoJoinAttempt, skip_locked=True)
        .limit(budget)
    )
    if account_id is not None:
        query = query.where(AutoJoinAttempt.account_id == account_id)
    from app.core.account.reconciliation_stream import legacy_account_filter
    query = query.where(legacy_account_filter(AutoJoinAttempt.account_id))
    rows = list((await db.scalars(query)).all())
    details: list[dict[str, Any]] = []
    for attempt in rows:
        joined = await joined_membership_for_attempt(db, attempt, lock=True)
        if joined is not None:
            attempt.status = DeliveryStatus.SUCCESS.value
            attempt.reason = "joined_membership_reconciled"
            attempt.error = None
            attempt.joined_at = joined.joined_at or joined.last_checked_at or attempt.request_sent_at
            # The Telegram request was already sent, so retain the counted
            # request state while closing reconciliation as confirmed.
            attempt.request_state = "sent"
            attempt.reservation_released_at = None
            attempt.reservation_expires_at = None
            attempt.reconciliation_status = "confirmed"
            attempt.reconciliation_checked_at = now
            attempt.reconciliation_next_at = None
            if settings.BUDGET_LEDGER_ENABLED:
                from app.core.account.budget_ledger import settle
                await settle(db, f"join:{attempt.id}", 0, reason="joined_membership_reconciled")
            await db.execute(
                update(GroupQualificationAudit)
                .where(
                    GroupQualificationAudit.batch_id == f"join-attempt:{attempt.id}:pending",
                    GroupQualificationAudit.state.in_(
                        ["waiting_membership", "manual_required", "queued"]
                    ),
                )
                .values(
                    state="cancelled",
                    reason="join_membership_confirmed",
                    next_retry_at=None,
                )
            )
            details.append(
                {
                    "attempt_id": attempt.id,
                    "account_id": attempt.account_id,
                    "group_id": attempt.group_id,
                    "status": attempt.status,
                    "reason": attempt.reason,
                }
            )
            continue
        attempt.status = DeliveryStatus.FAILED.value
        attempt.reason = "join_request_timeout"
        attempt.error = "approval_not_received_before_timeout"
        attempt.request_state = "released"
        attempt.reservation_released_at = now
        attempt.reservation_expires_at = None
        attempt.reconciliation_status = "resolved"
        attempt.reconciliation_checked_at = now
        attempt.reconciliation_next_at = None
        if settings.BUDGET_LEDGER_ENABLED:
            from app.core.account.budget_ledger import settle
            await settle(db, f"join:{attempt.id}", 0, reason="join_request_timeout")
        membership_filters = [
            GroupAccountMembership.account_id == attempt.account_id,
            GroupAccountMembership.status == "pending",
        ]
        if attempt.group_id is not None:
            membership_filters.append(GroupAccountMembership.group_id == attempt.group_id)
        elif attempt.telegram_group_id is not None:
            membership_filters.append(
                GroupAccountMembership.telegram_group_id.in_(
                    telegram_group_id_aliases(attempt.telegram_group_id)
                )
            )
        if attempt.group_id is not None or attempt.telegram_group_id is not None:
            await db.execute(
                update(GroupAccountMembership)
                .where(*membership_filters)
                .values(status="rejected", ad_status="blocked", updated_at=now)
            )
        await db.execute(
            update(GroupQualificationAudit)
            .where(
                GroupQualificationAudit.batch_id == f"join-attempt:{attempt.id}:pending",
                GroupQualificationAudit.state.in_(["waiting_membership", "manual_required", "queued"]),
            )
            .values(state="cancelled", reason="join_request_timeout", next_retry_at=None)
        )
        details.append(
            {
                "attempt_id": attempt.id,
                "account_id": attempt.account_id,
                "group_id": attempt.group_id,
                "status": attempt.status,
                "reason": attempt.reason,
            }
        )
    if rows:
        await db.commit()
    return {"checked": len(rows), "updated": len(rows), "details": details}


async def reconcile_joined_auto_join_attempts(
    db: AsyncSession,
    *,
    limit: int = 100,
    account_id: int | None = None,
    group_id: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Complete only the latest real request confirmed by a later successful audit.

    The membership's joined_at may be the request submission time. Use the
    successful audit time when the attempt has no confirmed join timestamp.
    Keep attempted_at and telegram_action_attempted unchanged so this repair
    neither creates another join nor consumes another daily request budget.
    """
    attempts = AutoJoinAttempt.__table__
    memberships = GroupAccountMembership.__table__
    newer = attempts.alias("newer_join_attempt")
    newer_request = exists(
        select(newer.c.id).where(
            newer.c.account_id == attempts.c.account_id,
            newer.c.group_id == attempts.c.group_id,
            newer.c.telegram_action_attempted.is_(True),
            or_(
                newer.c.attempted_at > attempts.c.attempted_at,
                and_(
                    newer.c.attempted_at == attempts.c.attempted_at,
                    newer.c.id > attempts.c.id,
                ),
            ),
        )
    ).correlate(attempts)
    confirmed_membership = exists(
        select(memberships.c.id).where(
            memberships.c.account_id == attempts.c.account_id,
            memberships.c.group_id == attempts.c.group_id,
            memberships.c.join_method == "auto_keyword_search",
            memberships.c.status == "joined",
            memberships.c.left_at.is_(None),
            memberships.c.last_checked_at >= attempts.c.attempted_at,
        )
    ).correlate(attempts)
    eligible = (
        attempts.c.status == DeliveryStatus.PENDING.value,
        attempts.c.telegram_action_attempted.is_(True),
        attempts.c.reason.in_(["join_request_pending", "verification_pending_recheck"]),
        confirmed_membership,
        ~newer_request,
    )
    query = (
        select(
            attempts.c.id,
            attempts.c.account_id,
            attempts.c.group_id,
            attempts.c.joined_at,
            memberships.c.last_checked_at,
            memberships.c.note,
        )
        .join(
            memberships,
            and_(
                memberships.c.account_id == attempts.c.account_id,
                memberships.c.group_id == attempts.c.group_id,
            ),
        )
        .where(*eligible)
        .order_by(attempts.c.id)
    )
    if account_id is not None:
        query = query.where(attempts.c.account_id == account_id)
    if group_id is not None:
        query = query.where(attempts.c.group_id == group_id)

    result: dict[str, Any] = {"checked": 0, "updated": 0, "details": []}
    # Skip invalid historical notes without starving later valid approvals.
    cursor = 0
    batch_size = max(1, min(int(limit), 100))
    while result["updated"] < max(0, int(limit)) and result["checked"] < 1000:
        rows = (
            (
                await db.execute(
                    query.where(attempts.c.id > cursor).limit(
                        min(batch_size, 1000 - result["checked"])
                    )
                )
            )
            .mappings()
            .all()
        )
        if not rows:
            break
        for row in rows:
            cursor = row["id"]
            result["checked"] += 1
            if not _read_successful_join_audit(row["note"]):
                continue

            joined_at = row["joined_at"] or row["last_checked_at"]
            if not dry_run:
                changed = await db.execute(
                    update(attempts)
                    .where(
                        attempts.c.id == row["id"],
                        *eligible,
                        exists(
                            select(memberships.c.id).where(
                                memberships.c.account_id == row["account_id"],
                                memberships.c.group_id == row["group_id"],
                                memberships.c.note == row["note"],
                                memberships.c.last_checked_at == row["last_checked_at"],
                            )
                        ),
                    )
                    .values(
                        status=DeliveryStatus.SUCCESS.value,
                        reason=None,
                        error=None,
                        joined_at=joined_at,
                    )
                    .returning(attempts.c.id)
                )
                if changed.scalar_one_or_none() is None:
                    continue
            result["updated"] += 1
            result["details"].append(
                {
                    "attempt_id": row["id"],
                    "account_id": row["account_id"],
                    "group_id": row["group_id"],
                    "status": DeliveryStatus.SUCCESS.value,
                    "reason": "joined_membership_reconciled",
                    "joined_at": joined_at.isoformat(),
                    "dry_run": dry_run,
                }
            )
            if result["updated"] >= limit:
                break
    if result["updated"] and not dry_run:
        await db.commit()
    return result


def unresolved_join_request_due(now: datetime) -> ColumnElement[bool]:
    stale_unresolved = and_(
        AutoJoinAttempt.status != DeliveryStatus.SUCCESS.value,
        AutoJoinAttempt.telegram_action_attempted.is_(True),
        AutoJoinAttempt.request_state.in_(_COUNTED_REQUEST_STATES),
        func.coalesce(
            AutoJoinAttempt.request_sent_at,
            AutoJoinAttempt.attempted_at,
        )
        <= now - timedelta(days=JOIN_REQUEST_PENDING_TIMEOUT_DAYS),
    )
    active_request = or_(
        AutoJoinAttempt.request_state == "outcome_unknown",
        and_(
            AutoJoinAttempt.request_state == "sent",
            AutoJoinAttempt.status == DeliveryStatus.PENDING.value,
        ),
    )
    return and_(
        or_(
            AutoJoinAttempt.target_key.is_not(None),
            AutoJoinAttempt.group_id.is_not(None),
            AutoJoinAttempt.telegram_group_id.is_not(None),
        ),
        AutoJoinAttempt.reconciliation_status.not_in(_RECONCILIATION_TERMINAL_STATES),
        # A request past the local approval SLA must wake the maintenance lane
        # even when a previous read failure left a later retry timestamp.  The
        # caller runs expire_stale_join_requests before network reconciliation,
        # so this path closes the request locally without another Telegram read.
        or_(
            stale_unresolved,
            and_(
                or_(
                    AutoJoinAttempt.reconciliation_next_at.is_(None),
                    AutoJoinAttempt.reconciliation_next_at <= now,
                ),
                active_request,
            ),
        ),
    )
