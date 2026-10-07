"""Skip ordinary group chatter only when no configured consumer needs it."""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import select

from app.core.automation_settings import get_group_ai_interaction_settings
from app.core.group.models import Group
from app.modules.owned_group.models import OwnedGroupAsset


async def policy_snapshot(db: Any, *, keywords: int, triggers: int) -> dict:
    settings = await get_group_ai_interaction_settings(db)
    rows = (
        await db.execute(
            select(OwnedGroupAsset.telegram_chat_id, Group.group_id)
            .outerjoin(Group, OwnedGroupAsset.core_group_id == Group.id)
            .where(OwnedGroupAsset.archived_at.is_(None))
        )
    ).all()
    from app.core.account.listener_interest import load_interests
    return {
        "loaded_at": time.monotonic(),
        "ordinary_required": bool(keywords or triggers or settings.get("enabled")),
        "owned_chats": {int(value) for row in rows for value in row if value is not None},
        "selective": True,
        "interests": await load_interests(db),
    }


def keep_message(event: Any, policy: dict | None) -> bool:
    # Unknown/stale policy never drops events during startup or a config outage.
    if not policy or time.monotonic() - policy["loaded_at"] > 90:
        return True
    if policy["ordinary_required"] or getattr(event, "is_private", False):
        return True
    if getattr(event, "chat_id", None) in policy["owned_chats"]:
        return True
    if policy.get('selective'):
        # Verification prompts become a coalesced review cue at raw ingress;
        # they never enter keyword/AI interaction or a persistent chat archive.
        return False
    message = getattr(event, "message", None)
    sender = getattr(event, "sender", None) or getattr(message, "sender", None)
    text = getattr(event, "raw_text", None) or getattr(event, "text", None) or ""
    # Keep commands, bot/verification prompts and direct interactions. Only
    # unsolicited ordinary group chat has no consumer under this policy.
    return bool(
        not getattr(event, "chat_id", None)
        or text.lstrip().startswith("/")
        or getattr(sender, "bot", False)
        or getattr(message, "action", None)
        or getattr(message, "mentioned", False)
        or getattr(message, "reply_to_msg_id", None)
    )
