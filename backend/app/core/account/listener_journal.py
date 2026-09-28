"""Durable raw update queue sharing the listener's SQLite checkpoint transaction.

Telethon calls put_nowait from its receiver, so the small append must commit
synchronously before the envelope becomes visible to its consumer. Message
bodies stay in the private session volume, never logs or runtime snapshots.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from telethon.extensions import BinaryReader
from telethon.sessions import SQLiteSession


class JournalQueue(asyncio.Queue):
    def __init__(self, session: Any) -> None:
        super().__init__()
        self.session = session
        self.observed: set[int] = set()
        self.ordered_complete = False
        self.queued: set[int] = set()
        self.replayed = 0
        self.failure: str | None = None
        cursor = session._cursor()
        try:
            cursor.execute("""CREATE TABLE IF NOT EXISTS vanguard_raw_updates (
                id INTEGER PRIMARY KEY AUTOINCREMENT, received_at REAL NOT NULL,
                payload BLOB NOT NULL, self_outgoing INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'pending')""")
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS vanguard_raw_state ON vanguard_raw_updates(state,id)"
            )
            SQLiteSession.save(session)
        finally:
            cursor.close()
        self.recover()

    def recover(self) -> None:
        # Called only while disconnected, before the SDK update consumer starts.
        cursor = self.session._cursor()
        try:
            rows = cursor.execute(
                "SELECT id,payload,self_outgoing FROM vanguard_raw_updates WHERE state='pending' ORDER BY id"
            ).fetchall()
        finally:
            cursor.close()
        self._queue.clear()
        self._unfinished_tasks = 0
        self._finished.set()
        self.observed.clear()
        self.ordered_complete = False
        self.queued.clear()
        for identity, payload, outgoing in rows:
            with BinaryReader(payload) as reader:
                item = reader.tgread_object()
            if outgoing:
                item._self_outgoing = True
            super().put_nowait((identity, item))
            self.queued.add(identity)
        self.replayed += len(rows)

    def put_nowait(self, item: Any) -> None:
        # No cursor is advanced by this append. If storage fails, fail closed.
        cursor = self.session._cursor()
        try:
            cursor.execute(
                "INSERT INTO vanguard_raw_updates(received_at,payload,self_outgoing) VALUES(?,?,?)",
                (time.time(), bytes(item), int(bool(getattr(item, "_self_outgoing", False)))),
            )
            identity = cursor.lastrowid
            SQLiteSession.save(self.session)
        except Exception as exc:
            self.failure = type(exc).__name__
            raise
        finally:
            cursor.close()
        super().put_nowait((identity, item))
        self.queued.add(identity)

    def get_nowait(self) -> Any:
        identity, item = super().get_nowait()
        self.queued.discard(identity)
        self.observed.add(identity)
        self.ordered_complete = False
        return item

    def checkpoint(self, *, safe: bool) -> None:
        # Executed inside ListenerSession.save before the same SQLite commit.
        # Gaps keep raw envelopes pending until SDK ordering is complete.
        if not safe or not self.ordered_complete or not self.observed:
            return
        cursor = self.session._cursor()
        try:
            cursor.executemany(
                "UPDATE vanguard_raw_updates SET state='checkpointed' WHERE id=? AND state='pending'",
                [(identity,) for identity in self.observed],
            )
            # Only acknowledged rows age out; unresolved raw evidence is retained.
            cursor.execute(
                "DELETE FROM vanguard_raw_updates WHERE state='checkpointed' AND received_at<?",
                (time.time() - 7 * 86400,),
            )
        finally:
            cursor.close()
        self.observed.clear()

    def reconciliation_required(self) -> None:
        cursor = self.session._cursor()
        try:
            # Only envelopes already consumed into the SDK gap are affected.
            # Still-queued arrivals must remain replayable after a restart.
            cursor.executemany(
                "UPDATE vanguard_raw_updates SET state='reconciliation_required' WHERE id=? AND state='pending'",
                [(identity,) for identity in self.observed],
            )
            SQLiteSession.save(self.session)
        finally:
            cursor.close()
        self.observed.clear()

    def snapshot(self) -> dict[str, Any]:
        from app.core.account.listener_storage import storage_metadata

        cursor = self.session._cursor()
        try:
            result = storage_metadata(cursor)
        finally:
            cursor.close()
        return {
            **result,
            "source": "live",
            "replayed_envelopes": self.replayed,
            "storage_error": self.failure,
        }


def install_journal(client: Any, session: Any) -> JournalQueue | None:
    if not hasattr(client, "_sender") or not hasattr(client, "_updates_queue"):
        return None  # Offline minimal test doubles.
    old = client._updates_queue
    assert not client.is_connected(), "journal_must_install_before_connect"
    assert old.qsize() == 0, "preexisting_queue_requires_explicit_import"
    assert client._sender._updates_queue is old, "sdk_queue_contract_changed"
    queue = JournalQueue(session)
    client._updates_queue = client._sender._updates_queue = queue
    return queue
