"""Persistent listener checkpoints advance only after a whole SDK batch is ingested."""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from typing import Any

from telethon.sessions import SQLiteSession, StringSession


class ListenerSession(SQLiteSession):
    def __init__(self, filename: str):
        self.journal = None
        self.journal_safe = lambda: False
        self.pending_updates = 0
        self._pending_states: dict = {}
        super().__init__(filename)

    def set_update_state(self, entity_id, state):
        # Telethon advances its in-memory pts for an entire batch before it
        # dispatches individual events. Never persist that cursor mid-batch.
        self._pending_states[entity_id] = state

    def save(self):
        if self.pending_updates == 0 and (
            self.journal is None or not self.journal.observed or self.journal.ordered_complete
        ):
            for entity_id, state in self._pending_states.items():
                super().set_update_state(entity_id, state)
            self._pending_states.clear()
            if self.journal is not None:
                self.journal.checkpoint(safe=self.journal_safe())
        super().save()

    def close(self):
        # SDK disconnect calls close(), not save(); flush only completed batches.
        self.save()
        super().close()


def listener_session_path(source: StringSession, base_path: Path) -> Path | None:
    if source.auth_key is None:
        return None
    identity = hashlib.sha256(source.auth_key.key).hexdigest()[:16]
    return base_path.with_name(base_path.stem + ".listener-" + identity + ".session")


def listener_session(source: StringSession, base_path: Path) -> Any:
    path = listener_session_path(source, base_path)
    if path is None:
        return source  # Unauthenticated test/setup clients have no cursor to restore.
    session = ListenerSession(str(path))
    if session.auth_key is None:
        session.set_dc(source.dc_id, source.server_address, source.port)
        session.auth_key = source.auth_key
        session.save()
    os.chmod(path, 0o600)
    return session


def install_listener_checkpoint(client: Any) -> None:
    session = client.session
    if not isinstance(session, ListenerSession):
        return
    from app.core.account.listener_journal import install_journal

    session.journal = install_journal(client, session)
    session.journal_safe = lambda: (
        not (
            getattr(getattr(client, "_message_box", None), "getting_diff_for", ())
            or getattr(getattr(client, "_message_box", None), "possible_gaps", ())
        )
    )
    ready = asyncio.Event()
    client._vanguard_ingest_ready = ready
    if not hasattr(client, "add_event_handler"):
        ready.set()  # Offline session test doubles.
    preprocess, dispatch = client._preprocess_updates, client._dispatch_update
    connect = getattr(client, "connect", None)

    async def reconnect_from_committed_cursor(*args, **kwargs):
        if not client.is_connected():
            session.pending_updates = 0
            session._pending_states.clear()
            if session.journal is not None:
                session.journal.recover()
        return await connect(*args, **kwargs)

    if connect is not None:
        client.connect = reconnect_from_committed_cursor

    async def ordered_batch(updates, users, chats):
        session.pending_updates += len(updates)
        result = await preprocess(updates, users, chats)
        session.pending_updates += len(result) - len(updates)
        if session.pending_updates == 0:
            # Empty Difference/Slice batches advance state too; do not replay
            # them forever after a paced wait disconnects the transport.
            if session.journal is not None:
                session.journal.ordered_complete = True
            await client._save_states_and_entities()
            session.save()
        return result

    async def dispatch_and_checkpoint(update):
        # connect() starts SDK tasks before the worker attaches callbacks.
        # Restored catch-up must not acknowledge events into an empty handler set.
        await ready.wait()
        await dispatch(update)
        session.pending_updates = max(0, session.pending_updates - 1)
        if session.pending_updates == 0:
            if session.journal is not None:
                session.journal.ordered_complete = True
            await client._save_states_and_entities()
            session.save()

    client._preprocess_updates = ordered_batch
    client._dispatch_update = dispatch_and_checkpoint
    client._vanguard_durable_checkpoint = True
