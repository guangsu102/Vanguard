"""Dependency-scoped renewal progress; dynamic role facts expire separately."""

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

ROLE_FRESHNESS = timedelta(minutes=5)
MAX_CHECKED = 256


def _event(event: dict) -> dict:
    return json.loads(json.dumps({key: value for key, value in event.items() if key != "queued"}))


def _identity(scope: dict) -> str:
    return hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()


def legacy_resume(previous: dict, event: dict, member: Any, now: datetime) -> dict:
    """Original exact-event, five-minute resume used with the stage disabled."""
    from app.modules.acquisition.evidence_progress import date

    scope = {"event": _event(event), "membership": member.id,
             "joined_at": member.joined_at.isoformat() if member.joined_at else None,
             "policy": previous.get("policy_version"), "profile": previous.get("profile_fingerprint")}
    identity = _identity(scope)
    saved = previous.get("renewal_progress") or {}
    started = date(saved.get("started_at"))
    if saved.get("scope") == identity and started and now-ROLE_FRESHNESS <= started <= now:
        return {**saved, "checked": dict(saved.get("checked") or {})}
    return {"scope": identity, "started_at": now.isoformat(), "checked": {}}


def resume(previous: dict, event: dict, member: Any, now: datetime) -> dict:
    """Keep stable work across waits, invalidating only provably affected facts.

    The event is retained as an input dependency, not an acknowledgement. The
    existing compare-and-clear still decides which event a task may complete.
    Message bodies are always read live by the renewal; checked signatures do
    not establish current author membership once its own freshness expires.
    """
    from app.modules.acquisition.evidence_progress import date

    joined_at = member.joined_at.isoformat() if member.joined_at else None
    scope = {
        "membership": member.id,
        "account": getattr(member, "account_id", None),
        "group": getattr(member, "group_id", None),
        "joined_at": joined_at,
        "policy": previous.get("policy_version"),
        "profile": previous.get("profile_fingerprint"),
        "system_identity": previous.get("system_identity_fingerprint"),
        "system_user_ids": previous.get("system_user_ids"),
    }
    identity = _identity(scope)
    current_event = _event(event)
    saved = previous.get("renewal_progress") or {}
    saved = saved if isinstance(saved, dict) else {}
    started = date(saved.get("started_at"))
    valid = saved.get("scope") == identity
    if saved.get("version") != 2:
        # Read old checkpoints only for their exact original event/scope. Their
        # old start time remains the fallback age of individual role checks.
        legacy_scope = {
            "event": current_event,
            "membership": member.id,
            "joined_at": joined_at,
            "policy": previous.get("policy_version"),
            "profile": previous.get("profile_fingerprint"),
        }
        valid = saved.get("scope") == _identity(legacy_scope)
    if not valid or started is None or started > now:
        return {
            "version": 2,
            "scope": identity,
            "event": current_event,
            "started_at": now.isoformat(),
            "checked": {},
            "checked_at": {},
        }
    result = {
        **saved,
        "version": 2,
        "scope": identity,
        "event": current_event,
        "checked": dict(saved.get("checked") or {}),
        "checked_at": dict(saved.get("checked_at") or {}),
    }
    old_event = saved.get("event", current_event)
    old_event = old_event if isinstance(old_event, dict) else {}
    if old_event != current_event:
        # Only a concrete message-only change has a bounded dependency set.
        # A gap/role change without IDs cannot establish unaffected authors.
        ids = current_event.get("message_ids") or []
        precise = (
            set(current_event.get("kinds") or []) <= {"rules", "membership"}
            and old_event.get("account_generation") == current_event.get("account_generation")
            and old_event.get("unscoped_rules_token") == current_event.get("unscoped_rules_token")
            and ("rules" not in current_event.get("kinds", []) or bool(ids))
        )
        changed = {str(value) for value in ids} if precise else set(result["checked"])
        for key in changed:
            result["checked"].pop(key, None)
            result["checked_at"].pop(key, None)
        # A changed event may include new participant visibility/permissions.
        # Retry capability checks, rather than inheriting an old denial.
        result.pop("admin_list_denied", None)
        result.pop("admin_list_retry_at", None)
    keys = list(result["checked"])[-MAX_CHECKED:]
    result["checked"] = {key: result["checked"][key] for key in keys}
    result["checked_at"] = {
        key: result["checked_at"][key] for key in keys if key in result["checked_at"]
    }
    return result


def checked_fresh(progress: dict | None, message_id: str, digest: str, now: datetime) -> bool:
    """A stable message signature alone never extends an author-role check."""
    from app.modules.acquisition.evidence_progress import date

    if progress is None or (progress.get("checked") or {}).get(message_id) != digest:
        return False
    checked_at = date((progress.get("checked_at") or {}).get(message_id))
    checked_at = checked_at or date(progress.get("started_at"))
    return checked_at is not None and now - ROLE_FRESHNESS <= checked_at <= now


def record_checked(progress: dict | None, message_id: str, digest: str, now: datetime) -> None:
    if progress is not None:
        progress.setdefault("checked", {})[message_id] = digest
        progress.setdefault("checked_at", {})[message_id] = now.isoformat()
        while len(progress["checked"]) > MAX_CHECKED:
            oldest = next(iter(progress["checked"]))
            progress["checked"].pop(oldest)
            progress["checked_at"].pop(oldest, None)


def signature(message: Any) -> str:
    from app.modules.acquisition.group_qualification import naive, text_of

    value = [
        message.id,
        getattr(message, "sender_id", None),
        text_of(message),
        str(naive(getattr(message, "date", None))),
        str(naive(getattr(message, "edit_date", None))),
    ]
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


async def administrator_ids(client: Any, entity: Any, progress: dict | None) -> set[int] | None:
    """A complete live admin list replaces many per-author rule lookups.

    This is only for rule authors; ordinary-member ad precedent still needs a
    live membership check. Permission failures fall back to bounded individual
    lookups and are not retried inside the same short continuation.
    """
    from telethon import errors, types, utils
    from telethon.tl.functions.channels import GetParticipantsRequest

    from app.modules.acquisition.evidence_progress import date

    if not isinstance(entity, types.Channel):
        return None
    if (progress or {}).get("admin_list_denied"):
        retry = date(progress.get("admin_list_retry_at"))
        if retry is None:
            started = date(progress.get("started_at"))
            retry = started + ROLE_FRESHNESS if started else None
        if retry is not None and datetime.utcnow() < retry:
            return None
        progress.pop("admin_list_denied", None)
        progress.pop("admin_list_retry_at", None)
    ids: set[int] = set()
    offset = 0
    try:
        for _ in range(3):
            result = await client(
                GetParticipantsRequest(
                    utils.get_input_channel(entity),
                    types.ChannelParticipantsAdmins(),
                    offset,
                    100,
                    0,
                )
            )
            participants = getattr(result, "participants", None)
            if participants is None:
                return None
            ids.update(p.user_id for p in participants if getattr(p, "user_id", None))
            offset += len(participants)
            if offset >= result.count:
                return ids if len(ids) >= result.count else None
            if not participants:
                return None
    except errors.ChatAdminRequiredError:
        if progress is not None:
            progress["admin_list_denied"] = True
            progress["admin_list_retry_at"] = (datetime.utcnow() + ROLE_FRESHNESS).isoformat()
    return None  # A partial list must never establish that an author is ordinary.
