"""PP-AI scoped image CAPTCHA lifecycle with durable send and answer fences."""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.risk_guard import AccountRiskAction, AccountRiskGuard
from app.core.account.telegram_execution import TelegramExecutionService
from app.core.settings_models import SystemSetting
from app.modules.guardian.keyword_reply import SingleAttemptBotClient
from app.modules.guardian.models import VerificationState, VerificationType
from app.modules.guardian.verification.captcha_gen import CaptchaGenerator
from app.modules.guardian.verification.verification_mgr import VerificationManager
from app.modules.owned_group.governance_worker import (
    owned_group_governance_gate_reason,
    resolve_governance_worker_target,
)

SETTING_KEY = "automation.pp_ai_guardian_verification"
PREFIX = "guardian.pp_ai_verify."
PERMISSIONS = (
    "can_send_messages", "can_send_audios", "can_send_documents", "can_send_photos",
    "can_send_videos", "can_send_video_notes", "can_send_voice_notes", "can_send_polls",
    "can_send_other_messages", "can_add_web_page_previews", "can_change_info",
    "can_invite_users", "can_pin_messages", "can_manage_topics",
)


async def _target(db: AsyncSession, bot_id: int, chat_id: int | None = None):
    row = await db.get(SystemSetting, SETTING_KEY, populate_existing=True)
    try:
        config = json.loads(row.value) if row else {}
    except (TypeError, ValueError):
        return None
    if not isinstance(config, dict) or config.get("enabled") is not True:
        return None
    if config.get("bot_account_id") != bot_id:
        return None
    configured_chat = config.get("telegram_chat_id")
    if type(configured_chat) is not int or configured_chat >= 0:
        return None
    if chat_id is not None and configured_chat != chat_id:
        return None
    target = await resolve_governance_worker_target(
        db, telegram_chat_id=configured_chat, bot_account_id=bot_id,
        owned_gate_reason=await owned_group_governance_gate_reason(),
    )
    if (
        not target.allowed or not target.is_owned_group
        or target.owned_group_asset_id != config.get("owned_group_asset_id")
        or target.managed_binding_id != config.get("managed_binding_id")
    ):
        return None
    return target


async def _save(db: AsyncSession, key: str, payload: dict[str, Any]) -> None:
    row = await db.get(SystemSetting, key)
    if row is None:
        row = SystemSetting(key=key)
        db.add(row)
    row.value = json.dumps(payload)
    await db.commit()


async def _claim(db: AsyncSession, key: str) -> bool:
    if await db.get(SystemSetting, key) is not None:
        return False
    try:
        async with db.begin_nested():
            db.add(SystemSetting(key=key, value='{"state":"attempted"}'))
            await db.flush()
    except IntegrityError:
        return False
    await db.commit()
    return True


async def _message_once(db, client, key, chat_id, text):
    if not await _claim(db, key):
        return
    try:
        message_id = await TelegramExecutionService(AccountRiskGuard(db)).send_bot_message(
            SingleAttemptBotClient(client), chat_id, text, parse_mode="",
            disable_web_page_preview=True, source="pp_ai_verification",
        )
        await _save(db, key, {"state": "sent" if message_id else "unknown", "message_id": message_id})
    except Exception as exc:
        await _save(db, key, {"state": "unknown", "reason": type(exc).__name__})


async def _restrict(client, chat_id, user_id, *, allow_text=False, permissions=None):
    rights = dict.fromkeys(PERMISSIONS, False)
    if permissions is not None:
        rights.update({key: bool(value) for key, value in permissions.items() if key in rights})
    elif allow_text:
        rights["can_send_messages"] = True
    async with client._risk_operation(
        AccountRiskAction.MODERATION, target_type="user", target_id=user_id,
        details={"source": "pp_ai_verification", "chat_id": chat_id},
    ):
        if not await client.restrict_chat_member(chat_id, user_id, rights):
            raise RuntimeError("member permission update not confirmed")


