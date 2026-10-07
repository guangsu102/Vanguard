"""Bounded verification actions, separate from read-only group qualification."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from sqlalchemy import select, update

from app.core.account.models import TelegramAccount
from app.core.account.operation_lease import AccountOperationLeaseBusy
from app.core.account.telegram_execution import TelegramSendPreflightError
from app.core.group.identity import is_owned_group_target
from app.core.group.models import Group, GroupAccountMembership
from app.core.settings_models import SystemSetting
from app.modules.acquisition.group_qualification import POLICY_VERSION
from app.modules.acquisition.models import GroupQualificationAudit
from app.modules.acquisition.qualification_identity import (
    entity_identity,
    identity_relation,
    peer_identity,
)
from app.modules.acquisition.qualification_service import (
    _date,
    _payload,
    account_block_reason,
    latest_review,
    manually_protected,
    policy,
    verification_account_allowed,
)


def targeted_recent_prompt(message: Any, me: Any, joined_at: datetime, now: datetime) -> bool:
    """Only a current prompt explicitly addressed to this account may cause a write."""
    from app.modules.acquisition.automation import VERIFICATION_SIGNAL_RE

    created = _date(getattr(message, "date", None))
    if (
        created is None
        or created < max(joined_at - timedelta(seconds=30), now - timedelta(minutes=15))
        or created > now + timedelta(minutes=1)
        or getattr(message, "out", False)
        or getattr(message, "fwd_from", None)
    ):
        return False
    text = str(getattr(message, "message", None) or getattr(message, "text", "") or "")
    if not VERIFICATION_SIGNAL_RE.search(text):
        return False
    user_id = int(me.id)
    mentioned = any(
        getattr(entity, "user_id", None) == user_id
        for entity in (getattr(message, "entities", None) or [])
    )
    mentioned = mentioned or bool(re.search(rf"tg://user\?id={user_id}(?!\d)", text))
    username = str(getattr(me, "username", "") or "")
    if username:
        mentioned = mentioned or bool(
            re.search(rf"(?<!\w)@{re.escape(username)}(?!\w)", text, re.I)
        )
    return mentioned


def challenge_key(account_id: int, group_id: int, message: Any, decision: Any) -> str:
    value = [
        account_id,
        group_id,
        getattr(message, "id", None),
        decision.action,
        decision.challenge_type,
        decision.button_text,
        decision.answer,
        decision.bot_username,
        decision.bot_start_payload,
    ]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


async def _save_ledger(db: Any, membership: GroupAccountMembership, ledger: dict[str, Any]) -> None:
    key = f"qualification.verification.{membership.id}"
    record = await db.get(SystemSetting, key)
    value = json.dumps(ledger, ensure_ascii=False, default=str)
    if record is None:
        db.add(SystemSetting(key=key, value=value))
    else:
        record.value = value


async def _queue_recheck(
    db: Any, row: GroupQualificationAudit, membership: GroupAccountMembership
) -> None:
    current = await latest_review(db, membership.id, include_pending=True)
    await db.refresh(membership)
    if current is None or current.id != row.id or membership.joined_at != row.membership_joined_at:
        return
    row.state, row.next_retry_at = "queued", datetime.utcnow()
    membership.review_status, membership.ad_status = "initial_pending", "blocked"
    membership.review_next_at = None


def action_count(ledger: dict[str, Any]) -> int:
    """Only issued or uncertain writes consume the three-action limit."""
    return sum(
        1 for action in ledger.get("actions", [])
        if action.get("status") not in {"not_sent", "read_deferred"}
        and not (action.get("status") == "manual_required"
                 and not action.get("attempted_at")
                 and not action.get("telegram_message_id"))
    )


async def _claim(db: Any, membership_id: int) -> tuple[str, dict[str, Any]] | None:
    member = await db.scalar(
        select(GroupAccountMembership)
        .where(GroupAccountMembership.id == membership_id)
        .with_for_update(of=GroupAccountMembership, skip_locked=True)
        .execution_options(populate_existing=True)
    )
    if member is None:
        await db.rollback()
        return None
    key = f"qualification.verification.{member.id}"
    record = await db.get(SystemSetting, key, populate_existing=True)
    ledger = json.loads(record.value) if record else {}
    version = member.joined_at.isoformat() if member.joined_at else None
    if ledger.get("membership_version") != version:
        ledger = {"membership_version": version, "actions": [], "read_failures": 0}
    now = datetime.utcnow()
    if (
        action_count(ledger) >= 3
        or (_date(ledger.get("claim_until")) or datetime.min) > now
        or (_date(ledger.get("next_check_at")) or datetime.min) > now
    ):
        await db.commit()
        return None
    token = uuid4().hex
    ledger.update(claim_token=token, claim_until=(now + timedelta(minutes=5)).isoformat())
    await _save_ledger(db, member, ledger)
    await db.commit()
    return token, ledger


async def _session_lease_active(account_id: int) -> bool:
    """Return whether the listener currently owns the account session.

    Verification must not repeatedly contend with a connected listener.  This
    is only a Redis metadata check; it never opens Telegram or consumes read
    budget.  Redis failures fail open so the normal durable lease path remains
    the safety boundary.
    """
    try:
        from app.core.redis import get_redis

        redis = await get_redis()
        return bool(await redis.exists(f"vanguard:telegram:lease:{int(account_id)}"))
    except Exception:
        return False


async def _save_claim(
    db: Any, membership_id: int, token: str, ledger: dict, *, release: bool = False
) -> bool:
    key = f"qualification.verification.{membership_id}"
    record = await db.get(SystemSetting, key, populate_existing=True)
    if record is None:
        return False
    prior = json.loads(record.value)
    if prior.get("claim_token") != token:
        return False
    if (_date(prior.get("claim_until")) or datetime.min) <= datetime.utcnow():
        return False
    value = dict(ledger)
    if release:
        value.pop("claim_token", None)
        value.pop("claim_until", None)
    changed = await db.execute(
        update(SystemSetting)
        .where(
            SystemSetting.key == key,
            SystemSetting.value == record.value,
        )
        .values(value=json.dumps(value, ensure_ascii=False, default=str))
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return changed.rowcount == 1


async def _current_context(service: Any, audit_id: int, token: str):
    db = service.db
    config = await policy(db, fresh=True)
    if not config.get("enabled") or not config.get("execute_verification"):
        return None
    row = await db.get(GroupQualificationAudit, audit_id, populate_existing=True)
    if (
        row is None
        or row.state != "completed"
        or row.decision not in {"observe", "wait"}
        or row.policy_version != POLICY_VERSION
        or row.content_scope != "text_profile"
    ):
        return None
    membership = await db.get(GroupAccountMembership, row.membership_id, populate_existing=True)
    group = await db.get(Group, row.group_id, populate_existing=True)
    account = await db.get(TelegramAccount, row.account_id, populate_existing=True)
    newest = await latest_review(db, row.membership_id, include_pending=True)
    now = datetime.utcnow()
    if (
        membership is None
        or group is None
        or newest is None
        or newest.id != row.id
        or membership.status not in {"joined", "pending"}
        or membership.joined_at != row.membership_joined_at
        or membership.account_id != row.account_id
        or membership.group_id != row.group_id
        or not membership.joined_at
        or membership.joined_at < now - timedelta(hours=48)
        or account_block_reason(account, now)
        or manually_protected(config, group, membership)
        or not verification_account_allowed(config, row.account_id)
        or await is_owned_group_target(db, core_group_id=group.id, telegram_group_id=group.group_id)
    ):
        return None
    record = await db.get(
        SystemSetting, f"qualification.verification.{membership.id}", populate_existing=True
    )
    ledger = json.loads(record.value) if record else {}
    if (
        ledger.get("claim_token") != token
        or (_date(ledger.get("claim_until")) or datetime.min) <= now
        or ledger.get("membership_version") != membership.joined_at.isoformat()
    ):
        return None
    return row, membership, group, account, ledger


def _callback_data(message: Any, text: str) -> bytes | None:
    for row in getattr(message, "buttons", None) or []:
        for button in row:
            raw = getattr(button, "button", button)
            data = getattr(raw, "data", None)
            if (
                getattr(button, "text", getattr(raw, "text", None)) == text
                and isinstance(data, bytes)
                and not getattr(raw, "url", None)
            ):
                return data
    return None


def _second_hop_button_decision(message: Any, service: Any, settings: Any) -> Any | None:
    """Accept only an admin prompt's explicit Telegram verification-bot button."""
    if not settings.allow_second_hop_bots:
        return None
    from app.modules.acquisition.automation import JoinVerificationDecision

    for row in getattr(message, "buttons", None) or []:
        for button in row:
            raw = getattr(button, "button", button)
            label = str(getattr(button, "text", getattr(raw, "text", "")) or "")
            url = str(getattr(raw, "url", "") or "")
            if not service._is_safe_verification_button(label) or not url:
                continue
            parsed = urlsplit(url)
            if (
                parsed.scheme.lower() not in {"http", "https"}
                or (parsed.hostname or "").lower() not in {"t.me", "telegram.me"}
            ):
                continue
            username = parsed.path.strip("/")
            if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
                continue
            params = parse_qs(parsed.query, keep_blank_values=True)
            if set(params) - {"start"} or len(params.get("start", [""])) != 1:
                continue
            payload = params.get("start", [""])[0]
            if payload and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", payload):
                continue
            return JoinVerificationDecision(
                challenge_type="second_hop_bot", action="second_hop",
                confidence=0.9, bot_username=username,
                bot_start_payload=payload or None, target_message_id=message.id,
                reason="verified group prompt links to a Telegram verification bot",
            )
    return None


