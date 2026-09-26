"""Final read checks and separately authorized exit actions."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any
from weakref import WeakKeyDictionary

from sqlalchemy import select, text

from app.core.account.models import TelegramAccount
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
    """A cached review never substitutes for present rights, rules or slow mode."""
    from telethon.tl.functions.channels import GetFullChannelRequest
    from telethon.tl.functions.messages import GetFullChatRequest, GetOnlinesRequest
    from telethon.tl.types import InputMessagesFilterPinned

    from app.modules.acquisition.group_qualification import (
        RULE,
        WARNING,
        EvidenceCollector,
        evidence_age,
        naive,
        text_of,
        trial_proof,
    )
    from app.modules.acquisition.qualification_identity import (
        entity_identity,
        identity_relation,
        peer_identity,
    )

    row, group, member = await current_authorization(db, account_id, target)
    if row is None or group is None or member is None:
        raise TelegramExecutionError("qualification_review_required")
    snapshot = json.loads(row.evidence_json or "{}")
    entity = await client.get_entity(target)
    if (
        identity_relation(entity_identity(entity), peer_identity(group.group_id, snapshot))
        != "same"
    ):
        raise TelegramExecutionError("qualification_identity_changed")
    current = await client.get_permissions(entity, "me")
    if current is None or not hasattr(current, "is_admin"):
        raise TelegramExecutionError("qualification_current_permission_unknown")
    if getattr(current, "is_admin", False) or getattr(current, "is_creator", False):
        raise TelegramExecutionError("qualification_protected_membership")
    own = getattr(getattr(current, "participant", None), "banned_rights", None)
    defaults = getattr(entity, "default_banned_rights", None)
    if (not getattr(current, "has_left", False)
            and any(getattr(own, field, False) for field in ("view_messages", "send_messages", "send_plain"))
            and not getattr(current, "is_admin", False) and not getattr(current, "is_creator", False)):
        from app.modules.acquisition.adaptive_frequency import record_live_mute
        from app.modules.acquisition.send_restriction import restriction_facts, track_restriction
        from app.modules.acquisition.qualification_service import annotate_newcomer_restriction
        observed_at = datetime.utcnow()
        live = {**snapshot, "technical_errors": [], "collected_at": observed_at.isoformat(),
                "permissions": {**restriction_facts([own], observed_at),
                    "member": not bool(getattr(current, "is_banned", False)
                                       or getattr(entity, "left", False)
                                       or getattr(own, "view_messages", False)),
                    "can_send_text": False}}
        annotate_newcomer_restriction(live, member.joined_at, observed_at)
        track_restriction(live, snapshot, observed_at)
        await record_live_mute(db, account_id, group.group_id, live)
    if (
        getattr(current, "has_left", False)
        or getattr(current, "is_banned", False)
        or getattr(entity, "left", False)
        or any(
            getattr(rights, field, False)
            for rights in (own, defaults)
            for field in ("view_messages", "send_messages", "send_plain")
        )
    ):
        raise TelegramExecutionError("qualification_current_permission_changed")
    response = await client(
        GetFullChannelRequest(entity)
        if hasattr(entity, "megagroup")
        else GetFullChatRequest(entity.id)
    )
    full = response.full_chat
    now = datetime.utcnow()
    slow = getattr(full, "slowmode_next_send_date", None)
    if isinstance(slow, int):
        slow = datetime.utcfromtimestamp(slow)
    if isinstance(slow, datetime) and naive(slow) > now:
        raise TelegramExecutionError("qualification_slowmode_wait")
    if getattr(full, "send_paid_messages_stars", 0):
        raise TelegramExecutionError("qualification_paid_messages_not_authorized")
    async def invalidate(reason: str, *, unknown: bool = False, retry_after: int = 0) -> None:
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
        await db.commit()
        raise TelegramExecutionError(
            "qualification_rules_unknown" if unknown else "qualification_rules_changed",
            retry_after_seconds=retry_after or None,
        )

    # Online count is an exit fact only; it is not an additional ad permission gate.
    online_count = getattr(full, "online_count", None)
    if snapshot.get("exit_group_rule_ban") is True:
        if not isinstance(online_count, int) or online_count < 0:
            try:
                online_count = getattr(await client(GetOnlinesRequest(entity)), "onlines", None)
            except Exception as exc:
                if "Flood" in type(exc).__name__:
                    raise
                online_count = None
        if type(online_count) is int and 0 <= online_count < 2:
            await invalidate("exit_group_ad_ban_and_online_below_2_before_send")
    members = getattr(full, "participants_count", None)
    if type(members) is int and 0 <= members < 50:
        await invalidate("exit_members_below_50_before_send")

    own_ids = snapshot.get("system_user_ids")
    if (
        snapshot.get("system_identity_coverage") is not True
        or not isinstance(own_ids, list)
        or any(type(value) is not int or value <= 0 for value in own_ids)
    ):
        await invalidate("system_account_identity_unconfirmed_before_send", unknown=True)
    # Trial authorization is based on an observed ordinary-member precedent.
    # Explicit group-rule permission is a separate route and needs no such post.
    if getattr(row, "decision", None) != "allowed":
        proof = trial_proof(
            [item for item in snapshot.get("evidence", []) if item.get("topic_id") is None]
        )
        if len(proof) != 1:
            await invalidate("advertising_precedent_unconfirmed_before_send", unknown=True)
        try:
            present = await client.get_messages(entity, ids=[item["message_id"] for item in proof])
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
        proof_collector = EvidenceCollector(client, own_user_ids=set(own_ids))
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
            except Exception as exc:
                await invalidate(
                    "advertising_precedent_identity_failed_before_send:" + type(exc).__name__,
                    unknown=True,
                    retry_after=max(120, int(getattr(exc, "seconds", 0) or 0)),
                )
                return
            if role != "ordinary":
                await invalidate("advertising_precedent_identity_changed_before_send", unknown=True)

    if getattr(row, "decision", None) == "trial":
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
    old_admin = {
        item.get("message_id"): item
        for item in snapshot.get("evidence", [])
        if item.get("source") == "admin_rule"
    }
    try:
        pins = [
            message
            async for message in client.iter_messages(
                entity, filter=InputMessagesFilterPinned(), limit=201
            )
        ]
        latest = [message async for message in client.iter_messages(entity, limit=100)]
        current_admin = await client.get_messages(entity, ids=list(old_admin)) if old_admin else []
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
    if any(date is None for date in dates) or (
        len(latest) >= 100 and all(date > since for date in dates)
    ):
        await invalidate("recent_rules_coverage_unknown_before_send", unknown=True)
    collector = EvidenceCollector(client, own_user_ids=set(own_ids))
    for message, date in zip(latest, dates, strict=True):
        edited = naive(getattr(message, "edit_date", None))
        if max(date, edited or date) <= since:
            continue
        text = text_of(message)
        if not (RULE.search(text) or WARNING.search(text)):
            continue
        try:
            role = await collector.role(entity, message)
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


async def reconcile_stale_exit_memberships(service: Any, config: dict, *, limit: int = 1) -> dict[str, int]:
    """Read old non-joined relations in a separate lane; never consume a leave slot."""
    from app.core.account.models import TelegramAccount
    from app.modules.acquisition.qualification_service import enqueue_membership_review, manually_protected
    from app.core.group.identity import is_owned_group_target
    query = select(GroupAccountMembership).where(
        GroupAccountMembership.status.not_in(["joined", "leave_failed"]),
        GroupAccountMembership.review_status.in_(["exit_pending", "leave_failed", "membership_reconciliation"]),
        GroupAccountMembership.review_next_at <= datetime.utcnow(),
    )
    if not config.get("exit_all_accounts"):
        query = query.where(GroupAccountMembership.account_id.in_(config.get("account_ids") or []))
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


async def run_exits(service: Any, *, limit: int = 1) -> dict[str, Any]:
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
        result = await run_frequency_exits(service, limit=limit)
        if not result["processed"]:
            result = await _run_exits_serialized(service, limit=limit)
        result["membership_reconciliation"] = await reconcile_stale_exit_memberships(service, config)
        return result


async def _run_exits_serialized(service: Any, *, limit: int = 1) -> dict[str, Any]:
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