async def handle_join(
    db: AsyncSession, client: Any, *, bot_account_id: int, chat_id: int,
    member: dict[str, Any], source_message_id: int,
) -> bool:
    target = await _target(db, bot_account_id, chat_id)
    if target is None:
        return False
    user_id = member.get("id")
    if type(user_id) is not int or member.get("is_bot"):
        return True
    manager = VerificationManager(db)
    config = await manager.get_verification_config(target.core_group_id)
    if not config or not config.enable_verification or config.verification_type != VerificationType.CAPTCHA:
        return False
    remote = await client.get_chat_member(chat_id, user_id)
    if remote.get("status") in {"administrator", "creator"}:
        return True
    should_verify, _ = await manager.should_verify(user_id, target.core_group_id)
    if not should_verify:
        return False
    key = f"{PREFIX}{bot_account_id}.{chat_id}.{user_id}"
    existing = await db.get(SystemSetting, key)
    previous = json.loads(existing.value) if existing else {}
    if previous.get("source_message_id") == source_message_id:
        return True
    # Preserve pre-existing individual restrictions and avoid granting privileges.
    defaults = await client.get_chat_permissions(chat_id)
    if not defaults:
        raise RuntimeError("group default permissions unavailable")
    baseline = {name: bool(defaults.get(name, False)) for name in PERMISSIONS}
    if remote.get("status") == "restricted":
        baseline = {name: baseline[name] and bool(remote.get(name, False)) for name in PERMISSIONS}
    payload = {
        "state": "initializing", "source_message_id": source_message_id,
        "user_id": user_id, "chat_id": chat_id, "core_group_id": target.core_group_id,
        "baseline_permissions": baseline, "joined_at": datetime.utcnow().isoformat(),
        "username": str(member.get("first_name") or "新成员")[:100],
    }
    await _save(db, key, payload)
    # Text remains available only for the /verify command; every unverified
    # incoming message is deleted before answer parsing or normal moderation.
    await _restrict(client, chat_id, user_id, allow_text=True)
    session = await manager._create_verification_session(
        user_id, target.core_group_id, VerificationType.CAPTCHA, config
    )
    captcha = CaptchaGenerator().generate_image_captcha()
    session.captcha_code = captcha.code
    await db.commit()
    payload.update(state="waiting", session_id=session.session_id, challenge_state="sending")
    await _save(db, key, payload)
    caption = (
        f"{payload['username']}，请识别图片验证码，并在群内发送 /verify 验证码。"
        f"\n请在 {config.timeout_minutes} 分钟内完成，最多 {config.max_attempts} 次。"
        "\n验证前其他消息会被删除，请勿发送个人敏感信息。"
    )
    try:
        png = base64.b64decode(captcha.image_data.split(",", 1)[1], validate=True)
        sent = await client.send_verification_photo(chat_id, png, caption)
        sent_id = getattr(sent, "message_id", None)
        payload.update(challenge_state="sent" if sent_id else "unknown", challenge_message_id=sent_id)
    except Exception as exc:
        payload.update(challenge_state="unknown", challenge_error=type(exc).__name__)
    await _save(db, key, payload)
    return True


async def _restore(db, client, key, payload, config):
    defaults = await client.get_chat_permissions(payload["chat_id"])
    if not defaults:
        raise RuntimeError("current group defaults unavailable")
    baseline = payload["baseline_permissions"]
    # Do not overrule a group's newer restrictions or prior individual restrictions.
    permissions = {name: bool(defaults.get(name, False)) and bool(baseline.get(name, False)) for name in PERMISSIONS}
    await _restrict(client, payload["chat_id"], payload["user_id"], permissions=permissions)
    payload["state"] = "passed"
    await _save(db, key, payload)
    text = (config.welcome_message or "欢迎 {username} 加入群聊！").replace(
        "{username}", payload.get("username", "新成员")
    )
    await _message_once(db, client, key + ".welcome." + str(payload["source_message_id"]), payload["chat_id"], text)


