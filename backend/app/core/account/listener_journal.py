"""Durable raw update queue sharing the listener's SQLite checkpoint transaction.

Telethon calls put_nowait from its receiver, so the small append must commit
synchronously before the envelope becomes visible to its consumer. Message
bodies stay in the private session volume, never logs or runtime snapshots.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from telethon.extensions import BinaryReader
from telethon.sessions import SQLiteSession
from telethon.tl import types


@dataclass
class ProtocolEnvelope:
    item: Any
    active: bool = True


class JournalQueue(asyncio.Queue):
    PAGE_SIZE = 1000

    def __init__(self, session: Any) -> None:
        super().__init__()
        self.session = session
        self.observed: dict[int, dict[str, int]] = {}
        self.message_box = lambda: None
        self.policy = lambda: None
        self.ordered_complete = False
        self.queued: set[int] = set()
        self.replayed = 0
        self.failure: str | None = None
        self.reconciled_scan_id = 0
        self.last_loaded_id = 0
        self.protocol_tail: dict[int, ProtocolEnvelope] = {}
        self.protocol_recovery: set[int] = set()
        self.hold_global_protocol = False
        self.protocol_discarded = 0
        self.projected_records = 0
        cursor = session._cursor()
        try:
            cursor.execute("""CREATE TABLE IF NOT EXISTS vanguard_raw_updates (
                id INTEGER PRIMARY KEY AUTOINCREMENT, received_at REAL NOT NULL,
                payload BLOB NOT NULL, self_outgoing INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'pending')""")
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS vanguard_raw_state ON vanguard_raw_updates(state,id)"
            )
            columns = {row[1] for row in cursor.execute("PRAGMA table_info(vanguard_raw_updates)")}
            for name, kind in {"discarded": "TEXT NOT NULL DEFAULT '[]'", "dependencies": "TEXT",
                               "consumed": "INTEGER NOT NULL DEFAULT 0", "compact_channel": "INTEGER",
                               "compact_pts": "INTEGER", "compact_count": "INTEGER"}.items():
                if name not in columns:
                    cursor.execute(f"ALTER TABLE vanguard_raw_updates ADD COLUMN {name} {kind}")
            cursor.execute("CREATE INDEX IF NOT EXISTS vanguard_raw_compact ON vanguard_raw_updates(compact_channel,id)")
            cursor.execute("CREATE TABLE IF NOT EXISTS vanguard_listener_facts (fact_key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            SQLiteSession.save(session)
        finally:
            cursor.close()
        self.recover()

    def recover(self) -> None:
        # Called only while disconnected, before the SDK update consumer starts.
        self._queue.clear()
        self._unfinished_tasks = 0
        self._finished.set()
        self.observed.clear()
        self.ordered_complete = False
        self.queued.clear()
        self.last_loaded_id = 0
        self.protocol_tail.clear()
        self.protocol_recovery.clear()
        self.hold_global_protocol = False
        self.session._cursor().execute("UPDATE vanguard_raw_updates SET consumed=0 WHERE state='pending'")
        self._fill_page()

    def _fill_page(self) -> None:
        room = self.PAGE_SIZE - self.qsize()
        if room <= 0:
            return
        cursor = self.session._cursor()
        try:
            rows = cursor.execute(
                "SELECT id FROM vanguard_raw_updates "
                "WHERE state='pending' AND id>? ORDER BY id LIMIT ?",
                (self.last_loaded_id, room),
            ).fetchall()
        finally:
            cursor.close()
        for (identity,) in rows:
            super().put_nowait(identity)
            self.queued.add(identity)
            self.last_loaded_id = identity
        self.replayed += len(rows)

    def put_nowait(self, item: Any) -> None:
        # No cursor is advanced by this append. If storage fails, fail closed.
        cursor = self.session._cursor()
        try:
            from app.core.account.listener_protocol import compact, dependencies, updates_in
            from app.core.account.listener_interest import fresh, project
            policy = self.policy()
            if fresh(policy):
                item, discarded, facts = project(item, policy)
                self._store_facts(cursor, facts)
                if len(discarded) == len(updates_in(item)):
                    if facts:
                        SQLiteSession.save(self.session)
                    self._put_protocol(item)
                    return
            else:
                item, discarded = compact(item, policy)
            deps = dependencies(item)
            channel, pts, count = None, None, None
            updates = updates_in(item)
            if discarded == [0] and len(updates) == 1 and not getattr(item, 'seq', 0):
                channel = updates[0].message.peer_id.channel_id
                pts, count = updates[0].pts, updates[0].pts_count
                previous = cursor.execute(
                    "SELECT id,compact_pts,compact_count,consumed,state FROM vanguard_raw_updates "
                    "WHERE compact_channel=? ORDER BY id DESC LIMIT 1", (channel,)
                ).fetchone()
                # Only coalesce an unconsumed, contiguous sequence of irrelevant
                # updates. Queue entries hold IDs, so the reader sees this write.
                if previous and previous[3] == 0 and previous[4] == 'pending' and previous[1] == pts-count:
                    updates[0].pts_count += previous[2]
                    cursor.execute("UPDATE vanguard_raw_updates SET payload=?,dependencies=?,compact_pts=?,compact_count=? WHERE id=?",
                                   (bytes(item), json.dumps(deps), pts, updates[0].pts_count, previous[0]))
                    SQLiteSession.save(self.session)
                    return
            cursor.execute(
                "INSERT INTO vanguard_raw_updates(received_at,payload,self_outgoing,discarded,dependencies,compact_channel,compact_pts,compact_count) VALUES(?,?,?,?,?,?,?,?)",
                (time.time(), bytes(item), int(bool(getattr(item, "_self_outgoing", False))),
                 json.dumps(discarded), json.dumps(deps), channel, pts, count),
            )
            SQLiteSession.save(self.session)
        except Exception as exc:
            self.failure = type(exc).__name__
            raise
        finally:
            cursor.close()
        if self.qsize() < self.PAGE_SIZE:
            # Load in journal order so live pushes never overtake disk backlog.
            self._fill_page()

    def get_nowait(self) -> Any:
        from app.core.account.listener_protocol import compact, dependencies, updates_in
        identity = super().get_nowait()
        if isinstance(identity, ProtocolEnvelope):
            identity.active = False
            self.ordered_complete = False
            self._fill_page()
            self._fill_protocol_recovery()
            return identity.item
        cursor = self.session._cursor()
        payload, outgoing, discarded_json = cursor.execute(
            "SELECT payload,self_outgoing,discarded FROM vanguard_raw_updates WHERE id=?", (identity,)
        ).fetchone()
        with BinaryReader(payload) as reader:
            item = reader.tgread_object()
        discarded = json.loads(discarded_json)
        from app.core.account.listener_interest import fresh, project
        if fresh(self.policy()):
            item, projected, facts = project(item, self.policy())
            self._store_facts(cursor, facts)
            discarded = sorted(set(discarded) | set(projected))
            for index in discarded:
                updates_in(item)[index]._vanguard_ignore_business = True
            if len(discarded) == len(updates_in(item)):
                cursor.execute('DELETE FROM vanguard_raw_updates WHERE id=?', (identity,))
                SQLiteSession.save(self.session)
                cursor.close()
                self.queued.discard(identity)
                self.observed.pop(identity, None)
                self.projected_records += 1
                self.ordered_complete = False
                self._fill_page()
                self._fill_protocol_recovery()
                return item
        elif not discarded:
            item, discarded = compact(item, self.policy())
        for index in discarded:
            updates_in(item)[index]._vanguard_ignore_business = True
        if outgoing:
            item._self_outgoing = True
        required = dependencies(item)
        cursor.execute("UPDATE vanguard_raw_updates SET consumed=1,payload=?,discarded=?,dependencies=? WHERE id=?",
                       (bytes(item), json.dumps(discarded), json.dumps(required), identity))
        updates = updates_in(item)
        if discarded == [0] and len(updates) == 1 and not getattr(item, 'seq', 0):
            import copy
            update = updates[0]
            channel = update.message.peer_id.channel_id
            prior = cursor.execute(
                "SELECT id,compact_count,received_at FROM vanguard_raw_updates WHERE compact_channel=? "
                "AND compact_pts=? AND consumed=1 AND state='pending' AND id<>? ORDER BY id DESC LIMIT 1",
                (channel, update.pts-update.pts_count, identity),
            ).fetchone()
            count = update.pts_count
            if prior and prior[0] in self.observed:
                # SDK already saw the earlier ordinary updates. Only compact
                # their durable replay representation, never its live input.
                merged = copy.copy(item)
                merged_update = copy.copy(update)
                count += prior[1]
                merged_update.pts_count = count
                if hasattr(merged, 'updates'):
                    merged.updates = [merged_update]
                else:
                    merged.update = merged_update
                cursor.execute("UPDATE vanguard_raw_updates SET payload=?,received_at=? WHERE id=?",
                               (bytes(merged), prior[2], identity))
                cursor.execute("DELETE FROM vanguard_raw_updates WHERE id=?", (prior[0],))
                self.observed.pop(prior[0])
            cursor.execute("UPDATE vanguard_raw_updates SET compact_channel=?,compact_pts=?,compact_count=? WHERE id=?",
                           (channel, update.pts, count, identity))
        cursor.close()
        self.queued.discard(identity)
        self.observed[identity] = required
        self.ordered_complete = False
        if self.qsize() < self.PAGE_SIZE // 2:
            self._fill_page()
        self._fill_protocol_recovery()
        return item

    def _store_facts(self, cursor: Any, facts: list[dict]) -> None:
        for fact in facts:
            previous = cursor.execute('SELECT value FROM vanguard_listener_facts WHERE fact_key=?', (fact['key'],)).fetchone()
            if previous:
                old = json.loads(previous[0])
                if fact['kind'] == 'deletion_candidates':
                    ids = sorted(set(old['ids']) | set(fact['ids']))
                    fact = {**fact, 'ids': ids[-512:], 'overflow': old.get('overflow', False) or fact.get('overflow', False) or len(ids)>512}
                elif old['at'] >= fact['at']:
                    continue
            cursor.execute('INSERT INTO vanguard_listener_facts VALUES(?,?) ON CONFLICT(fact_key) DO UPDATE SET value=excluded.value',
                           (fact['key'], json.dumps(fact, sort_keys=True)))

    def facts(self, limit: int = 100) -> list[tuple[str, str]]:
        return self.session._cursor().execute('SELECT fact_key,value FROM vanguard_listener_facts ORDER BY fact_key LIMIT ?', (limit,)).fetchall()

    def acknowledge_facts(self, rows: list[tuple[str, str]]) -> None:
        # Compare the exact version: a new deletion arriving during PG commit
        # cannot be removed by acknowledgement of an older merged fact.
        self.session._cursor().executemany('DELETE FROM vanguard_listener_facts WHERE fact_key=? AND value=?', rows)
        SQLiteSession.save(self.session)

    def _put_protocol(self, item: Any) -> None:
        from app.core.account.listener_protocol import channel_only, updates_in
        if self.hold_global_protocol and not channel_only(item):
            self._schedule_protocol_recovery(item)
            self.protocol_discarded += 1
            return
        updates = updates_in(item)
        if len(updates) == 1 and isinstance(updates[0], types.UpdateNewChannelMessage) and not getattr(item, 'seq', 0):
            update = updates[0]; channel = update.message.peer_id.channel_id
            previous = self.protocol_tail.get(channel)
            if previous is not None and previous.active:
                prior = updates_in(previous.item)[0]
                if prior.pts == update.pts-update.pts_count:
                    update.pts_count += prior.pts_count
                    previous.item = item
                    self.protocol_discarded += 1
                    return
        if self.qsize() < self.PAGE_SIZE:
            envelope = ProtocolEnvelope(item)
            super().put_nowait(envelope)
            if len(updates) == 1 and isinstance(updates[0], types.UpdateNewChannelMessage) and not getattr(item, 'seq', 0):
                self.protocol_tail[updates[0].message.peer_id.channel_id] = envelope
            if len(self.protocol_tail)>self.PAGE_SIZE:
                self.protocol_tail = {key: value for key,value in self.protocol_tail.items() if value.active}
            return
        # Only already-projected, non-business input reaches this branch. Mark
        # a real recovery need instead of retaining a full chat history. Never
        # manufacture seq/pts progress; the SDK requests its actual difference.
        self._schedule_protocol_recovery(item)
        self.protocol_discarded += 1

    def _schedule_protocol_recovery(self, item: Any) -> None:
        from app.core.account.listener_protocol import dependencies
        for key in dependencies(item):
            channel = int(key) if key.isdigit() else int(key[4:]) if key.startswith('gap:') else 0
            if len(self.protocol_recovery) < self.PAGE_SIZE or channel in self.protocol_recovery:
                self.protocol_recovery.add(channel)
            else:
                self.protocol_recovery.add(0)

    def _fill_protocol_recovery(self) -> None:
        available = self.protocol_recovery - ({0} if self.hold_global_protocol else set())
        while available and self.qsize()<self.PAGE_SIZE:
            channel = min(available)
            available.remove(channel)
            self.protocol_recovery.remove(channel)
            item = (types.UpdateShort(types.UpdateChannelTooLong(channel), datetime.now(UTC))
                    if channel else types.UpdatesTooLong())
            super().put_nowait(ProtocolEnvelope(item))

    def park_global_protocol(self) -> None:
        """Discard only projected protocol input; preserve its real recovery need."""
        from collections import deque
        from app.core.account.listener_protocol import channel_only

        self.hold_global_protocol = True
        kept = deque()
        for entry in self._queue:
            if isinstance(entry, ProtocolEnvelope) and not channel_only(entry.item):
                entry.active = False
                self._schedule_protocol_recovery(entry.item)
                self.protocol_discarded += 1
            else:
                kept.append(entry)
        self._queue = kept
        self._fill_page()
        self._fill_protocol_recovery()

    def get_channel_nowait(self) -> Any:
        """Take an independent channel envelope without consuming account input."""
        from app.core.account.listener_protocol import channel_only

        cursor = self.session._cursor()
        try:
            for index, entry in enumerate(self._queue):
                if isinstance(entry, ProtocolEnvelope):
                    item = entry.item
                else:
                    payload = cursor.execute(
                        'SELECT payload FROM vanguard_raw_updates WHERE id=?', (entry,),
                    ).fetchone()[0]
                    with BinaryReader(payload) as reader:
                        item = reader.tgread_object()
                if channel_only(item):
                    del self._queue[index]
                    self._queue.appendleft(entry)
                    return self.get_nowait()
        finally:
            cursor.close()
        raise asyncio.QueueEmpty

    def reconcile_batch(self, limit: int = 500) -> int:
        from app.core.account.listener_interest import fresh, project
        from app.core.account.listener_protocol import updates_in
        policy = self.policy()
        if not fresh(policy):
            return 0
        cursor = self.session._cursor()
        try:
            rows = cursor.execute("SELECT id,payload,discarded,state,consumed FROM vanguard_raw_updates WHERE state IN ('pending','reconciliation_required') AND id>? ORDER BY id LIMIT ?",
                                  (self.reconciled_scan_id,limit)).fetchall()
            replacements = {}
            for identity, payload, old_discarded, state, consumed in rows:
                with BinaryReader(payload) as reader:
                    item = reader.tgread_object()
                item, ignored, facts = project(item, policy)
                self._store_facts(cursor, facts)
                ignored = sorted(set(ignored) | set(json.loads(old_discarded)))
                if len(ignored) == len(updates_in(item)):
                    cursor.execute('DELETE FROM vanguard_raw_updates WHERE id=?', (identity,))
                    # The SDK may be waiting on a read budget. Project pending
                    # disk entries independently so that waiting never retains
                    # group history. Preserve queued protocol order; unqueued
                    # gaps are recovered by the SDK, without fabricating pts.
                    if identity in self.queued:
                        for index in ignored:
                            updates_in(item)[index]._vanguard_ignore_business = True
                        replacements[identity] = ProtocolEnvelope(item)
                        self.queued.discard(identity)
                    elif state == 'pending' and not consumed:
                        self._schedule_protocol_recovery(item)
                    self.observed.pop(identity, None)
                    self.projected_records += 1
                elif ignored:
                    cursor.execute('UPDATE vanguard_raw_updates SET payload=?,discarded=? WHERE id=?', (bytes(item),json.dumps(ignored),identity))
                self.reconciled_scan_id = identity
            if replacements:
                from collections import deque
                self._queue = deque(replacements.get(value, value) if isinstance(value, int) else value for value in self._queue)
            # Completed projection metadata is not an unresolved business queue.
            cursor.execute("DELETE FROM vanguard_raw_updates WHERE id IN (SELECT id FROM vanguard_raw_updates WHERE state IN ('discarded_irrelevant','reconciled_to_inbox') AND length(payload)=0 LIMIT 1000)")
            if rows or cursor.rowcount:
                SQLiteSession.save(self.session)
            return len(rows)
        finally:
            cursor.close()

    def checkpoint(self, *, safe: bool) -> None:
        # Executed inside ListenerSession.save before the same SQLite commit.
        # Gaps keep raw envelopes pending until SDK ordering is complete.
        if not self.ordered_complete or not self.observed:
            return
        from app.core.account.listener_protocol import satisfied
        box = self.message_box()
        confirmed = ([identity for identity, required in self.observed.items() if satisfied(required, box)]
                     if box is not None else (list(self.observed) if safe else []))
        if not confirmed:
            return
        cursor = self.session._cursor()
        try:
            cursor.executemany(
                "UPDATE vanguard_raw_updates SET state='checkpointed',payload=X'' WHERE id=? AND state='pending'",
                [(identity,) for identity in confirmed],
            )
            # Committed progress needs row metadata, never a retained chat body.
            # Also release legacy acknowledged payloads in bounded batches.
            cursor.execute("UPDATE vanguard_raw_updates SET payload=X'' WHERE id IN "
                           "(SELECT id FROM vanguard_raw_updates WHERE state='checkpointed' "
                           "AND length(payload)>0 ORDER BY id LIMIT 1000)")
            # Only acknowledged rows age out; unresolved raw evidence is retained.
            cursor.execute(
                "DELETE FROM vanguard_raw_updates WHERE state='checkpointed' AND id NOT IN "
                "(SELECT id FROM vanguard_raw_updates WHERE state='checkpointed' ORDER BY id DESC LIMIT 1000)",
            )
        finally:
            cursor.close()
        for identity in confirmed:
            self.observed.pop(identity, None)

    def reconciliation_required(self, request: Any = None) -> None:
        channel = getattr(getattr(request, 'channel', None), 'channel_id', None)
        keys = {str(channel), 'gap:' + str(channel)} if channel else {'account', 'secret', 'seq', 'global'}
        affected = [identity for identity, required in self.observed.items()
                    if request is None or keys.intersection(required)]
        cursor = self.session._cursor()
        try:
            # Only envelopes already consumed into the SDK gap are affected.
            # Still-queued arrivals must remain replayable after a restart.
            cursor.executemany(
                "UPDATE vanguard_raw_updates SET state='reconciliation_required' WHERE id=? AND state='pending'",
                [(identity,) for identity in affected],
            )
            SQLiteSession.save(self.session)
        finally:
            cursor.close()
        for identity in affected:
            self.observed.pop(identity, None)

    def snapshot(self) -> dict[str, Any]:
        from app.core.account.listener_storage import storage_metadata

        cursor = self.session._cursor()
        try:
            # Old TooLong records are not replayable automatically. Release only
            # entries proven wholly irrelevant under the current loaded policy;
            # preserve every mixed/unknown/critical event for explicit recovery.
            from app.core.account.listener_protocol import compact, updates_in
            policy = self.policy()
            if policy and not policy.get('selective'):
                rows = cursor.execute("SELECT id,payload,discarded FROM vanguard_raw_updates "
                                      "WHERE state='reconciliation_required' AND id>? ORDER BY id LIMIT 100",
                                      (self.reconciled_scan_id,)).fetchall()
                for identity, payload, old_discarded in rows:
                    with BinaryReader(payload) as reader:
                        item = reader.tgread_object()
                    _, discarded = compact(item, policy)
                    discarded = set(discarded) | set(json.loads(old_discarded))
                    if len(discarded) == len(updates_in(item)):
                        cursor.execute("UPDATE vanguard_raw_updates SET state='discarded_irrelevant',payload=X'' WHERE id=?", (identity,))
                    self.reconciled_scan_id = identity
                if rows:
                    SQLiteSession.save(self.session)
            result = storage_metadata(cursor)
        finally:
            cursor.close()
        return {
            **result,
            "source": "live",
            "replayed_envelopes": self.replayed,
            "storage_error": self.failure,
            "awaiting_checkpoint": len(self.observed),
            "queued_for_sdk": self.qsize(),
            "protocol_only_compacted_or_dropped": self.protocol_discarded,
            "projected_raw_records": self.projected_records,
            "protocol_recovery_markers": len(self.protocol_recovery),
            "business_facts_pending": self.session._cursor().execute('SELECT count(*) FROM vanguard_listener_facts').fetchone()[0],
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
