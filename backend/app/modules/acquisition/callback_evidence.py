"""Bounded callback message IDs for targeted review; never authorization."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from app.core.settings_models import SystemSetting
from app.modules.acquisition.evidence_progress import date


def key(member: Any) -> str:
    return f"qualification.callback_hints.{member.id}"


def decode(value: str | None) -> dict:
    try:
        payload = json.loads(value or "{}")
        return payload if isinstance(payload, dict) else {}
    except (ValueError, TypeError):
        return {}


def scoped(payload: dict, member: Any) -> bool:
    return payload.get("account_id") == member.account_id and payload.get("peer") == member.telegram_group_id and payload.get("joined_at") == member.joined_at.isoformat()


def trim(items: list[dict], now: datetime) -> list[dict]:
    items = items if isinstance(items, list) else []
    valid = {item["id"]: item for item in items if isinstance(item, dict) and type(item.get("id")) is int and item["id"] > 0
             and (stamp := date(item.get("date"))) is not None and now - timedelta(hours=72) <= stamp <= now}
    ordered = sorted(valid.values(), key=lambda item: item["date"])
    mature = [item for item in ordered if date(item["date"]) <= now - timedelta(hours=24)]
    recent = [item for item in ordered if date(item["date"]) > now - timedelta(hours=24)]
    return [*mature[-40:], *recent[-40:]]


async def save_hint(db: Any, member: Any, fact: dict, now: datetime) -> bool:
    if member.joined_at is None or fact.get("joined_at") != member.joined_at.isoformat():
        return False
    row = await db.get(SystemSetting, key(member))
    previous = decode(row.value) if row else {}
    items = previous.get("messages", []) if scoped(previous, member) else []
    items = items if isinstance(items, list) else []
    old_items = trim(items, now)
    items = trim([*items, {"id": fact.get("message_id"), "date": fact.get("message_date")}], now)
    if not items or items == old_items:
        return False
    value = json.dumps({"account_id": member.account_id, "peer": member.telegram_group_id,
                        "joined_at": member.joined_at.isoformat(), "messages": items})
    if row is None:
        db.add(SystemSetting(key=key(member), value=value))
    else:
        row.value = value
    return True


async def hint_ids(db: Any, member: Any, now: datetime) -> list[int]:
    if member is None or member.joined_at is None:
        return []
    row = await db.get(SystemSetting, key(member))
    if row is None:
        return []
    payload = decode(row.value)
    if not scoped(payload, member):
        return []
    items = trim(payload.get("messages", []), now)
    # Mature observations first. Actual visibility, sender identity, rules and
    # reply warnings are still checked by EvidenceCollector against Telegram.
    return [item["id"] for item in items[:6]]
