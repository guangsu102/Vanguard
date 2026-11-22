"""Final read checks and separately authorized exit actions."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from weakref import WeakKeyDictionary

from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.models import TelegramAccount
from app.core.account.rpc_governor import RpcDeferred
from app.core.account.telegram_execution import TelegramExecutionError
from app.core.config import get_settings
from app.core.group.models import Group, GroupAccountMembership
from app.modules.acquisition.qualification_service import (
    _date,
    assess,
    current_authorization,
    exit_scope_filters,
    policy,
    qualification_exit_fact,
    qualifies_for_automatic_exit,
)


async def validate_live_send(db: Any, client: Any, account_id: int, target: int | str) -> None:
    """The send boundary consumes local authorization; callbacks schedule any reads."""
    from app.modules.acquisition.qualification_events import pending
    from app.modules.acquisition.qualification_lifetime import authorization_current

    row, group, member = await current_authorization(db, account_id, target)
    now = datetime.utcnow()
    if (member is None or group is None or not authorization_current(row, now)
            or member.status != "joined" or member.review_status != "approved"
            or member.ad_status in {"blocked", "paused"}):
        raise TelegramExecutionError("qualification_review_required")
    from app.modules.acquisition.qualification_engine import enabled_for, event_blocks_authorization
    change = await pending(db, member)
    if change and (not enabled_for(account_id) or event_blocks_authorization(json.loads(row.evidence_json or "{}"), change)):
        raise TelegramExecutionError("qualification_event_pending", retry_after_seconds=60)
    snapshot = json.loads(row.evidence_json or "{}")
    if snapshot.get("invalidated_at") or snapshot.get("protected"):
        raise TelegramExecutionError("qualification_review_required")
    permissions = snapshot.get("permissions") or {}
    if permissions.get("member") is False or permissions.get("can_send_text") is False:
        raise TelegramExecutionError("qualification_current_permission_changed")
    if permissions.get("paid_messages"):
        raise TelegramExecutionError("qualification_paid_messages_not_authorized")
    for field in ("slowmode_until", "temporary_until", "newcomer_until", "restricted_until"):
        deadline = _date(permissions.get(field))
        if deadline and deadline > now:
            raise TelegramExecutionError("qualification_wait_condition")


_REFRESH_AUDIT_FIELDS = (
    "id", "membership_id", "account_id", "group_id", "membership_joined_at",
    "policy_version", "content_scope", "attempts", "state", "decision", "reason",
    "evidence_json", "evidence_hash", "checked_at", "expires_at", "next_retry_at",
    "checkpoint_revision",
)
_REFRESH_MEMBER_FIELDS = (
    "id", "account_id", "group_id", "telegram_group_id", "joined_at", "left_at",
    "status", "review_status", "ad_status", "ad_pause_until", "review_next_at",
)
_REFRESH_GROUP_FIELDS = ("id", "group_id", "username")
_REFRESH_ACCOUNT_FIELDS = (
    "id", "profile_bio", "status", "is_active", "risk_level", "risk_pause_until",
)
_expected_refresh_scope: ContextVar[dict[str, Any] | None] = ContextVar(
    "qualification_expected_refresh_scope", default=None,
)


def _refresh_values(value: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: getattr(value, field, None) for field in fields}


async def _refresh_scope(db: Any, row: Any, group: Any, member: Any) -> dict[str, Any]:
    """Capture the generation before releasing the read transaction."""
    from app.modules.acquisition.qualification_events import pending

    account = await db.get(TelegramAccount, member.account_id, populate_existing=True)
    return {
        "audit": _refresh_values(row, _REFRESH_AUDIT_FIELDS),
        "member": _refresh_values(member, _REFRESH_MEMBER_FIELDS),
        "group": _refresh_values(group, _REFRESH_GROUP_FIELDS),
        "account": _refresh_values(account, _REFRESH_ACCOUNT_FIELDS),
        "event": deepcopy(await pending(db, member)),
        "policy": deepcopy(await policy(db, fresh=True)),
    }


async def _release_refresh_transaction(db: Any, row: Any, member: Any) -> None:
    """Commit only this scoped snapshot; never commit another caller's writes.

    SQLAlchemy retains flushed mutations in the transaction rollback snapshot.
    Public dirty/new collections alone miss an already flushed write, so both
    are checked. An unsupported transaction implementation fails closed.
    """
    if not isinstance(db, AsyncSession):
        return  # Lightweight offline collectors do not own pooled connections.
    from app.core.database_transaction_boundary import (
        TransactionBoundaryBlocked,
        release_read_transaction,
    )
    from app.modules.acquisition.qualification_events import key

    try:
        await release_read_transaction(
            db, allowed_objects=(row, member), allowed_setting_keys=(key(member.id),),
            allowed_object_fields={row: frozenset({"evidence_json"}),
                                   member: frozenset({"review_next_at", "updated_at"})},
        )
    except TransactionBoundaryBlocked as exc:
        raise TelegramExecutionError("qualification_transaction_pending", retry_after_seconds=60) from exc


async def _lock_refresh_scope(db: Any, scope: dict[str, Any]) -> tuple[Any, Any, Any]:
    """Recheck audit -> member locks before applying any detached read result."""
    from app.modules.acquisition.models import GroupQualificationAudit
    from app.modules.acquisition.qualification_events import pending, same_evidence_event

    with db.no_autoflush:
        row = await db.scalar(select(GroupQualificationAudit).where(
            GroupQualificationAudit.id == scope["audit"]["id"],
        ).with_for_update(of=GroupQualificationAudit).execution_options(populate_existing=True))
        member = await db.scalar(select(GroupAccountMembership).where(
            GroupAccountMembership.id == scope["member"]["id"],
        ).with_for_update(of=GroupAccountMembership).execution_options(populate_existing=True))
        group = await db.get(Group, scope["group"]["id"], populate_existing=True)
        account = await db.get(TelegramAccount, scope["account"]["id"], populate_existing=True)
        valid = (
            row is not None and member is not None and group is not None and account is not None
            and _refresh_values(row, _REFRESH_AUDIT_FIELDS) == scope["audit"]
            and _refresh_values(member, _REFRESH_MEMBER_FIELDS) == scope["member"]
            and _refresh_values(group, _REFRESH_GROUP_FIELDS) == scope["group"]
            and _refresh_values(account, _REFRESH_ACCOUNT_FIELDS) == scope["account"]
            and same_evidence_event(await pending(db, member), scope["event"])
            and await policy(db, fresh=True) == scope["policy"]
        )
        if valid:
            current, _, _ = await current_authorization(db, member.account_id, group.group_id)
            valid = current is not None and current.id == row.id
    if not valid:
        # This transaction has performed only fresh reads. Commit releases its
        # locks without expiring callers' ORM instances or changing any verdict.
        await db.commit()
        raise TelegramExecutionError("qualification_refresh_scope_changed", retry_after_seconds=60)
    return row, group, member


def _detached_refresh_row(value: Any) -> SimpleNamespace:
    return SimpleNamespace(**{
        column.key: getattr(value, column.key) for column in inspect(value).mapper.column_attrs
    })


async def refresh_live_authorization(
    db: Any, client: Any, account_id: int, target: int | str,
    *, permissions_only: bool = False, message_ids: list[int] | None = None,
    recheck_rules: bool = False, progress: dict | None = None,
    event_evidence: list[Any] | None = None,
    shared_read_context: Any = None,
    handoff_scope: dict | None = None,
) -> None:
    """Read without a business connection, then apply to the same generation."""
    options = {"permissions_only": permissions_only, "message_ids": message_ids,
               "recheck_rules": recheck_rules, "progress": progress, "event_evidence": event_evidence,
               "shared_read_context": shared_read_context, "handoff_scope": handoff_scope}
    from app.modules.acquisition.qualification_engine import enabled_for, enqueue
    if enabled_for(account_id):
        # Event repair is executed by the fenced work runner. Direct callers
        # cannot rewrite the audit behind the active authorization pointer.
        _, _, member = await current_authorization(db, account_id, target)
        if member is not None:
            await enqueue(db, member, reason="event")
        raise TelegramExecutionError("qualification_event_pending", retry_after_seconds=60)
    if not isinstance(db, AsyncSession):
        await _read_live_authorization(db, client, account_id, target, **options)
        return
    row, group, member = await current_authorization(db, account_id, target)
    if row is None or group is None or member is None:
        raise TelegramExecutionError("qualification_review_required")
    expected_scope = _expected_refresh_scope.get()
    if expected_scope is not None:
        # Account acquisition/cache work may yield to a newer claim. Release
        # only the caller-owned snapshot before refreshing identity-map rows,
        # then reject that newer generation before making any Telegram read.
        await _release_refresh_transaction(db, row, member)
        row, group, member = await _lock_refresh_scope(db, expected_scope)
    from app.modules.acquisition.qualification_continuity import OWN_SOURCE, own_proof_valid

    previous = json.loads(row.evidence_json or "{}")
    if previous.get("authorization_basis") == OWN_SOURCE and not await own_proof_valid(
        db, previous, member, group,
    ):
        raise TelegramExecutionError("qualification_own_delivery_unconfirmed")
    scope = await _refresh_scope(db, row, group, member)
    if expected_scope is not None and scope != expected_scope:
        await _release_refresh_transaction(db, row, member)
        raise TelegramExecutionError("qualification_refresh_scope_changed", retry_after_seconds=60)
    detached = tuple(_detached_refresh_row(item) for item in (row, group, member))
    await _release_refresh_transaction(db, row, member)
    effects: dict[str, Any] = {}
    failure = None
    try:
        await _read_live_authorization(
            db, client, account_id, target, **options,
            authorization=detached, effects=effects,
        )
    except Exception as exc:
        failure = exc
    # Cancellation never applies a partial read. The original event and claim
    # remain recoverable through the existing bounded cancellation handler.
    row, group, member = await _lock_refresh_scope(db, scope)
    for field in ("evidence_json", "expires_at", "reason", "state", "decision", "next_retry_at"):
        value = getattr(detached[0], field)
        if value != scope["audit"][field]:
            setattr(row, field, value)
    for field in ("review_status", "ad_status"):
        value = getattr(detached[2], field)
        if value != scope["member"][field]:
            setattr(member, field, value)
    if effects.get("live_mute") is not None:
        from app.modules.acquisition.adaptive_frequency import record_live_mute

        await record_live_mute(db, account_id, group.group_id, effects["live_mute"])
    await db.commit()
    if expected_scope is not None:
        # Only this validated invocation can advance the caller's expected
        # evidence version. A new claim/callback must never be adopted here.
        expected_scope["audit"] = _refresh_values(row, _REFRESH_AUDIT_FIELDS)
        expected_scope["member"] = _refresh_values(member, _REFRESH_MEMBER_FIELDS)
    if failure is not None:
        raise failure


async def _read_live_authorization(
    db: Any, client: Any, account_id: int, target: int | str,
    *, permissions_only: bool = False, message_ids: list[int] | None = None,
    recheck_rules: bool = False, progress: dict | None = None,
    event_evidence: list[Any] | None = None,
    shared_read_context: Any = None,
    handoff_scope: dict | None = None,
    authorization: tuple[Any, Any, Any] | None = None,
    effects: dict[str, Any] | None = None,
) -> None:
    """Bounded repair after a concrete change/gap, never a per-send precheck."""
    from telethon.tl.types import InputMessagesFilterPinned

    from app.modules.acquisition.group_qualification import (
        WARNING,
        EvidenceCollector,
        evidence_age,
        naive,
        possible_rule,
        text_of,
        trial_proof,
    )
    from app.modules.acquisition.qualification_identity import (
        entity_identity,
        identity_relation,
        peer_identity,
    )
    from app.modules.acquisition.qualification_read_context import QualificationReadContext
    from app.modules.acquisition.review_read_flags import stage_enabled

    row, group, member = authorization or await current_authorization(db, account_id, target)
    if row is None or group is None or member is None:
        raise TelegramExecutionError("qualification_review_required")
    snapshot = json.loads(row.evidence_json or "{}")
    from app.modules.acquisition.qualification_continuity import OWN_SOURCE, own_proof_valid
    established = snapshot.get("authorization_basis") == OWN_SOURCE
    if authorization is None and established and not await own_proof_valid(db, snapshot, member, group):
        raise TelegramExecutionError("qualification_own_delivery_unconfirmed")
    from app.modules.acquisition.adaptive_frequency import canonical
    marked_target = canonical(group.group_id, snapshot)
    if marked_target is None:
        raise TelegramExecutionError("qualification_group_identity_unknown", retry_after_seconds=300)
    context = shared_read_context or QualificationReadContext(enabled=stage_enabled(account_id, "reuse"))
    # A bare positive legacy group ID is a PeerUser to Telethon. Resolve only
    # the namespace proved by the saved group evidence, then recheck identity.
    try:
        entity = await context.get_entity(client, getattr(group, "username", None) or marked_target)
    except ValueError as exc:
        raise TelegramExecutionError("qualification_entity_unavailable", retry_after_seconds=300) from exc
    if (
        identity_relation(entity_identity(entity), peer_identity(group.group_id, snapshot))
        != "same"
    ):
        raise TelegramExecutionError("qualification_identity_changed")
    if handoff_scope is not None:
        from app.modules.acquisition.review_fact_owner import bind_context
        await bind_context(context, entity, handoff_scope, snapshot)
    response = await context.full_info(client, entity)
    full = response.full_chat
    current = await context.permissions(client, entity, "me", full_chat=full)
    if current is None or not hasattr(current, "is_admin"):
        raise TelegramExecutionError("qualification_current_permission_unknown")
    if getattr(current, "is_admin", False) or getattr(current, "is_creator", False):
        raise TelegramExecutionError("qualification_protected_membership")
    own = getattr(getattr(current, "participant", None), "banned_rights", None)
    defaults = getattr(entity, "default_banned_rights", None)
    from app.modules.acquisition.send_restriction import telegram_member
    is_member = telegram_member(entity, current)
    if (not getattr(current, "has_left", False)
            and any(getattr(own, field, False) for field in ("view_messages", "send_messages", "send_plain"))
            and not getattr(current, "is_admin", False) and not getattr(current, "is_creator", False)):
        from app.modules.acquisition.adaptive_frequency import record_live_mute
        from app.modules.acquisition.qualification_service import annotate_newcomer_restriction
        from app.modules.acquisition.send_restriction import restriction_facts, track_restriction
        observed_at = datetime.utcnow()
        live = {**snapshot, "technical_errors": [], "collected_at": observed_at.isoformat(),
                "permissions": {**restriction_facts([own], observed_at),
                    "member": not bool(getattr(current, "is_banned", False)
                                       or getattr(entity, "left", False)
                                       or getattr(own, "view_messages", False)),
                    "can_send_text": False}}
        annotate_newcomer_restriction(live, member.joined_at, observed_at)
        track_restriction(live, snapshot, observed_at)
        if effects is None:
            await record_live_mute(db, account_id, group.group_id, live)
        else:
            effects["live_mute"] = live
    if (
        not is_member
        or any(
            getattr(rights, field, False)
            for rights in (own, defaults)
            for field in ("view_messages", "send_messages", "send_plain")
        )
    ):
        raise TelegramExecutionError("qualification_current_permission_changed")
    now = datetime.utcnow()
    slow = getattr(full, "slowmode_next_send_date", None)
    if isinstance(slow, int):
        slow = datetime.utcfromtimestamp(slow)
    if isinstance(slow, datetime) and naive(slow) > now:
        raise TelegramExecutionError("qualification_slowmode_wait")
    if getattr(full, "send_paid_messages_stars", 0):
        raise TelegramExecutionError("qualification_paid_messages_not_authorized")
    snapshot["permissions"] = {**snapshot.get("permissions", {}),
                               "member": True, "can_send_text": True,
                               "paid_messages": False,
                               "slowmode_until": None, "temporary_until": None}
    row.evidence_json = json.dumps(snapshot, ensure_ascii=False)
    deferred_rules = snapshot.get("rules_evaluation_status") == "NOT_EVALUATED"
    if permissions_only and not deferred_rules:
        return
    async def invalidate(reason: str, *, unknown: bool = False, retry_after: int = 0) -> None:
        if unknown and ("read_failed" in reason or "identity_failed" in reason):
            # Transport/lookup failures do not prove that previous facts changed.
            raise TelegramExecutionError(reason, retry_after_seconds=retry_after or 60)
        row.expires_at = now
        row.reason = reason
        row.state, row.decision = "completed", "observe"
        row.next_retry_at = now + timedelta(seconds=retry_after)
        member.review_status, member.ad_status = "initial_pending", "warming"
        snapshot["decision"], snapshot["reason"] = "observe", reason
        snapshot["invalidated_at"] = now.isoformat()
        if unknown:
            snapshot["unknowns"] = sorted(set(snapshot.get("unknowns", []) + [reason]))
        row.evidence_json = json.dumps(snapshot, ensure_ascii=False)
        # This invalidation must survive the outer send failure.
        if effects is None:
            await db.commit()
        raise TelegramExecutionError(
            "qualification_rules_unknown" if unknown else "qualification_rules_changed",
            retry_after_seconds=retry_after or None,
        )

    # Online count is an exit fact only; it is not an additional ad permission gate.
    online_count = getattr(full, "online_count", None)
    from app.modules.acquisition.qualification_engine import enabled_for
    efficiency = enabled_for(account_id)
    if not efficiency and (snapshot.get("exit_group_rule_ban") is True or deferred_rules):
        if not isinstance(online_count, int) or online_count < 0:
            try:
                online_count = getattr(await context.onlines(client, entity), "onlines", None)
            except RpcDeferred:
                raise
            except Exception as exc:
                if "Flood" in type(exc).__name__:
                    raise
                online_count = None
        if deferred_rules and (recheck_rules or type(online_count) is not int or online_count < 2):
            # An unread rule set cannot be treated as an empty/no-ban result.
            # Return to the original evidence path when it becomes relevant.
            await invalidate("deferred_rules_require_evidence", unknown=True)
        if snapshot.get("exit_group_rule_ban") is True and type(online_count) is int and 0 <= online_count < 2:
            await invalidate("exit_group_ad_ban_and_online_below_2_before_send")
    members = getattr(full, "participants_count", None)
    if not efficiency and type(members) is int and 0 <= members < 50:
        await invalidate("exit_members_below_50_before_send")
    if permissions_only:
        return

    own_ids = snapshot.get("system_user_ids")
    if (
        snapshot.get("system_identity_coverage") is not True
        or not isinstance(own_ids, list)
        or any(type(value) is not int or value <= 0 for value in own_ids)
    ):
        await invalidate("system_account_identity_unconfirmed_before_send", unknown=True)
    if established and not recheck_rules:
        # Already verified own 24h survival is local evidence. No repeated history,
        # third-party post or AI reads; live identity/rights/slowmode checks above remain.
        return
    # One context belongs to this invocation/event only. It cannot lend a
    # previous continuation's permissions or visibility to a fresh read.
    read_context = context if context.enabled else None
    rules_required = recheck_rules or getattr(row, "decision", None) != "trial"
    old_admin = {
        item.get("message_id"): item
        for item in snapshot.get("evidence", [])
        if item.get("source") == "admin_rule"
    }
    rule_ids = list(dict.fromkeys([*(message_ids or []), *old_admin])) if rules_required else []
    targeted_messages = None
    proof = []
    # Trial authorization is based on an observed ordinary-member precedent.
    # Explicit group-rule permission is a separate route and needs no such post.
    if getattr(row, "decision", None) != "allowed" and not established:
        proof = trial_proof(
            [item for item in snapshot.get("evidence", []) if item.get("topic_id") is None]
        )
        if len(proof) != 1:
            await invalidate("advertising_precedent_unconfirmed_before_send", unknown=True)
    if read_context is not None and event_evidence:
        live_ids = set(old_admin) | {item["message_id"] for item in proof}
        event_ids = set(message_ids or []) - live_ids
        # Only exact, scoped event bodies may be reused. The selected proof
        # and prior admin-rule messages always require a live targeted read.
        read_context.remember_messages(client, entity, [
            item for item in event_evidence if getattr(item, "id", None) in event_ids
        ])
    # Selected precedent and established rule anchors retain the original live
    # check even when an interrupted task handed off raw event messages.
    if hasattr(context, "mandatory_live_ids"):
        context.mandatory_live_ids.update(set(old_admin) | {item["message_id"] for item in proof})
    if proof:
        try:
            # Proof, concrete events and known rules can overlap. Fetch their
            # union now so later consumers do not repeat the same RPC or ID.
            present = (
                await read_context.get_messages(
                    client, entity,
                    [item["message_id"] for item in proof] + rule_ids,
                ) if read_context is not None
                else await client.get_messages(entity, ids=[item["message_id"] for item in proof])
            )
            targeted_messages = present if read_context is not None else None
        except RpcDeferred:
            raise
        except Exception as exc:
            await invalidate(
                "advertising_precedent_read_failed_before_send:" + type(exc).__name__,
                unknown=True,
                retry_after=max(120, int(getattr(exc, "seconds", 0) or 0)),
            )
            return
        current_ads = {
            getattr(message, "id", None): message
            for message in (present if isinstance(present, list) else [present])
            if message is not None
        }
        proof_collector = EvidenceCollector(
            client, own_user_ids=set(own_ids), read_context=read_context,
            account_id=account_id,
        )
        proof_collector.full_chat = full
        for prior in proof:
            message = current_ads.get(prior["message_id"])
            if message is None:
                await invalidate("advertising_precedent_disappeared_before_send")
            if (
                getattr(message, "sender_id", None) != prior["sender_id"]
                or text_of(message) != prior["text"]
                or (naive(getattr(message, "date", None)) or datetime.min).isoformat()
                != prior.get("date")
                or (
                    naive(getattr(message, "edit_date", None)).isoformat()
                    if getattr(message, "edit_date", None)
                    else None
                )
                != prior.get("edited_at")
                or (evidence_age(message, now) or 0) < 24
            ):
                await invalidate("advertising_precedent_changed_before_send")
            try:
                role = await proof_collector.role(entity, message)
            except RpcDeferred:
                raise
            except Exception as exc:
                await invalidate(
                    "advertising_precedent_identity_failed_before_send:" + type(exc).__name__,
                    unknown=True,
                    retry_after=max(120, int(getattr(exc, "seconds", 0) or 0)),
                )
                return
            if role != "ordinary":
                await invalidate("advertising_precedent_identity_changed_before_send", unknown=True)

    if getattr(row, "decision", None) == "trial" and not recheck_rules:
        # The retained ordinary-member ad was re-read and its sender verified above.
        # Group-rule permission is a separate fallback route, not a second veto.
        return

    old_rules = [
        item
        for item in snapshot.get("evidence", [])
        if item.get("source") in {"full_about", "pinned_message"}
    ]
    old_about = [item["text"].strip() for item in old_rules if item["source"] == "full_about"]
    about = str(getattr(full, "about", "") or "").strip()
    old_pins = {
        (item.get("message_id"), item.get("text", "").strip(), item.get("edited_at"))
        for item in old_rules
        if item["source"] == "pinned_message"
    }
    try:
        pins = [
            message
            async for message in client.iter_messages(
                entity, filter=InputMessagesFilterPinned(), limit=201
            )
        ]
        if read_context is None:
            latest = (
                [item for item in await client.get_messages(entity, ids=message_ids) if item is not None]
                if message_ids else []
            )
            current_admin = await client.get_messages(entity, ids=list(old_admin)) if old_admin else []
        else:
            current_messages = (
                targeted_messages if targeted_messages is not None
                else await read_context.get_messages(client, entity, rule_ids)
            )
            by_id = {
                getattr(message, "id", None): message
                for message in current_messages if message is not None
            }
            latest = [by_id[mid] for mid in dict.fromkeys(message_ids or []) if mid in by_id]
            current_admin = [by_id[mid] for mid in old_admin if mid in by_id]
    except RpcDeferred:
        raise
    except Exception as exc:
        await invalidate(
            "rules_read_failed_before_send:" + type(exc).__name__,
            unknown=True,
            retry_after=max(120, int(getattr(exc, "seconds", 0) or 0)),
        )
        return

    if len(pins) >= 201:
        await invalidate("pinned_coverage_unknown_before_send", unknown=True)
    if any(getattr(message, "media", None) and not text_of(message) for message in pins):
        await invalidate("pinned_media_unknown_before_send", unknown=True)
    if message_ids and not set(message_ids) <= {getattr(message, "id", None) for message in latest}:
        await invalidate("event_rules_unavailable", unknown=True)
    new_pins = {
        (
            getattr(message, "id", None),
            text_of(message),
            naive(message.edit_date).isoformat() if getattr(message, "edit_date", None) else None,
        )
        for message in pins
    }
    if old_about != ([about] if about else []) or old_pins != new_pins:
        await invalidate("rules_changed_before_send")
    found_admin = {
        getattr(message, "id", None): message
        for message in current_admin or []
        if message is not None
    }
    for message_id, prior in old_admin.items():
        message = found_admin.get(message_id)
        if message is None:
            await invalidate("admin_rule_unavailable_before_send", unknown=True)
        edited = naive(getattr(message, "edit_date", None))
        current_edit = edited.isoformat() if edited else None
        if text_of(message) != prior.get("text", "").strip() or (
            "edited_at" in prior and current_edit != prior["edited_at"]
        ):
            await invalidate("admin_rule_changed_before_send")

    since = naive(getattr(row, "checked_at", None)) or now - timedelta(hours=24)
    dates = [naive(getattr(message, "date", None)) for message in latest]
    if any(date is None for date in dates):
        await invalidate("recent_rules_coverage_unknown_before_send", unknown=True)
    collector = EvidenceCollector(
        client, own_user_ids=set(own_ids), read_context=read_context,
        account_id=account_id,
    )
    collector.full_chat = full
    from app.modules.acquisition.renewal_progress import (
        administrator_ids,
        checked_fresh,
        record_checked,
        signature,
    )
    checked = progress.setdefault('checked', {}) if progress is not None else {}
    candidates = [message for message, date in zip(latest, dates, strict=True)
                  if max(date, naive(getattr(message, 'edit_date', None)) or date) > since
                  and possible_rule(text_of(message))]
    # With a complete list each pass rechecks roles against today's admins,
    # including authors previously observed in an interrupted continuation.
    admins = await administrator_ids(client, entity, progress) if len(candidates) >= 3 else None
    for message, date in zip(latest, dates, strict=True):
        edited = naive(getattr(message, "edit_date", None))
        if max(date, edited or date) <= since:
            continue
        text = text_of(message)
        if not possible_rule(text):
            continue
        digest = signature(message)
        if admins is None and checked_fresh(progress, str(message.id), digest, now):
            continue
        try:
            sender = getattr(message, 'sender', None)
            if admins is not None and getattr(message, 'sender_id', None) in admins:
                role = 'admin'
            elif admins is not None and sender is not None and getattr(message, 'sender_id', 0) > 0:
                role = 'bot' if getattr(sender, 'bot', False) else 'ordinary'
            else:
                role = await collector.role(entity, message)
        except RpcDeferred:
            raise
        except Exception as exc:
            await invalidate(
                "admin_identity_read_failed_before_send:" + type(exc).__name__,
                unknown=True,
                retry_after=max(120, int(getattr(exc, "seconds", 0) or 0)),
            )
            return
        if role in {"unknown", "anonymous"}:
            await invalidate("admin_identity_unknown_before_send", unknown=True)
        if role == "admin" or (role == "bot" and WARNING.search(text)):
            # A new administrative statement is a new rule version even when
            # the same words previously appeared in ordinary members' messages.
            await invalidate("admin_rules_or_feedback_changed_before_send")
        checked[str(message.id)] = digest
        record_checked(progress, str(message.id), digest, now)


async def reconcile_exit(service: Any, member: GroupAccountMembership, group: Group, *, read_only_recovery: bool = False) -> str:
    """Only confirmed non-membership can close an uncertain leave."""
    wrapper = None
    membership_query_started = False
    try:
        # Scheduler cleanup closes the global pool after every task. A retry
        # must load its account before trying to acquire it in this fresh pool.
        account = await service.db.get(TelegramAccount, member.account_id)
        if account is None:
            return "unknown"
        await service.account_pool.add_account_from_db(account)
        wrapper = await service.account_pool.acquire_by_id(
            member.account_id, purpose="qualification_exit_reconcile", raise_on_lease_failure=True,
            allow_restricted=read_only_recovery
        )
        if wrapper is None or wrapper.client is None:
            return "unknown"
        entity, error = await service._resolve_group_entity_for_leave(
            wrapper.client, service._discovered_group_from_model(group)
        )
        if entity is None:
            return "unknown"
        from app.core.group.identity import telegram_group_id_aliases

        if getattr(entity, "id", None) not in telegram_group_id_aliases(group.group_id):
            return "unknown"
        membership_query_started = True
        permissions = await wrapper.client.get_permissions(entity, "me")
        if permissions is None or not hasattr(permissions, "has_left"):
            return "unknown"
        if getattr(permissions, "is_admin", False) or getattr(permissions, "is_creator", False):
            return "protected"
        return "left" if getattr(permissions, "has_left", False) else "member"
    except Exception as exc:
        return (
            "left"
            if membership_query_started and type(exc).__name__ == "UserNotParticipantError"
            else "unknown"
        )
    finally:
        if wrapper is not None:
            await service.account_pool.release(wrapper)


async def record_confirmed_exit(db: Any, member: GroupAccountMembership, group: Group) -> None:
    # current_authorization refreshes ORM membership state from the database.
    # Persist the confirmed left state before that refresh can replace it.
    await db.flush()
    from app.modules.acquisition.qualification_engine import enabled_for, business_decision
    if enabled_for(member.account_id):
        row = await business_decision(db, member, require_fresh=False)
        if row is not None:
            from app.modules.acquisition.models import GroupQualificationAudit
            snapshot = {**json.loads(row.evidence_json or "{}"), "exit_confirmed_at": member.leave_confirmed_at.isoformat(), "exit_source_audit_id": row.id}
            db.add(GroupQualificationAudit(
                batch_id=f"exit-confirmed:{row.id}:{member.leave_confirmed_at.isoformat()}",
                membership_id=member.id, account_id=member.account_id, group_id=member.group_id,
                membership_joined_at=member.joined_at, policy_version=row.policy_version, content_scope=row.content_scope,
                state="completed", decision="reject", reason=row.reason, checked_at=row.checked_at,
                expires_at=row.expires_at, evidence_json=json.dumps(snapshot, ensure_ascii=False, default=str),
                evidence_hash=row.evidence_hash,
            ))
        return
    row, _, _ = await current_authorization(db, member.account_id, group.group_id)
    if row is None:
        return
    snapshot = json.loads(row.evidence_json or "{}")
    history = list(snapshot.get("qualification_exit_history") or [])
    entry = next(
        (
            item
            for item in history
            if item.get("audit_id") == row.id
            and _date(item.get("checked_at")) == row.checked_at
            and item.get("evidence_hash") == row.evidence_hash
        ),
        None,
    )
    if entry is None:
        entry = qualification_exit_fact(row, snapshot)
        history.append(entry)
    entry["exit_confirmed_at"] = member.leave_confirmed_at.isoformat()
    snapshot["qualification_exit_history"] = history
    row.evidence_json = json.dumps(snapshot, ensure_ascii=False, default=str)


# Session-level PostgreSQL lock on a separate connection: business commits do
# not release it. All scheduler/manual runners share this one bounded exit lane.
EXIT_ADVISORY_LOCK_KEY = 20260922104118
_SQLITE_EXIT_LOCKS: WeakKeyDictionary[Any, asyncio.Lock] = WeakKeyDictionary()


@asynccontextmanager
async def exit_serialization_lock(db: Any):
    engine = getattr(db, "bind", None)
    if engine is not None and not hasattr(engine, "connect"):
        engine = getattr(engine, "engine", None)
    dialect = getattr(getattr(engine, "dialect", None), "name", None)
    if dialect == "sqlite" and get_settings().APP_ENV.lower() == "test":
        # SQLite is supported only in explicit test mode; never substitute a
        # process-local lock for a distributed production lock.
        lock = _SQLITE_EXIT_LOCKS.setdefault(engine, asyncio.Lock())
        if lock.locked():
            yield "qualification_exit_busy"
            return
        await lock.acquire()
        try:
            yield None
        finally:
            lock.release()
        return
    if dialect != "postgresql":
        yield "qualification_exit_lock_unsupported"
        return
    connection = None
    try:
        connection = await engine.connect()
        acquired = await connection.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": EXIT_ADVISORY_LOCK_KEY}
        )
        if acquired is True:
            # End the read transaction; the session advisory lock stays held.
            await connection.commit()
    except BaseException as exc:
        # An acquisition timeout may have acquired the server-side lock. Drop
        # this physical connection instead of returning it to the pool.
        if connection is not None:
            try:
                await connection.invalidate()
            finally:
                await connection.close()
        if not isinstance(exc, Exception):
            raise
        yield "qualification_exit_lock_unavailable"
        return
    if acquired is not True:
        await connection.close()
        yield "qualification_exit_busy"
        return
    try:
        yield None
    finally:
        try:
            unlocked = await connection.scalar(
                text("SELECT pg_advisory_unlock(:key)"), {"key": EXIT_ADVISORY_LOCK_KEY}
            )
            if unlocked is not True:
                await connection.invalidate()
        except BaseException:
            # Never put a connection with an uncertain held lock back in the pool.
            await connection.invalidate()
            raise
        finally:
            await connection.close()


async def reconcile_stale_exit_memberships(service: Any, config: dict, *, limit: int = 1, account_id: int | None = None) -> dict[str, int]:
    """Read old non-joined relations in a separate lane; never consume a leave slot."""
    from app.core.account.models import TelegramAccount
    from app.core.group.identity import is_owned_group_target
    from app.modules.acquisition.qualification_service import (
        enqueue_membership_review,
        manually_protected,
    )
    query = select(GroupAccountMembership).where(
        GroupAccountMembership.status.not_in(["joined", "leave_failed"]),
        GroupAccountMembership.review_status.in_(["exit_pending", "leave_failed", "membership_reconciliation"]),
        GroupAccountMembership.review_next_at <= datetime.utcnow(),
    )
    if not config.get("exit_all_accounts"):
        query = query.where(GroupAccountMembership.account_id.in_(config.get("account_ids") or []))
    if account_id is not None:
        query = query.where(GroupAccountMembership.account_id == account_id)
    members = (await service.db.scalars(query.order_by(GroupAccountMembership.review_next_at).limit(limit))).all()
    result = {"checked": 0, "restored": 0, "confirmed_absent": 0}
    for member in members:
        member.review_status = "membership_reconciliation"
        member.review_next_at = datetime.utcnow() + timedelta(hours=2)
        await service.db.commit()
        group = await service.db.get(Group, member.group_id)
        account = await service.db.get(TelegramAccount, member.account_id)
        if group is None or account is None:
            continue
        if manually_protected(config, group, member) or await is_owned_group_target(
            service.db, core_group_id=group.id, telegram_group_id=group.group_id
        ):
            member.review_status, member.ad_status = "owned_group_excluded", "blocked"
            member.review_next_at = None
            await service.db.commit()
            continue
        await service.account_pool.add_account_from_db(account)
        try:
            state = await asyncio.wait_for(reconcile_exit(service, member, group, read_only_recovery=True), timeout=45)
        except TimeoutError:
            state = "unknown"
        result["checked"] += 1
        if state == "member":
            member.status, member.ad_status = "joined", "blocked"
            member.left_at = member.leave_confirmed_at = None
            member.leave_error = None
            member.joined_at = member.joined_at or datetime.utcnow()
            await enqueue_membership_review(service.db, member,
                batch_id=f"stale-exit-recovery:{member.id}:{member.joined_at.isoformat()}")
            result["restored"] += 1
        elif state == "left":
            member.status, member.review_status, member.ad_status = "left", "left", "blocked"
            member.left_at = member.left_at or datetime.utcnow()
            member.leave_confirmed_at = datetime.utcnow()
            member.review_next_at = None
            member.leave_error = "non_membership_confirmed_without_leave_rpc"
            result["confirmed_absent"] += 1
        elif state == "protected":
            member.review_status, member.ad_status = "owned_group_excluded", "blocked"
            member.review_next_at = None
        else:
            member.leave_error = "membership_reconciliation_pending"
        await service.db.commit()
    return result


async def run_exits(service: Any, *, limit: int = 1, account_id: int | None = None) -> dict[str, Any]:
    from app.core.config import settings
    if settings.GROWTH_SIMPLIFIED_ENABLED:
        from app.modules.acquisition.simple_runtime import run
        return await run(service, "exit", account_id, limit=limit)
    from app.modules.acquisition.qualification_engine import run_business_maintenance
    await run_business_maintenance(service, account_id=account_id, limit=limit)
    config = await policy(service.db, fresh=True)
    if not config.get("enabled") or not config.get("execute_exits"):
        return {"processed": 0, "reason": "exits_paused"}
    try:
        membership_ids, reasons = exit_scope_filters(config)
    except ValueError as exc:
        return {"processed": 0, "reason": str(exc)}
    if membership_ids == set() or reasons == set():
        return {"processed": 0, "reason": "qualification_exit_scope_empty"}
    async with exit_serialization_lock(service.db) as blocked_reason:
        if blocked_reason:
            return {"processed": 0, "reason": blocked_reason}
        from app.modules.acquisition.adaptive_frequency import run_frequency_exits
        # Each class has its own small quantum. An unexecutable frequency exit
        # must never suppress ordinary exits, including for the same account.
        scoped = {"account_id": account_id} if account_id is not None else {}
        result = await run_frequency_exits(service, limit=limit, **scoped)
        ordinary = await _run_exits_serialized(service, limit=limit, **scoped)
        result["processed"] += ordinary["processed"]
        result.setdefault("results", []).extend(ordinary.get("results", []))
        result["membership_reconciliation"] = await reconcile_stale_exit_memberships(service, config, **scoped)
        return result


async def _run_exits_serialized(service: Any, *, limit: int = 1, account_id: int | None = None) -> dict[str, Any]:
    config = await policy(service.db, fresh=True)
    if not config.get("enabled") or not config.get("execute_exits"):
        return {"processed": 0, "reason": "exits_paused"}
    try:
        membership_ids, reasons = exit_scope_filters(config)
    except ValueError as exc:
        return {"processed": 0, "reason": str(exc)}
    if membership_ids == set() or reasons == set():
        return {"processed": 0, "reason": "qualification_exit_scope_empty"}
    now = datetime.utcnow()
    query = select(GroupAccountMembership).where(
        GroupAccountMembership.status.in_(["joined", "leave_failed"]),
        GroupAccountMembership.review_status.in_(["exit_pending", "leave_failed"]),
        GroupAccountMembership.review_next_at <= now,
    )
    if config.get("exit_all_accounts") is not True:
        query = query.where(GroupAccountMembership.account_id.in_(config.get("account_ids") or []))
    if membership_ids is not None:
        query = query.where(GroupAccountMembership.id.in_(membership_ids))
    from app.core.scheduler.growth_dispatch import eligible_accounts
    query = query.where(GroupAccountMembership.account_id.in_(eligible_accounts(now)))
    if account_id is not None:
        query = query.where(GroupAccountMembership.account_id == account_id)
    members = list(
        (
            await service.db.scalars(
                query.order_by(GroupAccountMembership.review_next_at).limit(limit)
            )
        ).all()
    )
    results = []
    for member in members:
        group = await service.db.get(Group, member.group_id)
        if group is None:
            member.review_status, member.ad_status = "exit_pending", "blocked"
            member.review_next_at = datetime.utcnow() + timedelta(hours=2)
            member.leave_error = "exit_qualification_group_missing"
            await service.db.commit()
            continue
        if member.leave_attempts:
            reconciled = await reconcile_exit(service, member, group)
            if reconciled == "left":
                member.status, member.review_status, member.ad_status = "left", "left", "blocked"
                member.left_at = member.leave_confirmed_at = datetime.utcnow()
                await record_confirmed_exit(service.db, member, group)
                member.review_next_at = member.leave_retry_at = None
                member.leave_error = None
                results.append({"membership_id": member.id, "status": "left", "reconciled": True})
                await service.db.commit()
                continue
            if reconciled in {"unknown", "protected"}:
                member.review_status = (
                    "owned_group_excluded" if reconciled == "protected" else "exit_pending"
                )
                member.review_next_at = (
                    None if reconciled == "protected" else datetime.utcnow() + timedelta(hours=2)
                )
                member.leave_error = "exit_reconciliation_" + reconciled
                continue
        # Renew evidence before an irreversible membership action.
        resolution = await current_authorization(service.db, member.account_id, group.group_id)
        row, current_group, current_member = resolution
        resolution_error = getattr(resolution, "reason", None)
        from app.modules.acquisition.qualification_engine import business_decision, enabled_for, enqueue_business_maintenance
        efficiency = enabled_for(member.account_id)
        if efficiency and current_member is not None:
            row = await business_decision(service.db, current_member)
            resolution_error = None if row is not None else "qualification_exit_review_required"
            if row is None:
                await enqueue_business_maintenance(service.db, current_member, refresh=True)
        if row is None or current_group is None or current_member is None:
            resolution_error = resolution_error or "qualification_review_required"
        elif current_group.id != group.id or current_member.id != member.id:
            resolution_error = "qualification_membership_changed"
        if resolution_error:
            member.review_status, member.ad_status = "exit_pending", "blocked"
            member.review_next_at = datetime.utcnow() + timedelta(hours=2)
            member.leave_error = "exit_" + resolution_error
            await service.db.commit()
            continue
        if efficiency:
            from app.modules.acquisition.automation import JoinedGroupAuditResult
            audit = JoinedGroupAuditResult(passed=False, verification_details={"qualification_decision": row.decision})
        else:
            audit = await assess(service, member.account_id, group, row=row, force_refresh=True)
        decision = audit.verification_details.get("qualification_decision")
        if decision == "observe" and row.state == "waiting_ai":
            # Model outages leave a prior rejection unresolved, not cleared.
            member.review_status, member.ad_status = "exit_pending", "blocked"
            member.review_next_at = row.next_retry_at or datetime.utcnow() + timedelta(minutes=5)
            member.leave_error = "exit_review_waiting_ai"
            results.append({"membership_id": member.id, "status": "exit_pending",
                            "reason": member.leave_error})
            await service.db.commit()
            continue
        if decision == "technical_wait":
            # A busy account lease or transport failure does not revoke the
            # prior rejection and must not silently drop this exit task.
            member.ad_status = "blocked"
            detail = str(audit.permission_reason or "technical_wait")[:160]
            member.review_status = "exit_pending"
            retry = row.next_retry_at or datetime.utcnow() + timedelta(hours=2)
            if detail == "AccountOperationLeaseBusy":
                retry = min(retry, datetime.utcnow() + timedelta(minutes=5))
            member.review_next_at = retry
            member.leave_error = "exit_review_technical_wait:" + detail
            results.append({"membership_id": member.id, "status": member.review_status,
                            "reason": member.leave_error})
            await service.db.commit()
            continue
        fresh_snapshot = json.loads(row.evidence_json or "{}")
        if decision != "reject" or not qualifies_for_automatic_exit(fresh_snapshot):
            if decision == "protected":
                member.review_status, member.ad_status, member.review_next_at = (
                    "owned_group_excluded", "blocked", None
                )
            elif decision in {"allowed", "trial"}:
                member.review_status, member.ad_status = "approved", "active"
                member.review_next_at = row.next_retry_at
            else:
                member.review_status, member.ad_status = "review_2h", "blocked"
                member.review_next_at = row.next_retry_at or datetime.utcnow() + timedelta(hours=2)
            member.leave_error = "exit_cancelled_after_fresh_review"
            await service.db.commit()
            continue
        member.review_status = "exit_pending"
        await service.db.commit()
        error = await service._leave_group(
            member.account_id, service._discovered_group_from_model(group)
        )
        member.ad_status = "blocked"
        if error == "qualification_send_restriction_cleared":
            results.append({"membership_id": member.id, "status": member.review_status, "reason": error})
            continue
        if error and (str(error) == "account unavailable" or "account operation lease busy" in str(error).lower()):
            # The account was never acquired, so no leave RPC was attempted.
            # Keep the original membership and retry without inventing a failure.
            member.review_status = "exit_pending"
            member.review_next_at = member.leave_retry_at = datetime.utcnow() + timedelta(minutes=2)
            member.leave_error = "exit_account_temporarily_unavailable"
            results.append({"membership_id": member.id, "status": "exit_pending", "reason": member.leave_error})
            await service.db.commit()
            continue
        member.leave_attempts = (member.leave_attempts or 0) + 1
        if error is None:
            member.status, member.review_status = "left", "left"
            member.left_at = member.leave_confirmed_at = datetime.utcnow()
            await record_confirmed_exit(service.db, member, group)
            member.review_next_at = member.leave_retry_at = None
            member.leave_error = None
        else:
            member.review_status = "leave_failed"
            member.review_next_at = member.leave_retry_at = datetime.utcnow() + timedelta(hours=2)
            member.leave_error = str(error)[:500]
        results.append({"membership_id": member.id, "status": member.review_status, "error": error})
        await service.db.commit()
    await service.db.commit()
    return {"processed": len(results), "results": results}
