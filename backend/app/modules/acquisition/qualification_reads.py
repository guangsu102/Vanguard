"""Reuse this collection's resolved peer without caching participant decisions."""
from __future__ import annotations

from typing import Any

from telethon import errors, types, utils
from telethon.tl.custom import ParticipantPermissions
from telethon.tl.functions.channels import GetParticipantRequest


async def current_permissions(
    client: Any, entity: Any, user: Any, *, full_chat: Any = None
) -> Any:
    if isinstance(entity, types.Channel):
        # get_permissions calls get_entity even when a full Channel was just
        # fetched. Resolve the participant as usual, then do the one live RPC.
        peer = await client.get_input_entity(user)
        result = await client(GetParticipantRequest(utils.get_input_channel(entity), peer))
        return ParticipantPermissions(result.participant, False)
    if isinstance(entity, types.Chat) and full_chat is not None:
        participants = getattr(getattr(full_chat, "participants", None), "participants", None)
        if participants is None:
            raise ValueError("participant_list_unavailable")
        peer = await client.get_input_entity(user)
        if isinstance(peer, types.InputPeerSelf):
            peer = await client.get_me(input_peer=True)
        user_id = getattr(peer, "user_id", None)
        if user_id is None:
            raise ValueError("participant_identity_unavailable")
        for participant in participants:
            if participant.user_id == user_id:
                return ParticipantPermissions(participant, True)
        raise errors.UserNotParticipantError(None)
    return await client.get_permissions(entity, user)
