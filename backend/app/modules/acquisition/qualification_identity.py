"""Namespace-aware Telegram peer identities for qualification history.

A positive legacy ID alone does not identify a chat versus a channel. It is a
candidate lookup key, never proof that both marked peers represent one group.
These helpers do not read or mutate Telegram, ORM objects or the database.
"""

from __future__ import annotations

from typing import Any, Literal

Namespace = Literal["chat", "channel"]
PeerIdentity = tuple[int, Namespace | None]
_CHANNEL_MARK = 1_000_000_000_000
_GROUP_TYPES: dict[str, Namespace] = {
    "basic_group": "chat",
    "supergroup": "channel",
    "channel": "channel",
}


def _number(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value and value.lstrip("-").isdigit():
        try:
            return int(value)
        except ValueError:
            pass
    return None


def _id_parts(value: Any) -> PeerIdentity | None:
    value = _number(value)
    if value is None or value == 0 or value == -_CHANNEL_MARK:
        return None
    if value < -_CHANNEL_MARK:
        return -value - _CHANNEL_MARK, "channel"
    if value < 0:
        return -value, "chat"
    return value, None


def peer_identity(
    telegram_id: Any,
    snapshot: dict[str, Any] | None = None,
    namespace: Namespace | None = None,
) -> PeerIdentity | None:
    """Reconcile the stored ID, optional marked snapshot ID and explicit type.

    ``snapshot.group_id`` is an internal database key and is deliberately ignored.
    Any contradiction in the Telegram IDs, raw peer ID or namespaces fails closed.
    """
    identity = _id_parts(telegram_id)
    if identity is None or namespace not in {None, "chat", "channel"}:
        return None
    raw, inferred = identity
    namespaces = {value for value in (inferred, namespace) if value is not None}
    if snapshot is not None:
        if not isinstance(snapshot, dict):
            return None
        recorded_id = snapshot.get("telegram_group_id")
        if recorded_id is not None:
            recorded = _id_parts(recorded_id)
            if recorded is None or recorded[0] != raw:
                return None
            if recorded[1] is not None:
                namespaces.add(recorded[1])
        recorded_raw = snapshot.get("raw_peer_id")
        if recorded_raw is not None:
            recorded_raw = _number(recorded_raw)
            if recorded_raw is None or recorded_raw <= 0 or recorded_raw != raw:
                return None
        group_type = snapshot.get("group_type")
        if group_type is not None:
            kind = _GROUP_TYPES.get(group_type) if isinstance(group_type, str) else None
            if kind is None:
                return None
            namespaces.add(kind)
    if len(namespaces) > 1:
        return None
    return raw, next(iter(namespaces), None)


def entity_identity(entity: Any) -> tuple[int, Namespace] | None:
    """Prefer real Telethon constructors; never guess a bare positive entity ID."""
    from telethon.tl import types

    kind: Namespace | None = None
    value = getattr(entity, "id", None)
    if isinstance(entity, (types.Channel, types.ChannelForbidden)):
        kind = "channel"
    elif isinstance(entity, (types.Chat, types.ChatEmpty, types.ChatForbidden)):
        kind = "chat"
    elif isinstance(entity, (types.PeerChannel, types.InputChannel, types.InputPeerChannel)):
        kind, value = "channel", entity.channel_id
    elif isinstance(entity, (types.PeerChat, types.InputPeerChat)):
        kind, value = "chat", entity.chat_id
    elif isinstance(entity, (types.User, types.UserEmpty, types.PeerUser, types.InputPeerUser)):
        return None
    elif any(
        getattr(entity, field, None) is True for field in ("megagroup", "gigagroup", "broadcast")
    ):
        kind = "channel"
    if kind is None:
        return None
    identity = peer_identity(value, namespace=kind)
    return (identity[0], kind) if identity is not None else None


def _identity_parts(identity: Any) -> PeerIdentity | None:
    if not isinstance(identity, tuple) or len(identity) != 2:
        return None
    raw, namespace = identity
    if type(raw) is not int or raw <= 0 or namespace not in {None, "chat", "channel"}:
        return None
    return raw, namespace


def identity_aliases(identity: PeerIdentity | None) -> set[int]:
    """Return lookup candidates; unknown namespaces still require relation checks."""
    parsed = _identity_parts(identity)
    if parsed is None:
        return set()
    raw, namespace = parsed
    if namespace == "chat":
        return {raw, -raw}
    if namespace == "channel":
        return {raw, -_CHANNEL_MARK - raw}
    return {raw, -raw, -_CHANNEL_MARK - raw}


def identity_relation(
    target: PeerIdentity | None, candidate: PeerIdentity | None
) -> Literal["same", "different", "unknown"]:
    first, second = _identity_parts(target), _identity_parts(candidate)
    if first is None or second is None:
        return "unknown"
    if first[0] != second[0]:
        return "different"
    if first[1] is None or second[1] is None:
        return "unknown"
    return "same" if first[1] == second[1] else "different"
