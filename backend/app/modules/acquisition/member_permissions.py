"""Only current Telegram membership and text permissions authorize growth ads."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.modules.acquisition.send_restriction import as_time, telegram_member


@dataclass(frozen=True)
class Permission:
    member: bool | None
    writable: bool | None
    reason: str
    retry_at: datetime | None = None
    exit_required: bool = False


def inspect_permission(entity: Any, permissions: Any, now: datetime) -> Permission:
    if permissions is None or not hasattr(permissions, "is_admin"):
        return Permission(None, None, "permission_unknown", now + timedelta(minutes=10))
    if not telegram_member(entity, permissions) or getattr(permissions, "is_banned", False):
        return Permission(False, False, "banned_in_channel" if getattr(permissions, "is_banned", False) else "not_member", exit_required=True)
    if getattr(permissions, "is_admin", False) or getattr(permissions, "is_creator", False):
        return Permission(True, False, "owned_group_protected")
    participant = getattr(permissions, "participant", None)
    own = getattr(participant, "banned_rights", None) or getattr(entity, "banned_rights", None)
    defaults = getattr(entity, "default_banned_rights", None)
    restrictions = []
    for rights in (own, defaults):
        if rights is None:
            continue
        until = as_time(getattr(rights, "until_date", None))
        permanent = until is None or until <= datetime(1970, 1, 2)
        if not permanent and until <= now:
            continue
        if any(getattr(rights, name, False) for name in ("send_messages", "send_plain", "view_messages")):
            restrictions.append((permanent, until))
    if restrictions:
        permanent = any(item[0] for item in restrictions)
        until = max((item[1] for item in restrictions if item[1]), default=None)
        long = permanent or bool(until and until > now + timedelta(days=3))
        return Permission(True, False, "permanent_mute" if permanent else "temporary_mute",
                          None if permanent else until, long)
    # Explicit SDK denial without rights evidence is not proof of permanent mute.
    if getattr(permissions, "send_messages", None) is False:
        return Permission(True, False, "text_not_allowed", now + timedelta(minutes=10))
    if getattr(entity, "send_paid_messages_stars", 0):
        return Permission(True, False, "paid_messages_not_authorized")
    return Permission(True, True, "text_allowed")


def apply_permission(member: Any, fact: Permission, now: datetime) -> None:
    member.permission_checked_at = now
    member.can_send_text = fact.writable
    member.permission_reason = fact.reason
    member.review_next_at = fact.retry_at
    if fact.member is False:
        member.status, member.left_at = "left", now
        member.review_status, member.ad_status = "rejected", "blocked"
    elif fact.writable is True and member.verification_status == "complete":
        member.review_status, member.ad_status = "approved", "active"
        member.review_next_at = now + timedelta(days=1)
        member.warmup_status = "ad_eligible"
        member.first_ad_allowed_at = now
        member.next_ad_allowed_at = member.next_ad_allowed_at or now
    else:
        if fact.writable is True:
            member.permission_reason = "verification_pending"
        member.review_status = "rejected" if fact.exit_required else "pending"
        member.ad_status = "blocked"
        if fact.exit_required and member.verification_status != "pending":
            member.leave_requested_at = now
            member.leave_retry_at = now
            member.leave_error = fact.reason
        elif member.review_next_at is None:
            member.review_next_at = now + timedelta(minutes=10)
