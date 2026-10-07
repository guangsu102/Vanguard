"""Short-lived rule observations scoped to one membership and change event."""

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any


def resume(previous: dict, event: dict, member: Any, now: datetime) -> dict:
    scope = {'event': {k: v for k, v in event.items() if k != 'queued'},
             'membership': member.id, 'joined_at': member.joined_at.isoformat(),
             'policy': previous.get('policy_version'), 'profile': previous.get('profile_fingerprint')}
    identity = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
    saved = previous.get('renewal_progress') or {}
    try:
        started = datetime.fromisoformat(saved['started_at'])
        if saved.get('scope') == identity and now - timedelta(minutes=5) <= started <= now:
            return {**saved, 'checked': dict(saved.get('checked') or {})}
    except (KeyError, TypeError, ValueError):
        pass
    return {'scope': identity, 'started_at': now.isoformat(), 'checked': {}}


def signature(message: Any) -> str:
    from app.modules.acquisition.group_qualification import naive, text_of

    value = [message.id, getattr(message, 'sender_id', None), text_of(message),
             str(naive(getattr(message, 'date', None))), str(naive(getattr(message, 'edit_date', None)))]
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


async def administrator_ids(client: Any, entity: Any, progress: dict | None) -> set[int] | None:
    """A complete live admin list replaces many per-author rule lookups.

    This is only for rule authors; ordinary-member ad precedent still needs a
    live membership check. Permission failures fall back to bounded individual
    lookups and are not retried inside the same short continuation.
    """
    from telethon import errors, types, utils
    from telethon.tl.functions.channels import GetParticipantsRequest

    if not isinstance(entity, types.Channel) or (progress or {}).get('admin_list_denied'):
        return None
    ids: set[int] = set()
    offset = 0
    try:
        for _ in range(3):
            result = await client(GetParticipantsRequest(utils.get_input_channel(entity), types.ChannelParticipantsAdmins(), offset, 100, 0))
            participants = getattr(result, 'participants', None)
            if participants is None:
                return None
            ids.update(p.user_id for p in participants if getattr(p, 'user_id', None))
            offset += len(participants)
            if offset >= result.count:
                return ids if len(ids) >= result.count else None
            if not participants:
                return None
    except errors.ChatAdminRequiredError:
        if progress is not None:
            progress['admin_list_denied'] = True
    return None  # A partial list must never establish that an author is ordinary.