async def handle_answer(
    db: AsyncSession, client: Any, *, bot_account_id: int, message: dict[str, Any],
    update_kind: str,
) -> bool:
    chat_id = (message.get("chat") or {}).get("id")
    user_id = (message.get("from") or {}).get("id")
    if type(chat_id) is not int or type(user_id) is not int:
        return False
    target = await _target(db, bot_account_id, chat_id)
    if target is None or (message.get("from") or {}).get("is_bot"):
        return False
    key = f"{PREFIX}{bot_account_id}.{chat_id}.{user_id}"
    row = await db.get(SystemSetting, key, populate_existing=True)
    if row is None:
        return False
    payload = json.loads(row.value)
    if payload.get("state") in {"passed", "kicked"}:
        return False
    message_id = message.get("message_id")
    if type(message_id) is not int or message_id <= 0:
        return True
    # Delete even edited/media/non-answer content while pending. Do not count
    # arbitrary chatter as an answer, so it cannot exhaust the challenge for others.
    async with client._risk_operation(
        AccountRiskAction.MODERATION, target_type="message", target_id=message_id,
        details={"source": "pp_ai_verification", "chat_id": chat_id},
    ):
        await client.delete_message(chat_id, message_id)
    if update_kind != "message":
        return True
    text = str(message.get("text") or "").strip()
    parts = text.split(maxsplit=1)
    if not parts or parts[0].split("@", 1)[0].lower() != "/verify" or len(parts) != 2:
        return True
    if message.get("sender_chat") or message.get("forward_origin") or message.get("forward_from"):
        return True
    session_id = payload.get("session_id")
    if not session_id:
        return True
    manager = VerificationManager(db)
    session = await manager.get_session(session_id)
    if session is None or session.user_id != user_id or session.chat_id != target.core_group_id:
        return True
    answer_key = f"guardian.pp_ai_answer.{bot_account_id}.{chat_id}.{message_id}"
    if not await _claim(db, answer_key):
        return True
    result = await manager.verify_answer(session_id, parts[1])
    config = await manager.get_verification_config(target.core_group_id)
    if result.success:
        payload["state"] = "restore_pending"
        await _save(db, key, payload)
        await _restore(db, client, key, payload, config)
    else:
        if result.remaining_attempts <= 0:
            payload["state"] = "failed"
            await _save(db, key, payload)
            await _restrict(client, chat_id, user_id)
        await _message_once(db, client, answer_key + ".result", chat_id, result.message)
    return True


async def maintain(db: AsyncSession, client: Any, *, bot_account_id: int) -> int:
    """Runs on every bot polling cycle, including cycles without new messages."""
    target = await _target(db, bot_account_id)
    if target is None:
        return 0
    manager = VerificationManager(db)
    config = await manager.get_verification_config(target.core_group_id)
    if not config or not config.enable_verification:
        return 0
    rows = (await db.scalars(select(SystemSetting).where(
        SystemSetting.key.like(f"{PREFIX}{bot_account_id}.{target.telegram_chat_id}.%"),
        or_(*[
            SystemSetting.value.like('%"state": "' + state + '"%')
            for state in ("initializing", "waiting", "restore_pending", "failed", "expired", "kick_pending")
        ]),
    ).order_by(SystemSetting.updated_at).limit(500))).all()
    processed = 0
    for row in rows:
        payload = json.loads(row.value)
        if payload.get("state") in {"sent", "unknown", "attempted", "passed", "kicked"}:
            continue
        if not payload.get("session_id") or not payload.get("joined_at"):
            continue
        session = await manager.get_session(payload["session_id"])
        if session is None or session.chat_id != target.core_group_id or session.user_id != payload.get("user_id"):
            continue
        # Real administrators are protected even if promoted after joining.
        member = await client.get_chat_member(payload["chat_id"], payload["user_id"])
        if member.get("status") in {"administrator", "creator", "left"}:
            continue
        if session.state == VerificationState.PASSED:
            await _restore(db, client, row.key, payload, config)
            processed += 1
            continue
        if payload.get("challenge_state") != "sent":
            # Unknown challenge delivery must not become a confirmed user failure.
            continue
        now = datetime.utcnow()
        if session.expires_at <= now and session.state == VerificationState.PENDING:
            session.state = VerificationState.EXPIRED
            await db.commit()
        if session.state in {VerificationState.FAILED, VerificationState.EXPIRED}:
            if payload.get("state") not in {"failed", "expired", "kick_pending"}:
                await _restrict(client, payload["chat_id"], payload["user_id"])
                payload["state"] = "expired"
                await _save(db, row.key, payload)
            kick_at = datetime.fromisoformat(payload["joined_at"]) + timedelta(minutes=config.kick_after_minutes)
            if config.auto_kick_unverified and now >= kick_at:
                payload["state"] = "kick_pending"
                await _save(db, row.key, payload)
                # Reconcile a prior timeout before repeating the membership write.
                async with client._risk_operation(
                    AccountRiskAction.MODERATION, target_type="user", target_id=payload["user_id"],
                    details={"source": "pp_ai_verification_kick", "chat_id": payload["chat_id"]},
                ):
                    if member.get("status") != "kicked":
                        if not await client.ban_chat_member(payload["chat_id"], payload["user_id"]):
                            continue
                    if not await client.unban_chat_member(payload["chat_id"], payload["user_id"], only_if_banned=True):
                        continue
                payload["state"] = "kicked"
                await _save(db, row.key, payload)
            processed += 1
    return processed
