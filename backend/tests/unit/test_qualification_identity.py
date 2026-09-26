"""Pure peer identity tests: raw aliases never bridge chat/channel namespaces."""

from types import SimpleNamespace

import pytest
from telethon.tl import types

from app.modules.acquisition.qualification_identity import (
    entity_identity,
    identity_aliases,
    identity_relation,
    peer_identity,
)


@pytest.mark.parametrize(
    "value,expected",
    [
        (42, (42, None)),
        ("42", (42, None)),
        (-42, (42, "chat")),
        (-1000000000042, (42, "channel")),
        (0, None),
        (True, None),
        (False, None),
        (42.5, None),
        ("42.0", None),
        (None, None),
        ("not-an-id", None),
        (-1000000000000, None),
    ],
)
def test_peer_id_markings(value, expected):
    assert peer_identity(value) == expected


@pytest.mark.parametrize(
    "stored,snapshot,namespace,expected",
    [
        (42, {"group_type": "basic_group", "raw_peer_id": 42}, None, (42, "chat")),
        (42, {"group_type": "supergroup", "raw_peer_id": 42}, None, (42, "channel")),
        (42, {"group_type": "channel", "telegram_group_id": -1000000000042}, None, (42, "channel")),
        (42, {"telegram_group_id": -42}, None, (42, "chat")),
        (-42, {"telegram_group_id": 42, "group_type": "basic_group"}, None, (42, "chat")),
        (
            -1000000000042,
            {"telegram_group_id": 42, "group_type": "supergroup"},
            None,
            (42, "channel"),
        ),
        (42, {"group_id": 987654321}, None, (42, None)),
        (42, {}, "channel", (42, "channel")),
        (42, {}, "chat", (42, "chat")),
        (42, {"raw_peer_id": 43}, "channel", None),
        (42, {"raw_peer_id": -42}, "chat", None),
        (42, {"raw_peer_id": True}, "chat", None),
        (42, {"telegram_group_id": 43}, "channel", None),
        (42, {"telegram_group_id": -42}, "channel", None),
        (-1000000000042, {"telegram_group_id": -42}, None, None),
        (-42, {"group_type": "supergroup"}, None, None),
        (-1000000000042, {"group_type": "basic_group"}, None, None),
        (42, {"group_type": "private_user"}, None, None),
        (42, {}, "user", None),
        (42, [], None, None),
    ],
)
def test_peer_snapshot_conflicts_fail_closed(stored, snapshot, namespace, expected):
    assert peer_identity(stored, snapshot, namespace) == expected


@pytest.mark.parametrize(
    "entity,expected",
    [
        (types.PeerChat(42), (42, "chat")),
        (types.InputPeerChat(42), (42, "chat")),
        (types.ChatEmpty(42), (42, "chat")),
        (types.ChatForbidden(42, "title"), (42, "chat")),
        (types.PeerChannel(42), (42, "channel")),
        (types.InputPeerChannel(42, 123), (42, "channel")),
        (types.InputChannel(42, 123), (42, "channel")),
        (types.ChannelForbidden(42, 123, "title"), (42, "channel")),
        (types.UserEmpty(42), None),
        (types.PeerUser(42), None),
        (SimpleNamespace(id=42, megagroup=True), (42, "channel")),
        (SimpleNamespace(id=42, broadcast=True), (42, "channel")),
        (SimpleNamespace(id=42, gigagroup=True), (42, "channel")),
        (SimpleNamespace(id=42, megagroup=False, broadcast=False), None),
        (SimpleNamespace(id=42, megagroup="true"), None),
        (SimpleNamespace(id=42), None),
        (SimpleNamespace(id=-42, megagroup=True), None),
        (None, None),
    ],
)
def test_typed_entities_preserve_namespace(entity, expected):
    assert entity_identity(entity) == expected


@pytest.mark.parametrize(
    "identity,expected",
    [
        ((42, "chat"), {42, -42}),
        ((42, "channel"), {42, -1000000000042}),
        ((42, None), {42, -42, -1000000000042}),
        (None, set()),
        ((0, "chat"), set()),
        ((-42, "chat"), set()),
    ],
)
def test_alias_candidates_are_namespace_scoped(identity, expected):
    assert identity_aliases(identity) == expected


@pytest.mark.parametrize(
    "target,candidate,expected",
    [
        ((42, "chat"), (42, "chat"), "same"),
        ((42, "channel"), (42, "channel"), "same"),
        ((42, "chat"), (42, "channel"), "different"),
        ((42, "channel"), (42, "chat"), "different"),
        ((42, None), (43, None), "different"),
        ((42, None), (42, "chat"), "unknown"),
        ((42, "channel"), (42, None), "unknown"),
        ((42, None), (42, None), "unknown"),
        (None, (42, "chat"), "unknown"),
    ],
)
def test_unknown_raw_never_bridges_marked_namespaces(target, candidate, expected):
    assert identity_relation(target, candidate) == expected
