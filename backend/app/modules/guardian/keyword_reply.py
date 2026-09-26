"""Static, account-scoped Guardian replies for the explicitly configured PP-AI group."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.risk_guard import AccountRiskGuard
from app.core.account.telegram_execution import TelegramExecutionService
from app.core.settings_models import SystemSetting
from app.modules.owned_group.governance_worker import (
    owned_group_governance_gate_reason,
    resolve_governance_worker_target,
)


class SingleAttemptBotClient:
    """Translate the execution service call into an exactly-once transport attempt."""

    def __init__(self, client: Any):
        self.client = client

    async def send_message(self, chat_id: int, message: str, **kwargs: Any) -> Any:
        return await self.client.send_message_once(chat_id, message, **kwargs)


SETTING_KEY = "automation.pp_ai_guardian_keyword_reply"


def select_reply(config: dict[str, Any], text: str) -> str | None:
    """Exact commands/keywords only; normal discussions never trigger by substring."""
    candidate = str(text or "").strip().casefold()
    if candidate.startswith("/"):
        candidate = candidate[1:].split("@", 1)[0]
    if not candidate or len(candidate) > 80:
        return None
    replies = config.get("replies")
    if not isinstance(replies, dict):
        return None
    for keyword, response in list(replies.items())[:20]:
        if candidate == str(keyword).strip().casefold():
            if isinstance(response, str) and 0 < len(response.strip()) <= 4096:
                return response.strip()
    return None


async def handle_pp_ai_keyword_reply(
    db: AsyncSession,
    telegram_client: Any,
    *,
    bot_account_id: int,
    message: dict[str, Any],
    update_kind: str,
    verification_manager: Any,
    execution: TelegramExecutionService | None = None,
) -> dict[str, Any]:
    """Persist one send intent before RPC; replay never resends unknown outcomes."""
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    if (
        update_kind != "message"
        or chat.get("type") not in {"group", "supergroup"}
        or sender.get("is_bot")
        or message.get("sender_chat")
        or message.get("forward_origin")
        or message.get("forward_from")
        or message.get("forward_from_chat")
    ):
        return {"state": "ineligible"}
    chat_id, message_id, user_id = chat.get("id"), message.get("message_id"), sender.get("id")
    if (
        type(chat_id) is not int or chat_id >= 0
        or type(message_id) is not int or message_id <= 0
        or type(user_id) is not int or user_id <= 0
    ):
        return {"state": "ineligible"}
    setting = await db.get(SystemSetting, SETTING_KEY, populate_existing=True)
    try:
        config = json.loads(setting.value) if setting else {}
    except (TypeError, ValueError):
        return {"state": "disabled"}
    if not isinstance(config, dict) or config.get("enabled") is not True:
        return {"state": "disabled"}
    if config.get("bot_account_id") != bot_account_id or config.get("telegram_chat_id") != chat_id:
        return {"state": "target_mismatch"}
    reply = select_reply(config, message.get("text", ""))
    if reply is None:
        return {"state": "no_match"}
    target = await resolve_governance_worker_target(
        db,
        telegram_chat_id=chat_id,
        bot_account_id=bot_account_id,
        owned_gate_reason=await owned_group_governance_gate_reason(),
    )
    if (
        not target.allowed or not target.is_owned_group
        or target.owned_group_asset_id != config.get("owned_group_asset_id")
        or target.managed_binding_id != config.get("managed_binding_id")
    ):
        return {"state": "target_not_managed"}
    verification = await verification_manager.get_verification_config(target.core_group_id)
    if verification and verification.enable_verification:
        if not await verification_manager.is_user_verified(user_id, target.core_group_id):
            return {"state": "unverified"}

    key = f"guardian.pp_ai_reply.{bot_account_id}.{chat_id}.{message_id}"
    if await db.get(SystemSetting, key) is not None:
        return {"state": "duplicate"}
    payload = {
        "state": "sending",
        "bot_account_id": bot_account_id,
        "telegram_chat_id": chat_id,
        "source_message_id": message_id,
        "reply_sha256": hashlib.sha256(reply.encode()).hexdigest(),
        "attempted_at": datetime.utcnow().isoformat(),
    }
    intent = SystemSetting(key=key, value=json.dumps(payload))
    try:
        async with db.begin_nested():
            db.add(intent)
            await db.flush()
    except IntegrityError:
        return {"state": "duplicate"}
    # Persist the fence before contacting Telegram, including across worker restarts.
    await db.commit()
    sender_service = execution or TelegramExecutionService(AccountRiskGuard(db))
    try:
        sent_id = await sender_service.send_bot_message(
            SingleAttemptBotClient(telegram_client),
            chat_id,
            reply,
            parse_mode="",
            disable_web_page_preview=True,
            disable_notification=True,
            source="pp_ai_guardian_keyword_reply",
        )
        if type(sent_id) is int and sent_id > 0:
            payload.update(state="sent", message_id=sent_id)
        else:
            payload.update(state="unknown", reason="message_id_missing")
    except Exception as exc:
        # Do not expose Telegram response text or assume a timeout means no send.
        payload.update(state="unknown", reason=type(exc).__name__)
    intent = await db.get(SystemSetting, key, populate_existing=True)
    if intent is not None:
        intent.value = json.dumps(payload)
        await db.commit()
    return payload
