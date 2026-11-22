"""Bounded numeric invalidation facts observed before listener compaction.

This is an optional in-memory proof of a channel PTS transition, not a second
update consumer. It never acknowledges an SDK cursor, retains message bodies,
or treats message continuity as participant/permission continuity.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from telethon.tl import types

MAX_CHANNELS = 256
MAX_CHANGES = 128
MAX_TOTAL_CHANGES = 2048
MAX_IDS = 64


@dataclass(frozen=True, slots=True)
class Change:
    revision: int
    pts: int
    count: int
    kind: str
    ids: tuple[int, ...]
    date: float | None = None


@dataclass(slots=True)
class Channel:
    epoch: str = field(default_factory=lambda: uuid4().hex)
    revision: int = 0
    changes: deque[Change] = field(default_factory=lambda: deque(maxlen=MAX_CHANGES))


class HistoryDependencies:
    def __init__(self) -> None:
        self.epoch = uuid4().hex
        self.channels: OrderedDict[int, Channel] = OrderedDict()

    def channel(self, channel_id: int) -> Channel:
        row = self.channels.get(channel_id)
        if row is None:
            if len(self.channels) >= MAX_CHANNELS:
                self.channels.popitem(last=False)
            row = self.channels[channel_id] = Channel()
        self.channels.move_to_end(channel_id)
        return row

    def invalidate(self, channel_id: int | None = None) -> None:
        if channel_id is None:
            self.epoch = uuid4().hex
            self.channels.clear()
        else:
            self.channels.pop(channel_id, None)

    def observe(self, envelope: Any) -> None:
        from app.core.account.listener_protocol import updates_in

        if isinstance(envelope, types.UpdatesTooLong):
            self.invalidate()
            return
        for update in updates_in(envelope):
            channel_id = getattr(update, "channel_id", None)
            message = getattr(update, "message", None)
            if isinstance(message, (types.Message, types.MessageService)):
                channel_id = getattr(message.peer_id, "channel_id", channel_id)
            if type(channel_id) is not int or channel_id <= 0:
                # Account seq/PTS is checked separately. Unscoped deletions and
                # update gaps can affect any page, including basic groups.
                if isinstance(update, (types.UpdateDeleteMessages, types.UpdatesTooLong)):
                    self.invalidate()
                continue
            if isinstance(update, (types.UpdateNewChannelMessage, types.UpdateEditChannelMessage)):
                if not isinstance(message, types.Message) or not isinstance(message.date, datetime):
                    self.invalidate(channel_id)
                    continue
                identifiers = [message.id]
                reply = getattr(message, "reply_to", None)
                identifiers += [getattr(reply, name, None) for name in
                                ("reply_to_msg_id", "reply_to_top_id")]
                identifiers = [value for value in identifiers if type(value) is int and value > 0]
                # Telegram wire dates have second precision, including a raw
                # envelope deserialized later from the durable journal.
                instant = int((message.date if message.date.tzinfo else message.date.replace(tzinfo=UTC)).timestamp())
                kind = "new" if isinstance(update, types.UpdateNewChannelMessage) else "edit"
            elif isinstance(update, types.UpdateDeleteChannelMessages):
                identifiers, instant, kind = list(update.messages), None, "delete"
            elif isinstance(update, types.UpdatePinnedChannelMessages):
                # Pin state is re-read by the rules collector. It does not
                # modify any historical body or history pagination.
                identifiers, instant, kind = [], None, "pins"
            else:
                self.invalidate(channel_id)
                continue
            pts, count = getattr(update, "pts", None), getattr(update, "pts_count", None)
            if (type(pts) is not int or pts <= 0 or type(count) is not int or count <= 0
                    or len(identifiers) > MAX_IDS
                    or any(type(value) is not int or value <= 0 for value in identifiers)):
                self.invalidate(channel_id)
                continue
            row = self.channel(channel_id)
            signature = (pts, count, kind, tuple(identifiers), instant)
            # Durable replay can deliver the same original envelope twice.
            # A conflicting same-PTS update fences the entire group.
            duplicate = next((change for change in row.changes if change.pts == pts), None)
            if duplicate is not None:
                if (duplicate.pts, duplicate.count, duplicate.kind, duplicate.ids, duplicate.date) != signature:
                    self.invalidate(channel_id)
                continue
            if (len(row.changes) < MAX_CHANGES
                    and sum(len(item.changes) for item in self.channels.values()) >= MAX_TOTAL_CHANGES):
                # The current channel is most recently used. Evicting an old
                # channel changes its next epoch and can only cause misses.
                self.channels.popitem(last=False)
            row.revision += 1
            row.changes.append(Change(row.revision, *signature))


def tracker(journal: Any, *, create: bool = False) -> HistoryDependencies | None:
    result = getattr(journal, "_vanguard_history_dependencies", None)
    if not isinstance(result, HistoryDependencies):
        if not create:
            return None
        result = HistoryDependencies()
        journal._vanguard_history_dependencies = result
    return result


def observe_envelope(journal: Any, envelope: Any, policy: dict | None) -> None:
    """Best-effort optional hook; failures fence reuse and never affect ingress."""
    current = tracker(journal)
    try:
        if current is None:
            from app.modules.acquisition.review_read_flags import stage_enabled

            account_id = policy.get("account_id") if isinstance(policy, dict) else None
            if not stage_enabled(account_id, "dependency_resume"):
                return
            current = tracker(journal, create=True)
        current.observe(envelope)
    except Exception:
        if current is not None:
            current.invalidate()


def state(journal: Any, channel_id: int, pts: int) -> dict | None:
    current = tracker(journal, create=True)
    row = current.channel(channel_id)
    # A pushed update seen before SDK application is pending evidence. It may
    # not be hidden behind the still-old SDK watermark.
    if any(change.pts > pts for change in row.changes):
        return None
    return {"epoch": current.epoch, "channel_epoch": row.epoch,
            "revision": row.revision, "pts": pts}


def unaffected(journal: Any, channel_id: int, saved: dict, current: dict,
               *, start: datetime, end: datetime, message_ids: set[int]) -> bool:
    """Require a complete, ordered explanation for every changed channel PTS."""
    if any(saved.get(key) != current.get(key) for key in ("epoch", "channel_epoch")):
        return False
    old_pts, new_pts = saved.get("pts"), current.get("pts")
    revision, latest = saved.get("revision"), current.get("revision")
    if any(type(value) is not int for value in (old_pts, new_pts, revision, latest)):
        return False
    if old_pts == new_pts and revision == latest:
        return True
    if new_pts <= old_pts or latest <= revision:
        return False
    row = tracker(journal).channels.get(channel_id)
    if row is None:
        return False
    changes = [change for change in row.changes if change.revision > revision]
    if (not changes or changes[0].revision != revision + 1
            or changes[-1].revision != latest):
        return False
    begin = (start if start.tzinfo else start.replace(tzinfo=UTC)).timestamp()
    stop = (end if end.tzinfo else end.replace(tzinfo=UTC)).timestamp()
    cursor = old_pts
    for change in changes:
        if change.pts - change.count != cursor or change.pts > new_pts:
            return False
        if message_ids.intersection(change.ids):
            return False
        if change.kind in {"new", "edit"} and (change.date is None or begin <= change.date <= stop):
            return False
        cursor = change.pts
    return cursor == new_pts


def event_changes(old: Any, new: Any, *, joined_at: str | None) -> set[int] | None:
    """Only a precise known dependency event or its acknowledgement is reusable."""
    from app.modules.acquisition.qualification_events import same_evidence_event

    if same_evidence_event(old, new):
        return set()
    if new in (None, {}):
        return set()  # Acknowledgement is not a new Telegram fact.
    if not isinstance(new, dict) or old is not None and not isinstance(old, dict):
        return None
    prior = old or {}
    kinds = new.get("kinds")
    ids = new.get("message_ids", [])
    if (not isinstance(kinds, list) or not kinds or not set(kinds) <= {"rules", "membership"}
            or not isinstance(ids, list) or len(ids) > 32
            or any(type(value) is not int or value <= 0 for value in ids)
            or new.get("joined_at") != joined_at
            or prior.get("account_generation") != new.get("account_generation")
            or prior.get("unscoped_rules_token") != new.get("unscoped_rules_token")
            or ("rules" in kinds and not ids)):
        return None
    return set(ids) if "rules" in kinds else set()
