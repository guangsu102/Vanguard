"""History page facts in the bounded ordinary evidence area.

These are traversal dependencies, never cached participant rights or mature
advertising proof. The default path requires an exact SDK/event snapshot.
Optional dependency resume can explain a continuous channel PTS transition and
retain only unaffected pages; every unknown transition still uses fresh reads.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections import Counter
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import structlog
from telethon import utils
from telethon._updates.messagebox import ENTRY_ACCOUNT
from telethon.tl import types

VERSION = 1
DEPENDENCY_TTL_SECONDS = 72 * 3600
REQUIRED_SCOPE = {"membership_id", "joined_at", "event", "policy", "profile", "system_identity"}
_GATES = {
    "scope_invalid": "scope", "ingest_unavailable": "ingest", "ingest_stopped": "ingest",
    "ingest_controls_pending": "ingest", "ingest_account_pending": "ingest", "ingest_dirty": "ingest",
    "sdk_state_unavailable": "sdk_state", "sdk_watermark_unavailable": "sdk_state",
    "sdk_gap": "protocol", "dependency_unproven": "protocol", "fingerprint_invalid": "protocol",
    "facts_overflow": "pending_facts", "fact_gap": "pending_facts", "fact_scope_changed": "pending_facts",
    "page_reference_unavailable": "unavailable", "page_store_unavailable": "unavailable",
    "page_changed_during_load": "mismatch", "page_metadata_invalid": "mismatch",
    "page_records_invalid": "mismatch", "page_dependency_changed": "mismatch",
    "page_load_invalid": "mismatch", "save_scope_changed": "save_rejected",
    "save_segment_incomplete": "save_rejected", "save_store_rejected": "save_rejected",
    "save_invalid": "save_rejected", "page_loaded": "hit",
    "fingerprint_ready": None, "page_saved": None,
}
_gate_counts: Counter[str] = Counter()
_last_gate_log = 0.0


def diagnostics() -> dict[str, int]:
    """Process-local evaluation counts, not task hit rates or avoided RPCs."""
    return dict(_gate_counts)


def _gate(reason: str) -> None:
    # Fixed reason names keep storage and log cardinality independent of the
    # number of groups, accounts and payloads. Persist only inside real work.
    global _last_gate_log
    if reason not in _GATES:
        reason = "fingerprint_invalid"
    _gate_counts[reason] = min((1 << 63)-1, _gate_counts[reason] + 1)
    from app.core.account.read_metrics import mark_history_cache_gate

    if _GATES[reason] is not None:
        mark_history_cache_gate(_GATES[reason])
    instant = time.monotonic()
    if instant - _last_gate_log >= 60:
        _last_gate_log = instant
        structlog.get_logger().info("review_history_cache_gates", evaluations=diagnostics())


def _dependency_state(client: Any, account_id: int, group_id: int, scope: dict) -> dict | None:
    from app.modules.acquisition import review_history_dependencies as dependencies
    from app.modules.acquisition.review_read_flags import stage_enabled

    if not stage_enabled(account_id, "dependency_resume") or group_id >= -10**12:
        return None
    box = client._message_box
    channel_id = -group_id - 10**12
    current = dependencies.state(client.session.journal, channel_id, box.map[channel_id].pts)
    if current is None:
        return None
    base = {key: value for key, value in scope.items() if key != "event"}
    generation = client._vanguard_history_generation[0]
    return {**current, "base": hashlib.sha256(json.dumps(
                base, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest(),
            "event": scope["event"], "generation": generation,
            "seq": box.seq, "account_pts": box.map[ENTRY_ACCOUNT].pts}


def _date(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


def _stamp(value: Any) -> str | None:
    value = _date(value)
    return value.isoformat() if value is not None else None


def group_key(entity: Any) -> int | None:
    try:
        return utils.get_peer_id(entity)
    except (TypeError, ValueError):
        identifier = getattr(entity, "id", None)
        if type(identifier) is int and identifier > 0 and hasattr(entity, "megagroup"):
            return -(10**12 + identifier)
        return None


def fingerprint(client: Any, account_id: int, group_id: int, scope: dict | None) -> str | None:
    """Observe local protocol state only; never acknowledge or advance it."""
    from app.modules.acquisition.review_evidence_ingest import cache_safe, cache_state

    if not isinstance(scope, dict) or not REQUIRED_SCOPE <= set(scope):
        _gate("scope_invalid")
        return None
    if not cache_safe(account_id):
        _gate(cache_state(account_id)["reason"])
        return None
    box = getattr(client, "_message_box", None)
    journal = getattr(getattr(client, "session", None), "journal", None)
    if box is None or journal is None:
        _gate("sdk_state_unavailable")
        return None
    try:
        if box.getting_diff_for or box.possible_gaps:
            _gate("sdk_gap")
            return None
        facts = journal.facts(limit=1001)
        if len(facts) >= 1001:
            _gate("facts_overflow")
            return None
        for _key, raw in facts:
            fact = json.loads(raw)
            if (fact.get("kind") == "gap" or
                    (str(fact.get("peer")) == str(group_id)
                     and fact.get("kind") in {"rules", "membership"})):
                _gate("fact_gap" if fact.get("kind") == "gap" else "fact_scope_changed")
                return None
        account_pts = box.map.get(ENTRY_ACCOUNT)
        channel_pts = box.map.get(-group_id - 10**12) if group_id < -10**12 else None
        if account_pts is None or (group_id < -10**12 and channel_pts is None):
            _gate("sdk_watermark_unavailable")
            return None
        sender = getattr(client, "_sender", None)
        connection = getattr(sender, "_connection", None)
        generation = getattr(client, "_vanguard_history_generation", None)
        if generation is None or generation[1] is not sender or generation[2] is not connection:
            # Hold references while this generation is current: object ID reuse
            # and process restarts cannot accidentally match a persisted bundle.
            generation = (uuid.uuid4().hex, sender, connection)
            client._vanguard_history_generation = generation
        material = [VERSION, account_id, group_id, scope, generation[0],
                    box.seq, account_pts.pts, channel_pts.pts if channel_pts else None]
        from app.modules.acquisition.review_read_flags import stage_enabled
        if stage_enabled(account_id, "dependency_resume") and group_id < -10**12:
            dependency_state = _dependency_state(client, account_id, group_id, scope)
            if dependency_state is None:
                _gate("dependency_unproven")
                return None
            material.append(dependency_state)
        identity = hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False,
                                            allow_nan=False).encode()).hexdigest()
        _gate("fingerprint_ready")
        return identity
    except (AttributeError, ValueError, TypeError, OSError):
        _gate("fingerprint_invalid")
        return None


def _raw_identity(account_id: int, group_id: int, scope: dict) -> str:
    material = ["raw-discovery-v1", account_id, group_id,
                scope.get("membership_id"), scope.get("joined_at")]
    return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()


def page_identity(client: Any, account_id: int, group_id: int, scope: dict | None) -> str | None:
    from app.modules.acquisition.qualification_engine import enabled_for
    if enabled_for(account_id) and isinstance(scope, dict) and REQUIRED_SCOPE <= set(scope):
        # Only discovery pages use this stable scope. Exact-ID handoff and live
        # proofs continue to use fingerprint() with its connection/event fence.
        return _raw_identity(account_id, group_id, scope)
    return fingerprint(client, account_id, group_id, scope)


def _record(message: Any) -> dict[str, Any]:
    reply, forward = getattr(message, "reply_to", None), getattr(message, "fwd_from", None)
    peer = getattr(forward, "from_id", None)
    forward_origin = None
    if forward is not None:
        forward_origin = {"kind": type(peer).__name__,
                          "id": next((getattr(peer, field) for field in
                                      ("user_id", "channel_id", "chat_id")
                                      if getattr(peer, field, None) is not None), None),
                          "channel_post": getattr(forward, "channel_post", None),
                          "date": _stamp(getattr(forward, "date", None)),
                          "hidden": bool(getattr(forward, "from_name", None))}
    return {"key": str(message.id),
            "body": str(getattr(message, "message", None) or getattr(message, "text", None) or ""),
            "metadata": {"message_id": message.id, "sender_id": getattr(message, "sender_id", None),
                         "date": _stamp(message.date), "edit_date": _stamp(getattr(message, "edit_date", None)),
                         "media": getattr(message, "media", None) is not None,
                         "action": getattr(message, "action", None) is not None,
                         "reply_to_msg_id": getattr(reply, "reply_to_msg_id", None),
                         "reply_to_top_id": getattr(reply, "reply_to_top_id", None),
                         "forum_topic": bool(getattr(reply, "forum_topic", False)),
                         "replies_count": getattr(getattr(message, "replies", None), "replies", None),
                         "forward": forward_origin}}


def _message(record: dict) -> Any:
    meta = record["metadata"]
    reply = SimpleNamespace(reply_to_msg_id=meta.get("reply_to_msg_id"),
                            reply_to_top_id=meta.get("reply_to_top_id"),
                            forum_topic=meta.get("forum_topic")) if meta.get("reply_to_msg_id") else None
    forward = meta.get("forward")
    if forward:
        peer_class = {"PeerUser": types.PeerUser, "PeerChannel": types.PeerChannel,
                      "PeerChat": types.PeerChat}.get(forward.get("kind"))
        forward = SimpleNamespace(from_id=peer_class(forward["id"]) if peer_class and forward.get("id") else None,
                                  channel_post=forward.get("channel_post"), date=_date(forward.get("date")),
                                  from_name="hidden" if forward.get("hidden") else None)
    return SimpleNamespace(id=meta["message_id"], sender_id=meta.get("sender_id"), sender=None,
                           message=record["body"], date=_date(meta["date"]), edit_date=_date(meta.get("edit_date")),
                           action=True if meta.get("action") else None,
                           media=True if meta.get("media") else None,
                           reply_to=reply, fwd_from=forward,
                           replies=SimpleNamespace(replies=meta["replies_count"]) if type(meta.get("replies_count")) is int else None,
                           _vanguard_history_evidence=True)


async def load(store: Any, *, client: Any, account_id: int, group_id: int, scope: dict,
               cursor: dict) -> dict | None:
    identity = page_identity(client, account_id, group_id, scope)
    key = cursor.get("page_key")
    if identity is None or not isinstance(key, str):
        _gate("page_reference_unavailable")
        return None
    try:
        response = await store.load_history_page(account_id, group_id, key=key)
        if not response.ok:
            _gate("page_store_unavailable")
            return None
        if page_identity(client, account_id, group_id, scope) != identity:
            _gate("page_changed_during_load")
            return None
        data, meta = response.data, response.data["metadata"]
        observed = float(meta["observed_at"])
        ttl = meta.get("cache_ttl_seconds", 300)
        from app.modules.acquisition.qualification_engine import enabled_for
        from app.modules.acquisition.review_read_flags import stage_enabled
        raw_only = enabled_for(account_id) and meta.get("raw_scope") == _raw_identity(account_id, group_id, scope)
        if (ttl not in (300, DEPENDENCY_TTL_SECONDS)
                or (ttl != 300 and not raw_only and (not stage_enabled(account_id, "dependency_resume")
                                   or not isinstance(meta.get("dependencies"), dict)))
                or meta.get("version") != VERSION or not 0 <= time.time()-observed < ttl
                or any(meta.get(field) != cursor.get(field)
                       for field in ("window_end", "offset_id", "complete"))):
            _gate("page_metadata_invalid")
            return None
        start, end = _date(meta["window_start"]), _date(meta["window_end"])
        if start is None or end is None or (end-start).total_seconds() != 86400:
            _gate("page_metadata_invalid")
            return None
        messages = [_message(record) for record in data["records"]]
        if (len({message.id for message in messages}) != len(messages)
                or any(type(message.id) is not int or message.id <= 0 or message.date is None
                       or not start <= message.date <= end for message in messages)):
            _gate("page_records_invalid")
            return None
        if meta.get("identity") != identity and not raw_only:
            from app.modules.acquisition import review_history_dependencies as dependencies

            saved = meta.get("dependencies")
            current = _dependency_state(client, account_id, group_id, scope)
            if (not isinstance(saved, dict) or current is None
                    or any(saved.get(key) != current.get(key) for key in ("base", "generation"))):
                _gate("page_dependency_changed")
                return None
            # Account-wide seq/PTS can advance for other chats. Channel raw
            # history is fenced by the complete channel transition instead;
            # account/member rights and live root checks remain uncached.
            changed_ids = dependencies.event_changes(saved.get("event"), scope["event"],
                                                     joined_at=scope["joined_at"])
            message_ids = {message.id for message in messages}
            if (changed_ids is None or changed_ids.intersection(message_ids)
                    or not dependencies.unaffected(client.session.journal, -group_id-10**12,
                        saved, current, start=start, end=end, message_ids=message_ids)):
                _gate("page_dependency_changed")
                return None
        if page_identity(client, account_id, group_id, scope) != identity:
            _gate("page_changed_during_load")
            return None
        _gate("page_loaded")
        return {"messages": messages, "observed_at": observed, "sample_count": int(meta["sample_count"]),
                "calls": int(meta["calls"]), "window_start": start, "window_end": end,
                "coverage_current": not raw_only}
    except (AttributeError, KeyError, ValueError, TypeError, OSError):
        _gate("page_load_invalid")
        return None


async def save(store: Any, *, client: Any, account_id: int, group_id: int, scope: dict,
               identity: str, segment: dict) -> str | None:
    if identity is None or page_identity(client, account_id, group_id, scope) != identity:
        _gate("save_scope_changed")
        return None
    from app.modules.acquisition.qualification_engine import enabled_for
    raw_only = enabled_for(account_id)
    invalid_keys = ("stalled", "cache_invalid") if raw_only else ("error", "stalled", "resumed_hint", "cache_invalid")
    if (any(segment.get(key) for key in invalid_keys)
            or "tail_offset" in segment):
        _gate("save_segment_incomplete")
        return None
    try:
        records = [_record(message) for message in segment["messages"].values()]
        metadata = {"version": VERSION, "identity": identity,
                    "window_start": segment["start"].isoformat(), "window_end": segment["end"].isoformat(),
                    "offset_id": segment["offset_id"], "complete": bool(segment["complete"]),
                    "sample_count": segment["sample_count"], "calls": segment["calls"]}
        dependencies = _dependency_state(client, account_id, group_id, scope)
        ttl = DEPENDENCY_TTL_SECONDS if dependencies is not None or raw_only else 300
        metadata["cache_ttl_seconds"] = ttl
        if raw_only:
            metadata["raw_scope"] = _raw_identity(account_id, group_id, scope)
        if dependencies is not None:
            metadata["dependencies"] = dependencies
        key = uuid.uuid4().hex
        response = await store.save_history_page(account_id, group_id, key=key, records=records,
                                                metadata=metadata, observed_at=segment["source_observed_at"],
                                                ttl_seconds=ttl)
        if not response.ok:
            _gate("save_store_rejected")
            return None
        if page_identity(client, account_id, group_id, scope) != identity:
            _gate("save_scope_changed")
            return None
        _gate("page_saved")
        return key
    except (AttributeError, KeyError, ValueError, TypeError, OSError):
        _gate("save_invalid")
        return None
