"""Protocol dependencies and lossless ordering metadata for discarded chatter."""
from __future__ import annotations

import copy
import time
from typing import Any

from telethon._updates.messagebox import (
    BOT_CHANNEL_DIFF_LIMIT,
    ENTRY_ACCOUNT,
    ENTRY_SECRET,
    USER_CHANNEL_DIFF_LIMIT,
    MessageBox,
    PtsInfo,
)
from telethon.helpers import get_running_loop
from telethon.tl import types
from telethon.tl.functions.updates import GetChannelDifferenceRequest


def updates_in(envelope: Any) -> list[Any]:
    updates = getattr(envelope, "updates", None)
    return updates if updates is not None else [getattr(envelope, "update", envelope)]


def dependencies(envelope: Any) -> dict[str, int]:
    result = {}
    seq = getattr(envelope, "seq", 0)
    if seq:
        result["seq"] = int(seq)
    for update in updates_in(envelope):
        pts = PtsInfo.from_update(update)
        if pts:
            key = "account" if pts.entry is ENTRY_ACCOUNT else "secret" if pts.entry is ENTRY_SECRET else str(pts.entry)
            result[key] = max(result.get(key, 0), pts.pts)
        elif isinstance(update, types.UpdateChannelTooLong):
            result["gap:" + str(update.channel_id)] = 1
    if isinstance(envelope, types.UpdatesTooLong) or not result:
        result["global"] = 1
    return result


def channel_only(envelope: Any) -> bool:
    """A seq-free, channel-PTS-only envelope can progress during account recovery."""
    if (getattr(envelope, "date", None) is None
            or getattr(envelope, "seq", 0) or getattr(envelope, "seq_start", 0)):
        return False
    updates = updates_in(envelope)
    return bool(updates) and all(
        isinstance(update, types.UpdateChannelTooLong)
        or ((pts := PtsInfo.from_update(update)) is not None and type(pts.entry) is int)
        for update in updates
    )


def satisfied(required: dict[str, int], box: Any) -> bool:
    if box is None:
        return False
    gaps = box.getting_diff_for | set(box.possible_gaps)
    for key, target in required.items():
        if key == "global":
            if gaps:
                return False
        elif key == "seq":
            if box.seq < target:
                return False
        elif key.startswith("gap:"):
            if int(key[4:]) in gaps:
                return False
        else:
            entry = ENTRY_ACCOUNT if key == "account" else ENTRY_SECRET if key == "secret" else int(key)
            state = box.map.get(entry)
            if state is None or state.pts < target:
                return False
    return True


def compact(envelope: Any, policy: dict | None) -> tuple[Any, list[int]]:
    """Only discard confidently irrelevant incoming group chatter; no RPCs.

    Unknown bots, verification prompts, direct interactions and mixed service
    updates retain their evidence. A minimal normal TL update still goes through
    SDK pts/seq ordering; a durable sidecar prevents business dispatch on replay.
    """
    if not policy or time.monotonic() - policy["loaded_at"] > 90 or policy["ordinary_required"]:
        return envelope, []
    candidates = updates_in(envelope)
    users = {u.id: u for u in (getattr(envelope, "users", None) or [])}
    compacted, discarded = [], []
    for index, update in enumerate(candidates):
        message = getattr(update, "message", None)
        peer = getattr(message, "peer_id", None)
        channel = getattr(peer, "channel_id", None)
        sender_id = getattr(getattr(message, "from_id", None), "user_id", None)
        sender = users.get(sender_id)
        owned = policy.get("owned_chats", set())
        ordinary = (
            isinstance(update, types.UpdateNewChannelMessage) and isinstance(message, types.Message)
            and channel is not None and channel not in owned and -(10**12 + channel) not in owned
            and not message.out and not message.mentioned and not message.reply_to
            and not (message.message or "").lstrip().startswith("/")
            and sender is not None and getattr(sender, "bot", None) is False
        )
        if ordinary:
            light = types.Message(id=message.id, peer_id=peer, date=message.date, message="", from_id=message.from_id)
            update = types.UpdateNewChannelMessage(light, pts=update.pts, pts_count=update.pts_count)
            update._vanguard_ignore_business = True
            discarded.append(index)
        compacted.append(update)
    if not discarded:
        return envelope, []
    result = copy.copy(envelope)
    if hasattr(result, "updates"):
        result.updates = compacted
    elif hasattr(result, "update"):
        result.update = compacted[0]
    else:
        return envelope, []
    return result, discarded


class ListenerMessageBox(MessageBox):
    __slots__ = ('last_channel',)

    def check_deadlines(self) -> float:
        # This headless listener has no actively viewed channel windows. Joined
        # channels continue receiving pushes; idle per-channel short polling is
        # unnecessary. Keep account recovery, real pts gaps and explicit
        # TooLong requests intact. No pts/date/seq is advanced here.
        # https://core.telegram.org/api/updates#recovering-gaps
        now = get_running_loop().time()
        idle = {entry for entry, state in self.map.items()
                if isinstance(entry, int) and state.deadline <= now
                and entry not in self.possible_gaps and entry not in self.getting_diff_for}
        if idle:
            self.reset_deadlines(idle, now + 900)
        return super().check_deadlines()

    def get_channel_difference(self, chat_hashes: Any) -> Any:
        entries = sorted(entry for entry in self.getting_diff_for if isinstance(entry, int))
        if not entries:
            return None
        previous = getattr(self, 'last_channel', 0)
        entry = next((entry for entry in entries if entry > previous), entries[0])
        self.last_channel = entry
        packed = chat_hashes.get(entry)
        if not packed:
            self.end_get_diff(entry)
            self.map.pop(entry, None)
            return None
        state = self.map.get(entry)
        if state is None:
            raise RuntimeError('channel difference has no committed state')
        return GetChannelDifferenceRequest(force=False, channel=types.InputChannel(packed.id, packed.hash),
                                           filter=types.ChannelMessagesFilterEmpty(), pts=state.pts,
                                           limit=BOT_CHANNEL_DIFF_LIMIT if chat_hashes.self_bot else USER_CHANNEL_DIFF_LIMIT)

    def apply_channel_difference(self, request: Any, diff: Any, chat_hashes: Any) -> Any:
        result = super().apply_channel_difference(request, diff, chat_hashes)
        if isinstance(diff, types.updates.ChannelDifferenceTooLong) and diff.final:
            # The SDK accepts the returned pts but leaves getting_diff_for set.
            # Honor the server's final response; preserve its timeout. Raw
            # uncertain events were separately quarantined before this call.
            self.end_get_diff(request.channel.channel_id)
            self.reset_channel_deadline(request.channel.channel_id, diff.timeout)
        return result

    def apply_pts_info(self, update: Any, *, reset_deadlines: Any) -> Any:
        result = super().apply_pts_info(update, reset_deadlines=reset_deadlines)
        pts = PtsInfo.from_update(update)
        # SDK 1.44/1.45 tests an initially empty set for truthiness. Correctly
        # renew active entries without suppressing any genuine gap/TooLong.
        if reset_deadlines is not None and pts is not None and pts.entry in self.map:
            reset_deadlines.add(pts.entry)
        return result


def install_message_box(client: Any) -> None:
    from telethon import __version__
    old = getattr(client, "_message_box", None)
    if type(old) is MessageBox and __version__ in {"1.44.0", "1.45.0"}:
        client._message_box = ListenerMessageBox(old._log, old.map, old.date, old.seq,
                                                old.next_deadline, old.possible_gaps, old.getting_diff_for)
