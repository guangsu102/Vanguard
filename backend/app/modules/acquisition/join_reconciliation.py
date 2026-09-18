"""Reconcile completed join approvals without making Telegram requests."""

import json
from typing import Any

from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.group.models import GroupAccountMembership
from app.modules.acquisition.models import AutoJoinAttempt, DeliveryStatus


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