async def _run_second_hop(
    service: Any, wrapper: Any, client: Any, row: GroupQualificationAudit,
    membership: GroupAccountMembership, prompt: Any, decision: Any,
    key: str, token: str, ledger: dict[str, Any], settings: Any,
) -> dict[str, Any]:
    """One bounded bot hand-off; every write has a durable, unique attempt."""
    from app.modules.acquisition.automation import (
        APPROVAL_PENDING_RE,
        CAPTCHA_EXTERNAL_LINK_RE,
        SECOND_HOP_FAILURE_RE,
        SECOND_HOP_SUCCESS_RE,
    )

    answer = {"attempted": False, "status": "automatic_review", "reason": "second_hop_unresolved"}
    async def manual(reason: str) -> dict[str, Any]:
        answer["reason"] = reason
        ledger["actions"].append({
            "key": key, "message_id": prompt.id, "action": "second_hop",
            "status": "read_deferred", "reason": reason,
            "finished_at": datetime.utcnow().isoformat(),
        })
        await _save_claim(service.db, membership.id, token, ledger)
        return answer

    username = str(decision.bot_username or "").strip()
    payload = str(decision.bot_start_payload or "")
    if (not username or not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username)
            or (payload and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", payload))):
        return await manual("second_hop_invalid_bot_link")
    try:
        bot = await asyncio.wait_for(client.get_entity(username), timeout=10)
    except Exception as exc:
        return await manual("second_hop_bot_lookup_" + type(exc).__name__)
    bot_id = getattr(bot, "id", None)
    if getattr(bot, "bot", None) is not True or not isinstance(bot_id, int) or bot_id <= 0:
        return await manual("second_hop_target_not_bot")

    started = asyncio.get_running_loop().time()
    boundary = None

    async def write_step(step: str, source_message_id: int, text: str | None = None,
                         data: bytes | None = None) -> bool:
        nonlocal boundary
        if action_count(ledger) >= 3:
            answer["reason"] = "second_hop_action_limit"
            return False
        if await _current_context(service, row.id, token) is None:
            answer["reason"] = "second_hop_scope_changed"
            return False
        step_key = key + ":" + step + ":" + str(source_message_id)
        action = {
            "key": step_key, "message_id": prompt.id if step == "start" else source_message_id,
            "bot_id": bot_id, "action": "second_hop", "step": step,
            "status": "attempted", "attempted_at": datetime.utcnow().isoformat(),
        }
        ledger["actions"].append(action)
        if not await _save_claim(service.db, membership.id, token, ledger):
            answer["reason"] = "second_hop_claim_changed"
            return False
        answer["attempted"] = True
        try:
            remaining = max(1.0, min(30.0, 90.0 - (asyncio.get_running_loop().time() - started)))
            if text is not None:
                outcome = await asyncio.wait_for(
                    service.telegram_execution.send_verification_bot_text(
                        wrapper, bot, text, challenge_key=step_key,
                        group_id=membership.telegram_group_id,
                    ), timeout=remaining,
                )
                sent_id = getattr(outcome, "id", None)
                if not isinstance(sent_id, int) or sent_id <= 0:
                    raise RuntimeError("second_hop_message_id_missing")
                boundary = sent_id
                action["telegram_message_id"] = sent_id
            else:
                outcome = await asyncio.wait_for(
                    service.telegram_execution.click_verification_button(
                        wrapper, bot, source_message_id, data,
                        challenge_key=step_key, source="qualification_second_hop",
                        risk_target_type="verification_bot",
                    ), timeout=remaining,
                )
                if outcome is None:
                    raise RuntimeError("second_hop_callback_result_missing")
                boundary = source_message_id
            action["status"] = "succeeded"
        except TelegramSendPreflightError as exc:
            action["status"], action["reason"] = "not_sent", str(exc)[:160]
            answer["reason"] = "second_hop_preflight_blocked"
        except Exception as exc:
            # The original attempt and budget reservation are durable. Never retry it.
            action["status"], action["reason"] = "unknown", type(exc).__name__
            answer["reason"] = "second_hop_outcome_unknown"
        action["finished_at"] = datetime.utcnow().isoformat()
        await _save_claim(service.db, membership.id, token, ledger)
        return action["status"] == "succeeded"

    command = "/start" + (" " + payload if payload else "")
    if not await write_step("start", int(prompt.id), text=command):
        answer["status"] = "unknown" if answer["attempted"] else "manual_required"
        return answer
    for _ in range(3):
        remaining = 90.0 - (asyncio.get_running_loop().time() - started)
        if remaining <= 2:
            break
        await asyncio.sleep(min(4.0, max(1.0, remaining / 3)))
        try:
            messages = await asyncio.wait_for(
                client.get_messages(bot, limit=10), timeout=min(10.0, remaining)
            )
        except Exception as exc:
            answer["reason"] = "second_hop_reply_read_" + type(exc).__name__
            break
        fresh = [
            item for item in (messages or [])
            if getattr(item, "out", False) is not True
            and isinstance(getattr(item, "id", None), int)
            and item.id > int(boundary or 0)
        ]
        fresh.sort(key=lambda item: item.id)
        if not fresh:
            continue
        boundary = max(item.id for item in fresh)
        for item in fresh:
            text = str(getattr(item, "message", None) or getattr(item, "text", "") or "")
            if APPROVAL_PENDING_RE.search(text):
                answer.update(status="waiting", reason="second_hop_approval_pending")
                return answer
            if SECOND_HOP_SUCCESS_RE.search(text):
                answer.update(status="verified", reason="second_hop_bot_verified")
                return answer
            if SECOND_HOP_FAILURE_RE.search(text):
                answer.update(status="automatic_review", reason="second_hop_bot_rejected")
                return answer
            if CAPTCHA_EXTERNAL_LINK_RE.search(text):
                answer["reason"] = "second_hop_external_link"
                return answer
        if action_count(ledger) >= 3:
            answer["reason"] = "second_hop_action_limit"
            break
        for item in fresh:
            for buttons in getattr(item, "buttons", None) or []:
                for button in buttons:
                    label = str(getattr(button, "text", "") or "")
                    data = _callback_data(item, label)
                    if (settings.allow_button_clicks and data is not None
                            and service._is_safe_verification_button(label)):
                        if not await write_step("button", item.id, data=data):
                            answer["status"] = "unknown"
                            return answer
                        break
                else:
                    continue
                break
            else:
                continue
            break
        else:
            combined = "\n".join(
                str(getattr(item, "message", None) or getattr(item, "text", "") or "")
                for item in fresh
            )
            reply = service._solve_math_verification(combined)
            if reply is None and any(
                phrase in combined.lower()
                for phrase in ("加群目的", "入群目的", "你是做什么", "来自哪里", "why do you join")
            ):
                reply = service._default_join_verification_answer(settings.answer_profile)
            if (reply and settings.allow_text_answers
                    and service._is_safe_verification_answer(reply)):
                if not await write_step("answer", fresh[-1].id, text=reply):
                    answer["status"] = "unknown"
                    return answer
            else:
                answer["reason"] = "second_hop_unsupported_challenge"
                break
    return answer


async def run_verifications(service: Any, *, limit: int = 1, account_id: int | None = None) -> dict[str, Any]:
    """Claim durably before acquiring Telegram; re-read state before every action."""
    config = await policy(service.db, fresh=True)
    result: dict[str, Any] = {"checked": 0, "attempted": 0, "details": []}
    if not config.get("enabled") or not config.get("execute_verification") or limit <= 0:
        return result
    settings = await service._join_verification_settings()
    if not settings.enabled:
        return result
    settings = replace(
        settings, ai_enabled=False, unknown_challenge_action="manual"
    )
    now = datetime.utcnow()
    query = (
        select(GroupQualificationAudit)
        .join(
            GroupAccountMembership,
            GroupAccountMembership.id == GroupQualificationAudit.membership_id,
        )
        .where(
            GroupQualificationAudit.state == "completed",
            GroupQualificationAudit.policy_version == POLICY_VERSION,
            GroupQualificationAudit.content_scope == "text_profile",
            GroupQualificationAudit.decision.in_(["observe", "wait"]),
            GroupAccountMembership.status.in_(["joined", "pending"]),
            GroupAccountMembership.joined_at >= now - timedelta(hours=48),
        )
        .order_by(GroupQualificationAudit.id)
    )
    verification_ids = config.get("verification_account_ids", config.get("account_ids"))
    if account_id is not None:
        query = query.where(GroupQualificationAudit.account_id == account_id)
    if verification_ids is not None:
        if not isinstance(verification_ids, list) or any(
            type(value) is not int or value <= 0 for value in verification_ids
        ):
            return result
        query = query.where(GroupQualificationAudit.account_id.in_(verification_ids))
    rows = list((await service.db.scalars(query.limit(20))).all())
    for candidate in rows:
        if result["attempted"] >= min(1, limit):
            break
        account = await service.db.get(
            TelegramAccount, candidate.account_id, populate_existing=True
        )
        if account_block_reason(account, datetime.utcnow()):
            continue
        if await _session_lease_active(candidate.account_id):
            result["details"].append(
                {"audit_id": candidate.id, "status": "session_lease_busy"}
            )
            continue
        claimed = await _claim(service.db, candidate.membership_id)
        if claimed is None:
            continue
        token, ledger = claimed
        wrapper, attempted = None, False
        membership_id = candidate.membership_id
        try:
            context = await _current_context(service, candidate.id, token)
            if context is None:
                continue
            row, membership, group, account, ledger = context
            await service.account_pool.add_account_from_db(account)
            wrapper = await service.account_pool.acquire_by_id(
                account.id,
                purpose="qualification_verification",
                require_session=True,
                raise_on_lease_failure=True,
            )
            if wrapper is None or wrapper.client is None:
                raise RuntimeError("verification_account_unavailable")
            # Waiting for the account lease may outlive an approval, membership or switch.
            context = await _current_context(service, candidate.id, token)
            if context is None:
                continue
            row, membership, group, account, ledger = context
            client = wrapper.client
            entity = await client.get_entity(group.username or group.group_id)
            identity = peer_identity(group.group_id, _payload(row))
            if (
                identity_relation(entity_identity(entity), identity) != "same"
                or identity_relation(
                    peer_identity(membership.telegram_group_id, _payload(row)), identity
                )
                != "same"
            ):
                raise RuntimeError("verification_group_identity_mismatch")
            me = await client.get_me()
            messages, _, writable, permission_reason, _ = await service._read_join_audit_snapshot(
                client,
                entity,
                message_limit=50,
            )
            ledger["read_failures"] = 0
            result["checked"] += 1
            ledger["next_check_at"] = (datetime.utcnow() + timedelta(minutes=5)).isoformat()
            if writable is not False or permission_reason in {
                "account_banned",
                "group_membership_banned",
                "account_not_participant",
            }:
                continue
            # ``_read_join_audit_snapshot`` already returned the complete
            # recent-message batch.  Fetching each candidate again by ID was
            # the main source of duplicate Telegram reads in this lane.  Keep
            # one permission result per sender for this snapshot as well.
            sender_permissions: dict[int, Any] = {}
            for old_message in messages:
                if not targeted_recent_prompt(
                    old_message, me, membership.joined_at, datetime.utcnow()
                ):
                    continue
                # Use the object from the single bounded snapshot read.  A
                # durable event or later scheduled pass gets a new evidence
                # version and therefore a new snapshot; it never re-reads the
                # same message within this pass.
                message = old_message
                sender_id = getattr(message, "sender_id", None)
                if not isinstance(sender_id, int) or sender_id <= 0:
                    continue
                if sender_id not in sender_permissions:
                    sender_permissions[sender_id] = await client.get_permissions(entity, sender_id)
                sender = sender_permissions[sender_id]
                if not (getattr(sender, "is_admin", False) or getattr(sender, "is_creator", False)):
                    continue
                decision = service._local_join_verification_decision(
                    [message],
                    can_send_messages=writable,
                    permission_reason=permission_reason,
                    settings_config=settings,
                )
                if decision.action != "second_hop" and (
                    decision.action != "click_button"
                    or _callback_data(message, decision.button_text) is None
                ):
                    decision = _second_hop_button_decision(message, service, settings) or decision
                if (
                    decision.action not in {"click_button", "send_answer", "second_hop"}
                    or decision.challenge_type not in {
                        "rules_button", "math", "question", "second_hop_bot"
                    }
                    or decision.confidence < settings.confidence_threshold
                ):
                    continue
                key = challenge_key(account.id, group.group_id, message, decision)
                prior = next(
                    (
                        action
                        for action in ledger["actions"]
                        if action.get("key") == key or action.get("message_id") == message.id
                    ),
                    None,
                )
                if prior is not None and prior.get("status") not in {"not_sent", "read_deferred"}:
                    if decision.action != "second_hop":
                        await _queue_recheck(service.db, row, membership)
                        prior["reconciliation_queued"] = True
                    continue
                if decision.action == "second_hop":
                    if not settings.allow_second_hop_bots:
                        continue
                    if await _current_context(service, candidate.id, token) is None:
                        break
                    hop = await _run_second_hop(
                        service, wrapper, client, row, membership, message, decision,
                        key, token, ledger, settings,
                    )
                    attempted = bool(hop["attempted"])
                    result["attempted"] += int(attempted)
                    if attempted:
                        await _queue_recheck(service.db, row, membership)
                    result["details"].append({"audit_id": row.id, **hop})
                    break
                data = (
                    _callback_data(message, decision.button_text)
                    if decision.action == "click_button"
                    else None
                )
                if decision.action == "click_button" and data is None:
                    continue
                if decision.action == "send_answer" and not str(decision.answer or "").strip():
                    continue
                # Last check after Telegram reads and immediately before the durable write marker.
                if await _current_context(service, candidate.id, token) is None:
                    break
                action = {
                    "key": key,
                    "message_id": message.id,
                    "action": decision.action,
                    "challenge_type": decision.challenge_type,
                    "status": "attempted",
                    "attempted_at": datetime.utcnow().isoformat(),
                }
                if prior is not None:
                    ledger["actions"].remove(prior)
                ledger["actions"].append(action)
                if not await _save_claim(service.db, membership.id, token, ledger):
                    break
                attempted = True
                result["attempted"] += 1
                if decision.action == "send_answer":
                    outcome = await service.telegram_execution.send_verification_answer(
                        wrapper,
                        entity,
                        str(decision.answer),
                        reply_to=message.id,
                        challenge_key=key,
                        source="qualification_verification",
                    )
                    confirmed = bool(getattr(outcome, "id", None))
                else:
                    outcome = await service.telegram_execution.click_verification_button(
                        wrapper,
                        entity,
                        message.id,
                        data,
                        challenge_key=key,
                        source="qualification_verification",
                    )
                    confirmed = outcome is not None
                action["status"] = "succeeded" if confirmed else "unknown"
                action["finished_at"] = datetime.utcnow().isoformat()
                await _queue_recheck(service.db, row, membership)
                action["reconciliation_queued"] = True
                result["details"].append({"audit_id": row.id, **action})
                break
        except AccountOperationLeaseBusy:
            ledger["next_check_at"] = (datetime.utcnow() + timedelta(seconds=60)).isoformat()
            result["details"].append({"audit_id": candidate.id, "status": "account_busy"})
        except TelegramSendPreflightError as exc:
            ledger["actions"][-1].update(status="not_sent", reason=str(exc)[:160])
            ledger["next_check_at"] = (
                datetime.utcnow()
                + timedelta(seconds=max(60, int(getattr(exc, "retry_after_seconds", 0) or 0)))
            ).isoformat()
            result["details"].append(
                {"audit_id": candidate.id, "status": "not_sent", "reason": str(exc)[:160]}
            )
        except Exception as exc:
            seconds = max(300, int(getattr(exc, "seconds", 0) or 0))
            ledger["next_check_at"] = (datetime.utcnow() + timedelta(seconds=seconds)).isoformat()
            if attempted:
                from telethon.errors import RPCError

                ledger["actions"][-1].update(
                    status="rejected" if isinstance(exc, RPCError) else "unknown",
                    reason=type(exc).__name__,
                )
                if getattr(exc, "telegram_message_id", None):
                    ledger["actions"][-1]["telegram_message_id"] = exc.telegram_message_id
                await _queue_recheck(service.db, row, membership)
            else:
                ledger["read_failures"] = int(ledger.get("read_failures", 0)) + 1
                backoff = min(7200, 300 * 2 ** min(5, ledger["read_failures"] - 1))
                ledger["next_check_at"] = (
                    datetime.utcnow() + timedelta(seconds=max(seconds, backoff))
                ).isoformat()
            result["details"].append(
                {
                    "audit_id": candidate.id,
                    "status": "unknown" if attempted else "read_failed",
                    "reason": type(exc).__name__,
                }
            )
        finally:
            try:
                await _save_claim(service.db, membership_id, token, ledger, release=True)
            finally:
                if wrapper is not None:
                    await service.account_pool.release(wrapper)
    return result
