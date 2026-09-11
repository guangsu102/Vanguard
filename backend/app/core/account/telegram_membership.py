"""Strict Telethon channel-membership response classification."""

from __future__ import annotations

from typing import Literal

from telethon.tl import types

TelethonMembershipState = Literal["verified", "not_member", "unknown"]

_VERIFIED_PARTICIPANT_TYPES = (
    types.ChannelParticipant,
    types.ChannelParticipantSelf,
    types.ChannelParticipantAdmin,
    types.ChannelParticipantCreator,
)
_NON_MEMBER_PARTICIPANT_TYPES = (
    types.ChannelParticipantBanned,
    types.ChannelParticipantLeft,
)


def classify_telethon_membership_response(response: object) -> TelethonMembershipState:
    """Only explicit active ChannelParticipant variants are considered verified."""

    participant = getattr(response, "participant", None)
    if isinstance(participant, _VERIFIED_PARTICIPANT_TYPES):
        return "verified"
    if isinstance(participant, _NON_MEMBER_PARTICIPANT_TYPES):
        return "not_member"
    return "unknown"
