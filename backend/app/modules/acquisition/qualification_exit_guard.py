"""Recheck mutable send restrictions at the final leave-RPC boundary."""

import json
from datetime import datetime
from typing import Any

from sqlalchemy import func, select

from app.modules.acquisition.adaptive_frequency import payload
from app.modules.acquisition.models import AdDeliveryLog, GroupAdFrequency, GroupAdFrequencyEvent
from app.modules.acquisition.send_restriction import (
    as_time,
    restriction_facts,
    telegram_member,
    track_restriction,
    verified_long_restriction,
)

CLEARED = "qualification_send_restriction_cleared"
MUTE_REASONS = {
    "frequency_muted",
    "account_permanent_send_restriction",
    "account_long_send_restriction",
}


def live_restriction(entity: Any, permission: Any, now: datetime) -> dict | None:
    """Unknown rights never authorize a destructive action."""
    if (
        permission is None
        or not all(
            hasattr(permission, key)
            for key in (
                "is_admin",
                "is_creator",
                "has_left",
                "participant",
            )
        )
        or not hasattr(entity, "default_banned_rights")
    ):
        return None
    participant = permission.participant
    own = getattr(participant, "banned_rights", None) or getattr(entity, "banned_rights", None)
    member = telegram_member(entity, permission)
    blocked = []
    for rights in (own, entity.default_banned_rights):
        if rights is None or not any(
            getattr(rights, key, False) for key in ("send_messages", "send_plain")
        ):
            continue
        end = as_time(getattr(rights, "until_date", None))
        if end is not None and datetime(1970, 1, 2) < end <= now:
            continue
        blocked.append(rights)
    return {
        "permissions": {
            "member": member,
            "can_send_text": member
            and not blocked
            and getattr(permission, "send_messages", None) is not False,
            **restriction_facts(blocked, now),
        },
        "collected_at": now.isoformat(),
    }


async def revoke_mute_exit(
    db: Any, account_id: int, group: Any, member: Any, now: datetime
) -> None:
    """Retain original evidence and append scoped revocations; keep unrelated exits."""
    pairs = (
        list(
            (
                await db.execute(
                    select(GroupAdFrequencyEvent, AdDeliveryLog)
                    .join(AdDeliveryLog, AdDeliveryLog.id == GroupAdFrequencyEvent.log_id)
                    .where(
                        GroupAdFrequencyEvent.kind == "muted",
                        GroupAdFrequencyEvent.created_at <= now,
                        AdDeliveryLog.account_id == account_id,
                        AdDeliveryLog.group_id == group.id,
                        func.coalesce(AdDeliveryLog.sent_at, AdDeliveryLog.created_at)
                        >= member.joined_at,
                    )
                )
            ).all()
        )
        if member.joined_at
        else []
    )
    for key in sorted({event.telegram_group_id for event, _ in pairs}):
        state = await db.scalar(
            select(GroupAdFrequency)
            .where(GroupAdFrequency.telegram_group_id == key)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if state is None:
            continue
        for event, log in pairs:
            if event.telegram_group_id != key:
                continue
            exists = await db.scalar(
                select(GroupAdFrequencyEvent.id).where(
                    GroupAdFrequencyEvent.log_id == log.id,
                    GroupAdFrequencyEvent.kind == "mute_revoked",
                )
            )
            if not exists:
                db.add(
                    GroupAdFrequencyEvent(
                        telegram_group_id=key,
                        log_id=log.id,
                        kind="mute_revoked",
                        epoch=event.epoch,
                        old_quota=state.quota,
                        new_quota=state.quota,
                        created_at=now,
                    )
                )
        await db.flush()
        revoked = select(GroupAdFrequencyEvent.log_id).where(
            GroupAdFrequencyEvent.kind == "mute_revoked"
        )
        remaining = await db.scalar(
            select(GroupAdFrequencyEvent.id)
            .where(
                GroupAdFrequencyEvent.telegram_group_id == key,
                GroupAdFrequencyEvent.kind == "muted",
                ~GroupAdFrequencyEvent.log_id.in_(revoked),
            )
            .limit(1)
        )
        deletions = (
            await db.scalars(
                select(AdDeliveryLog)
                .join(GroupAdFrequencyEvent, GroupAdFrequencyEvent.log_id == AdDeliveryLog.id)
                .where(
                    GroupAdFrequencyEvent.telegram_group_id == key,
                    GroupAdFrequencyEvent.epoch == state.epoch,
                    GroupAdFrequencyEvent.kind == "deleted",
                )
            )
        ).all()
        minimum_deleted = any(
            (payload(log.qualification_context_json).get("frequency") or {}).get(
                "sent_quota",
                (payload(log.qualification_context_json).get("frequency") or {}).get("quota"),
            )
            == 1
            for log in deletions
        )
        if state.reason == "frequency_muted" and not remaining:
            if minimum_deleted:
                state.reason = "frequency_deleted_at_minimum"
            else:
                state.status, state.reason = "active", None
                state.epoch += 1
                state.epoch_started_at = now
            state.updated_at = now


async def recheck_mute_exit(
    db: Any, account_id: int, group: Any, member: Any, entity: Any, permission: Any, now: datetime
) -> str | None:
    from app.modules.acquisition.qualification_service import (
        annotate_newcomer_restriction,
        current_authorization,
        enqueue_membership_review,
    )

    snapshot = live_restriction(entity, permission, now)
    if snapshot is None or snapshot["permissions"]["member"] is not True:
        return "qualification_current_permission_unknown"
    row, current_group, current_member = await current_authorization(db, account_id, group.group_id)
    if current_group is None or current_member is None or current_member.id != member.id:
        return "qualification_membership_changed"
    previous = (
        payload(row.evidence_json) if row and row.membership_joined_at == member.joined_at else {}
    )
    for key in ("verification_pending", "verification_required"):
        if (previous.get("permissions") or {}).get(key) is True:
            return "qualification_exit_verification_pending"
    last_sent = await db.scalar(
        select(func.max(AdDeliveryLog.sent_at)).where(
            AdDeliveryLog.account_id == account_id,
            AdDeliveryLog.group_id == group.id,
            AdDeliveryLog.status == "success",
        )
    )
    annotate_newcomer_restriction(snapshot, member.joined_at, now)
    track_restriction(snapshot, previous, now, last_sent_at=last_sent)
    if verified_long_restriction(snapshot, now):
        return None
    await revoke_mute_exit(db, account_id, group, member, now)
    member.status, member.left_at, member.leave_confirmed_at = "joined", None, None
    member.leave_retry_at, member.leave_error = None, CLEARED
    review = await enqueue_membership_review(
        db, member, batch_id=f"mute-exit-cleared:{member.id}:{now.isoformat()}"
    )
    review.evidence_json = json.dumps({**snapshot, "trigger": CLEARED})
    await db.commit()
    return CLEARED
