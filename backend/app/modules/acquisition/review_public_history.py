"""Share public candidate discovery with two current visibility certificates.

The finite process-local index contains references, never bodies or grants.
Every account still reads its own full info, membership, roles, selected roots
and warning threads. Shared pages never prove target-account history coverage.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
import time
from typing import Any

from telethon.tl import types

MAX_REFERENCES = 256
TTL_SECONDS = 300


@dataclass(frozen=True, slots=True)
class Reference:
    account_id: int
    group_id: int
    pts: int
    key: str
    identity: str
    observed_at: float
    cursor: dict


_references: OrderedDict[tuple[int, int, str], Reference] = OrderedDict()


def certificate(collector: Any, entity: Any, result: dict) -> dict | None:
    """Require server-current public history visibility in this exact task."""
    from app.modules.acquisition.review_read_flags import stage_enabled
    from app.modules.acquisition.review_history_progress import fingerprint, group_key

    account_id = collector.account_id
    if not stage_enabled(account_id, "public_share"):
        return None
    full, current = collector.full_chat, getattr(collector, "full_entity", None)
    permissions = result.get("permissions") or {}
    group_id = group_key(entity)
    if (type(account_id) is not int or account_id <= 0 or group_id is None
            or group_id >= -10**12 or not isinstance(full, types.ChannelFull)
            or not isinstance(current, types.Channel) or current.id != entity.id
            or full.id != entity.id or not current.username or not current.megagroup
            or current.forum or current.broadcast or current.min or current.left
            or current.restricted or current.admin_rights or current.creator
            or full.hidden_prehistory is not False or full.available_min_id not in (None, 0)
            or getattr(current.banned_rights, "view_messages", False)
            or permissions.get("member") is not True or permissions.get("is_admin") is not False
            or permissions.get("is_creator") is not False
            or getattr(collector, "self_history_visible", False) is not True
            or result.get("technical_errors") or type(full.pts) is not int or full.pts <= 0):
        return None
    try:
        box = collector.client._message_box
        if box.map[entity.id].pts != full.pts:
            return None
        identity = fingerprint(collector.client, account_id, group_id, collector.history_scope)
        if identity is None:
            return None
        return {"account_id": account_id, "group_id": group_id, "pts": full.pts,
                "identity": identity}
    except (KeyError, TypeError, AttributeError):
        return None


def publish(cert: dict | None, *, cursor: dict, observed_at: float) -> None:
    if cert is None or cursor.get("complete") is not True:
        return
    key = cursor.get("page_key")
    if not isinstance(key, str) or len(key) != 32 or not 0 <= time.time()-observed_at < TTL_SECONDS:
        return
    # Only numeric/scalar references from our existing fixed-page writer.
    saved = {name: cursor.get(name) for name in ("window_end", "offset_id", "complete", "page_key")}
    ref = Reference(cert["account_id"], cert["group_id"], cert["pts"], key,
                    cert["identity"], observed_at, saved)
    for index, old in list(_references.items()):
        if not 0 <= time.time()-old.observed_at < TTL_SECONDS:
            del _references[index]
    index = ref.group_id, ref.account_id, ref.key
    _references[index] = ref
    _references.move_to_end(index)
    while len(_references) > MAX_REFERENCES:
        _references.popitem(last=False)


async def load(store: Any, *, collector: Any, entity: Any, result: dict,
               window_end: datetime) -> tuple[dict, dict] | None:
    from app.modules.acquisition.review_history_progress import _date, _message, fingerprint, VERSION

    before = certificate(collector, entity, result)
    if before is None:
        return None
    candidates = [ref for ref in reversed(_references.values())
                  if ref.account_id != before["account_id"] and ref.group_id == before["group_id"]
                  and ref.pts == before["pts"] and 0 <= time.time()-ref.observed_at < TTL_SECONDS]
    for ref in candidates:
        anchor = _date(ref.cursor.get("window_end"))
        if anchor is None or not 0 <= (window_end-anchor).total_seconds() < 86400:
            continue
        try:
            response = await store.load_history_page(ref.account_id, ref.group_id, key=ref.key)
            if not response.ok or certificate(collector, entity, result) != before:
                continue
            data, meta = response.data, response.data["metadata"]
            observed = float(meta["observed_at"])
            if (meta.get("version") != VERSION or meta.get("identity") != ref.identity
                    or observed != ref.observed_at or not 0 <= time.time()-observed < TTL_SECONDS
                    or any(meta.get(name) != ref.cursor.get(name)
                           for name in ("window_end", "offset_id", "complete"))):
                continue
            start, end = _date(meta["window_start"]), _date(meta["window_end"])
            if start is None or end is None or (end-start).total_seconds() != 86400:
                continue
            messages = [_message(record) for record in data["records"]]
            if (len({message.id for message in messages}) != len(messages)
                    or any(type(message.id) is not int or message.id <= 0 or message.date is None
                           or not start <= message.date <= end for message in messages)):
                continue
            if fingerprint(collector.client, before["account_id"], before["group_id"],
                           collector.history_scope) != before["identity"]:
                return None
            return dict(ref.cursor), {"messages": messages, "observed_at": observed,
                "sample_count": int(meta["sample_count"]), "calls": int(meta["calls"]),
                "window_start": start, "window_end": end}
        except (KeyError, TypeError, ValueError, AttributeError, OSError):
            continue
    return None


async def candidate_ids(store: Any, *, collector: Any, entity: Any, result: dict,
                        now: datetime) -> list[int]:
    """Yield hints only. The existing target-account live reader proves facts."""
    from datetime import timedelta
    from app.modules.acquisition.group_qualification import looks_like_ad, text_of

    if store is None or certificate(collector, entity, result) is None:
        return []
    identifiers = []
    for day in range(3):
        found = await load(store, collector=collector, entity=entity, result=result,
                           window_end=now-timedelta(days=day))
        if found is None:
            continue
        for message in found[1]["messages"]:
            if (not message.action and not getattr(message.reply_to, "forum_topic", False)
                    and now-timedelta(hours=72) <= message.date <= now
                    and looks_like_ad(text_of(message)) and message.id not in identifiers):
                identifiers.append(message.id)
                if len(identifiers) == 6:
                    return identifiers
    return identifiers
