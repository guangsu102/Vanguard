"""Account-scoped V5 evidence integration; every miss uses the original reader.

Ingress capture never advances SDK cursors or confers authorization. Stored
messages may describe an exact recent event; mature ads and known rule anchors
still require live reads. All optional payloads live in the bounded store.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from telethon import utils
from telethon.tl import types

from app.core.config import settings

_store: Any = None
_store_path: str | None = None
_budget_cache: tuple[tuple[str, ...], float, dict] | None = None


def component_budget_status() -> dict:
    """Bounded periodic read-only check; a failure disables only new reuse."""
    global _budget_cache
    from app.modules.acquisition.review_storage_budget import verify

    paths = (settings.REVIEW_EVIDENCE_STORE_PATH, settings.REVIEW_READ_METRICS_PATH,
             settings.REVIEW_DIAGNOSTICS_PATH)
    instant = time.monotonic()
    if _budget_cache and _budget_cache[0] == paths and instant < _budget_cache[1]:
        return _budget_cache[2]
    result = verify(dict(zip(("evidence", "metrics", "diagnostics"), paths)))
    _budget_cache = (paths, instant + (60 if result["ok"] else 5), result)
    return result


def enabled_for(account_id: int) -> bool:
    from app.modules.acquisition.review_read_flags import enabled_for as rollout_enabled
    from app.modules.acquisition.qualification_engine import enabled_for as efficiency_enabled

    return efficiency_enabled(account_id) or rollout_enabled(account_id)


def store_for(account_id: int) -> Any:
    global _store, _store_path
    if (not enabled_for(account_id) or not settings.REVIEW_EVIDENCE_STORE_ENABLED
            or not settings.REVIEW_EVIDENCE_STORE_PATH):
        return None
    if not component_budget_status()["ok"]:
        return None
    from pathlib import Path

    path = settings.REVIEW_EVIDENCE_STORE_PATH
    if not Path(path).is_absolute():
        return None
    if _store is None or _store_path != path:
        from app.modules.acquisition.review_evidence_store import (
            EvidenceStoreConfig,
            ReviewEvidenceStore,
        )

        _store = ReviewEvidenceStore(EvidenceStoreConfig(directory=path, enabled=True))
        _store_path = path
    return _store


def maintenance_store(account_id: int | None = None) -> Any:
    """Open an existing ledger even after rollout/admission is switched off."""
    global _store, _store_path
    from pathlib import Path

    from app.modules.acquisition.review_evidence_store import (
        FILE_NAME,
        EvidenceStoreConfig,
        ReviewEvidenceStore,
    )

    path = settings.REVIEW_EVIDENCE_STORE_PATH
    if not path or not Path(path).is_absolute():
        return _store
    if _store is not None and _store_path == path:
        return _store
    if not (Path(path) / FILE_NAME).is_file():
        return None
    _store = ReviewEvidenceStore(EvidenceStoreConfig(directory=path, enabled=True))
    _store_path = path
    return _store


def _date(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


def _stamp(value: Any) -> str | None:
    value = _date(value)
    return value.isoformat() if value is not None else None


def content_version(body: str, *, sender_id: int | None, date: Any, edit_date: Any) -> str:
    material = [body, sender_id, _stamp(date), _stamp(edit_date)]
    return hashlib.sha256(json.dumps(material, ensure_ascii=False).encode()).hexdigest()


def _record(message: Any, account_id: int, peer: int, member: dict, deps: dict,
            users: dict[int, Any]) -> dict:
    body = str(getattr(message, "message", None) or "")
    sender_id = getattr(message, "sender_id", None)
    sender = getattr(message, "sender", None) or users.get(sender_id)
    reply = getattr(message, "reply_to", None)
    replies = getattr(message, "replies", None)
    version = content_version(body, sender_id=sender_id, date=message.date,
                              edit_date=getattr(message, "edit_date", None))
    return {
        "key": f"message:{message.id}:{version}",
        "body": body,
        "event_at": _stamp(getattr(message, "edit_date", None) or message.date),
        "metadata": {
            "message_id": message.id, "version": version, "account_id": account_id,
            "group_id": peer, "joined_at": member.get("joined_at"),
            "membership_id": member.get("membership_id"), "sender_id": sender_id,
            "sender_bot": getattr(sender, "bot", None),
            "date": _stamp(message.date),
            "edit_date": _stamp(getattr(message, "edit_date", None)),
            "reply_to_msg_id": getattr(reply, "reply_to_msg_id", None),
            "reply_to_top_id": getattr(reply, "reply_to_top_id", None),
            "forum_topic": bool(getattr(reply, "forum_topic", False)),
            "media": getattr(message, "media", None) is not None,
            "replies_count": getattr(replies, "replies", None),
            "out": bool(getattr(message, "out", False)),
            "dependencies": deps,
        },
    }


def capture_envelope(envelope: Any, policy: dict | None, *, store: Any = None,
                     allow_admission: bool = True) -> dict[tuple[int, int], dict]:
    """Capture before either ingress compaction path, with no Telegram request."""
    if not policy or type(policy.get("account_id")) is not int:
        return {}
    account_id = policy["account_id"]
    store = store if store is not None else store_for(account_id)
    if store is None:
        return {}
    from app.core.account.listener_interest import fresh
    from app.core.account.listener_protocol import dependencies, updates_in
    from app.modules.acquisition.group_qualification import WARNING, looks_like_ad, possible_rule

    groups = policy.get("groups", {})
    users = {user.id: user for user in (getattr(envelope, "users", None) or [])}
    deps = dependencies(envelope)
    refs: dict[tuple[int, int], dict] = {}
    observations: dict[int, list[dict]] = {}

    def watched(peer: int) -> list[dict]:
        if peer not in observations:
            result = store.list_observations_sync(account_id=account_id, group_id=peer)
            observations[peer] = (result.data or {}).get("observations", []) if result.ok else []
        return observations[peer]

    def control(peer: int, kind: str, *, message_ids: set[int] | None = None,
                source_id: int | None = None) -> None:
        for row in watched(peer):
            current = row["control"]
            if message_ids is None or current["message_id"] in message_ids:
                store.record_control_sync(row["handle"], kind, source_id=source_id)

    try:
        updates = updates_in(envelope)
        if isinstance(envelope, types.UpdateShortChatMessage):
            short = types.Message(id=envelope.id, peer_id=types.PeerChat(envelope.chat_id),
                                  from_id=types.PeerUser(envelope.from_id), date=envelope.date,
                                  message=envelope.message, out=envelope.out,
                                  reply_to=envelope.reply_to)
            updates = [types.UpdateNewMessage(short, envelope.pts, envelope.pts_count)]
        for update in updates:
            message = getattr(update, "message", None)
            if isinstance(message, types.Message):
                peer = utils.get_peer_id(message.peer_id)
                member = groups.get(peer)
                if not member or not member.get("joined_at") or getattr(message, "out", False):
                    continue
                body = message.message or ""
                editing = isinstance(update, (types.UpdateEditMessage, types.UpdateEditChannelMessage))
                reply_id = getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None)
                if not (editing or looks_like_ad(body) or possible_rule(body) or WARNING.search(body) or reply_id
                        or message.id in member.get("watched_evidence", [])):
                    continue
                if editing:
                    store.invalidate_messages_sync(account_id, peer, [message.id])
                    version = content_version(body, sender_id=message.sender_id,
                                              date=message.date, edit_date=message.edit_date)
                    for row in watched(peer):
                        old = row["control"]
                        if old["message_id"] == message.id and old["version"] != version:
                            store.record_control_sync(row["handle"], "edited", source_id=message.id)
                if WARNING.search(body):
                    control(peer, "warning", source_id=message.id)
                if not allow_admission:
                    continue
                roots = {row["control"]["message_id"] for row in watched(peer)}
                relevant = (looks_like_ad(body) or possible_rule(body)
                            or message.id in member.get("watched_evidence", [])
                            or message.id in roots or reply_id in roots)
                if not relevant:
                    continue
                record = _record(message, account_id, peer, member, deps, users)
                record["metadata"]["evidence_kind"] = (
                    "warning" if WARNING.search(body) else "rule" if possible_rule(body)
                    else "reply" if reply_id else "candidate")
                # Every selected rule/reply is paid from each existing slot's
                # original reservation. Overflow invalidates reuse, never drops
                # the old root or silently claims complete dependencies.
                for row in watched(peer):
                    root = row["control"]["message_id"]
                    if (record["metadata"]["evidence_kind"] in {"warning", "rule"}
                            or reply_id == root):
                        store.append_dependencies_sync(row["handle"], [record])
                stored = store.capture_sync(account_id=account_id, group_id=peer, **record)
                if stored.ok:
                    refs[peer, message.id] = {
                        key: record["metadata"][key]
                        for key in ("version", "account_id", "group_id", "joined_at", "message_id")
                    } | {"key": record["key"]}
                created = _date(message.date)
                changed = _date(getattr(message, "edit_date", None)) or created
                if (fresh(policy) and member.get("evidence_needed") and looks_like_ad(body)
                        and created is not None and changed is not None
                        and datetime.utcnow() - timedelta(hours=72) <= created <= changed <= datetime.utcnow()
                        and (datetime.utcnow() - changed) < timedelta(hours=24)
                        and not record["metadata"]["forum_topic"]):
                    dependencies_result = store.dependency_records_sync(
                        account_id, peer, member["joined_at"], message.id)
                    if not dependencies_result.ok:
                        continue
                    retained = [record] + [item for item in dependencies_result.data["records"]
                                           if item["key"] != record["key"]]
                    admitted = store.admit_observation_sync(
                        account_id=account_id, group_id=peer, message_id=message.id,
                        version=record["metadata"]["version"], joined_at=member["joined_at"],
                        date=message.date, edit_date=message.edit_date, records=retained,
                        metadata={"membership_id": member.get("membership_id")},
                    )
                    if admitted.ok:
                        observations.pop(peer, None)
            elif isinstance(update, types.UpdateDeleteChannelMessages):
                peer = -(10**12 + update.channel_id)
                if peer in groups:
                    store.invalidate_messages_sync(account_id, peer, update.messages)
                    control(peer, "deleted", message_ids=set(update.messages))
            elif isinstance(update, types.UpdateDeleteMessages):
                for peer in groups:
                    if -(10**12) < peer < 0:
                        store.invalidate_messages_sync(account_id, peer, update.messages)
                        control(peer, "deleted", message_ids=set(update.messages))
            elif isinstance(update, (types.UpdateChannel, types.UpdateChannelTooLong)):
                peer = -(10**12 + update.channel_id)
                if peer in groups:
                    store.invalidate_messages_sync(account_id, peer)
                    control(peer, "gap")
            elif isinstance(update, types.UpdatesTooLong):
                for peer in groups:
                    store.invalidate_messages_sync(account_id, peer)
                    control(peer, "gap")
            elif isinstance(update, types.UpdateChannelParticipant):
                peer = -(10**12 + update.channel_id)
                if peer in groups:
                    control(peer, "unknown", source_id=update.user_id)
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        # Optional persistence never changes SDK handling or the durable cues.
        return {}
    return refs


def attach_event_refs(facts: list[dict], refs: dict[tuple[int, int], dict]) -> None:
    for fact in facts:
        if fact.get("kind") != "rules":
            continue
        matched = {str(mid): refs[fact["peer"], mid] for mid in fact.get("message_ids", [])
                   if (fact["peer"], mid) in refs}
        if matched:
            fact["evidence_refs"] = matched


async def candidate_ids(member: Any, now: datetime, *, group_id: int | None = None,
                        mature_only: bool = False) -> list[int]:
    store = store_for(member.account_id) or maintenance_store(member.account_id)
    if store is None or member.joined_at is None:
        return []
    result = await store.list_observations(account_id=member.account_id,
                                           group_id=group_id if group_id is not None else member.telegram_group_id,
                                           joined_at=member.joined_at)
    rows = (result.data or {}).get("observations", []) if result.ok else []
    rows = [row["control"] for row in rows if not row["control"].get("facts")
            and row["control"].get("state") == "observing"]
    instant = (now.replace(tzinfo=UTC) if now.tzinfo is None else now).timestamp()
    if mature_only:
        rows = [row for row in rows if row["ready_at"] <= instant]
    rows.sort(key=lambda row: (row["ready_at"] > instant, row["ready_at"]))
    return [row["message_id"] for row in rows[:6]]


async def cached_event_messages(*, account_id: int, group_id: int | str,
                                joined_at: Any, refs: dict, client: Any) -> list[Any]:
    """Return only exact recent pushes with SDK continuity, never mature proof."""
    store = store_for(account_id)
    if store is None or not isinstance(refs, dict):
        return []
    from app.modules.acquisition.review_evidence_ingest import cache_safe
    if not cache_safe(account_id):
        return []
    from app.core.account.listener_protocol import satisfied

    box = getattr(client, "_message_box", None)
    if box is None:
        return []
    journal = getattr(getattr(client, "session", None), "journal", None)
    if journal is None:
        return []
    try:
        pending_facts = journal.facts(limit=1001)
        if len(pending_facts) >= 1001:
            return []
        for _key, raw in pending_facts:
            fact = json.loads(raw)
            if str(fact.get("peer")) == str(group_id) and fact.get("kind") in {"rules", "gap", "membership"}:
                return []
    except (ValueError, OSError, AttributeError):
        return []
    messages = []
    for mid, ref in list(refs.items())[:32]:
        if (not isinstance(ref, dict) or str(ref.get("message_id")) != str(mid)
                or ref.get("account_id") != account_id
                or str(ref.get("group_id")) != str(group_id)
                or _stamp(ref.get("joined_at")) != _stamp(joined_at)):
            continue
        result = await store.get_cache(account_id, group_id, ref.get("key", ""))
        if not result.ok:
            continue
        data = result.data or {}
        meta = data.get("metadata") or {}
        if (meta.get("version") != ref.get("version") or meta.get("message_id") != ref.get("message_id")
                or meta.get("account_id") != account_id or str(meta.get("group_id")) != str(group_id)
                or _stamp(meta.get("joined_at")) != _stamp(joined_at)
                or not 0 <= time.time() - float(data.get("event_at", 0)) <= 90
                or not satisfied({"global": 1, **(meta.get("dependencies") or {})}, box)):
            continue
        peer_id = int(group_id)
        peer = types.PeerChannel(-peer_id - 10**12) if peer_id < -(10**12) else types.PeerChat(-peer_id)
        sender_id = meta.get("sender_id")
        if type(sender_id) is not int or sender_id <= 0:
            continue
        reply = None
        if meta.get("reply_to_msg_id"):
            reply = types.MessageReplyHeader(reply_to_msg_id=meta["reply_to_msg_id"],
                                            reply_to_top_id=meta.get("reply_to_top_id"),
                                            forum_topic=meta.get("forum_topic"))
        message = types.Message(id=meta["message_id"], peer_id=peer,
                                from_id=types.PeerUser(sender_id), message=data["body"],
                                date=_date(meta.get("date")), edit_date=_date(meta.get("edit_date")),
                                reply_to=reply, out=False,
                                media=types.MessageMediaUnsupported() if meta.get("media") else None)
        message._client = client
        if type(meta.get("replies_count")) is int:
            message.replies = types.MessageReplies(replies=meta["replies_count"], replies_pts=0)
        if type(meta.get("sender_bot")) is bool:
            message._sender = types.User(id=sender_id, bot=meta["sender_bot"])
        message._vanguard_event_evidence = True
        messages.append(message)
    return messages


async def cached_hint_messages(*, account_id: int, group_id: int | str,
                               joined_at: Any, message_ids: list[int], client: Any) -> list[Any]:
    """Seed first/event collection with exact recent pushes, never mature proof."""
    store = store_for(account_id)
    if store is None or joined_at is None:
        return []
    refs = await store.hint_refs(account_id, group_id, joined_at, message_ids)
    if not refs.ok:
        return []
    return await cached_event_messages(account_id=account_id, group_id=group_id,
                                       joined_at=joined_at, refs=refs.data["refs"], client=client)


async def retain_candidates(member: Any, snapshot: dict, *, group_id: int | None = None) -> None:
    """Reserve young ads discovered by the existing live collector as well."""
    store = store_for(member.account_id)
    if store is None or member.joined_at is None:
        return
    if group_id is None:
        from app.modules.acquisition.qualification_identity import peer_identity

        identity = peer_identity(member.telegram_group_id, snapshot)
        if identity is None or identity[1] is None:
            return
        group_id = -(10**12 + identity[0]) if identity[1] == "channel" else -identity[0]
    for item in snapshot.get("evidence", []):
        if (item.get("source") != "recent_promotional_message"
                or item.get("sender_role") != "ordinary" or item.get("system_account")
                or item.get("topic_id") is not None or item.get("accessible") is not True
                or item.get("warning_reply_ids") or not 0 <= float(item.get("age_hours") or 0) < 24):
            continue
        version = content_version(item.get("text", ""), sender_id=item.get("sender_id"),
                                  date=item.get("date"), edit_date=item.get("edited_at"))
        records = [{"key": f"message:{item['message_id']}:{version}", "body": item.get("text", ""),
                    "metadata": {"source": "live_collection", "evidence": {
                        key: value for key, value in item.items() if key != "text"}}}]
        # A complete task is admitted atomically: original root, already-read
        # rules and required reply bodies all share the same fixed reservation.
        materials = [value for value in snapshot.get("evidence", [])
                     if value.get("source") != "recent_promotional_message"]
        materials += [value for value in snapshot.get("evidence_dependencies", [])
                      if value.get("message_id") == item["message_id"]
                      or value.get("reply_to_message_id", value.get("reply_to_msg_id")) == item["message_id"]]
        for dependency in materials:
            dependency_version = content_version(dependency.get("text", ""),
                sender_id=dependency.get("sender_id"), date=dependency.get("date"),
                edit_date=dependency.get("edited_at"))
            records.append({"key": f"dependency:{dependency.get('source', 'reply')}:{dependency.get('message_id')}:{dependency_version}",
                            "body": dependency.get("text", ""),
                            "event_at": dependency.get("edited_at") or dependency.get("date") or snapshot.get("collected_at"),
                            "metadata": {"source": "live_dependency", "evidence": {
                                key: value for key, value in dependency.items() if key != "text"}}})
        pushed = await store.dependency_records(member.account_id, group_id, member.joined_at, item["message_id"])
        if not pushed.ok:
            continue
        records.extend(pushed.data["records"])
        result = await store.admit_observation(
            account_id=member.account_id, group_id=group_id,
            joined_at=member.joined_at, message_id=item["message_id"], version=version,
            date=item["date"], edit_date=item.get("edited_at"),
            records=records,
            metadata={"membership_id": member.id},
        )
        if result.ok and result.reason == "observation_replay" and len(records) > 1:
            await store.append_dependencies(result.handle, records[1:])
        if not result.ok and "capacity" in result.reason:
            break


async def evidence_status(account_id: int | None = None) -> dict:
    if account_id is not None:
        store = store_for(account_id)
    else:
        store = _store
    if store is None:
        store = maintenance_store(account_id)
    if store is None:
        return {"enabled": False, "reason": "review_evidence_store_disabled"}
    result = await store.stats()
    return {"enabled": result.ok, "reason": result.reason, **(result.data or {})}
