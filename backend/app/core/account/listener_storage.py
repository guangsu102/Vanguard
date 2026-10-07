"""Read-only listener journal and checkpoint metadata, including before connect."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def storage_metadata(cursor: Any, *, now: float | None = None) -> dict[str, Any]:
    """Never select payload, auth keys, entities, or session identifiers."""
    now = time.time() if now is None else now
    tables = {
        row[0]
        for row in cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('vanguard_raw_updates','update_state','vanguard_listener_facts')"
        )
    }
    states, oldest, queues = {}, None, {}
    if "vanguard_raw_updates" in tables:
        states = dict(
            cursor.execute(
                "SELECT state,count(*) FROM vanguard_raw_updates GROUP BY state"
            ).fetchall()
        )
        oldest = cursor.execute(
            "SELECT min(received_at) FROM vanguard_raw_updates WHERE state='pending'"
        ).fetchone()[0]
        columns = {row[1] for row in cursor.execute("PRAGMA table_info(vanguard_raw_updates)")}
        if 'consumed' in columns:
            rows = cursor.execute("SELECT state,consumed,count(*),coalesce(sum(length(payload)),0),min(received_at) "
                                  "FROM vanguard_raw_updates WHERE state IN ('pending','reconciliation_required') GROUP BY state,consumed").fetchall()
            for state, consumed, count, size, received in rows:
                label = ('awaiting_checkpoint' if consumed else 'queued_for_sdk') if state == 'pending' else state
                entry = queues.setdefault(label, {"count": 0, "bytes": 0, "oldest_seconds": 0})
                entry['count'] += count
                entry['bytes'] += size
                entry['oldest_seconds'] = max(entry['oldest_seconds'], max(0, int(now-received)))
        else:
            for state,count,size,received in cursor.execute("SELECT state,count(*),sum(length(payload)),min(received_at) FROM vanguard_raw_updates WHERE state IN ('pending','reconciliation_required') GROUP BY state"):
                queues[state] = {'count':count,'bytes':size,'oldest_seconds':max(0,int(now-received))}
    if 'vanguard_listener_facts' in tables:
        count,size,oldest_fact = cursor.execute("SELECT count(*),coalesce(sum(length(value)),0),min(json_extract(value,'$.at')) FROM vanguard_listener_facts").fetchone()
        queues['business_facts'] = {'count':count,'bytes':size,'oldest_seconds':max(0,int(now-oldest_fact)) if oldest_fact else 0}
    checkpoints = (
        list(cursor.execute("SELECT id,pts,qts,date,seq FROM update_state ORDER BY id"))
        if "update_state" in tables
        else []
    )
    latest = max((row[3] or 0 for row in checkpoints), default=0)
    return {
        "available": True,
        "states": states,
        "queues": queues,
        "pressure": any(row['count'] > 1000 or row['bytes'] > 16 * 1024 * 1024
                        or (name == "business_facts" and row['oldest_seconds'] > 120)
                        for name, row in queues.items()),
        "protocol_recovery_pending": any(row['count'] for name, row in queues.items() if name != "business_facts"),
        "oldest_pending_seconds": max(0, int(now - oldest)) if oldest is not None else 0,
        "storage_error": None,
        "checkpoint": {
            "count": len(checkpoints),
            "latest_at": datetime.fromtimestamp(latest, UTC).isoformat() if latest else None,
            "digest": hashlib.sha256(json.dumps(checkpoints).encode()).hexdigest(),
        },
    }


def read_listener_storage(path: Path) -> dict[str, Any]:
    """Open an existing SQLite file in ro mode; never create a missing session."""
    if path.is_symlink():
        return {"available": False, "source": "disk", "storage_error": "session_symlink"}
    if not path.is_file():
        return {"available": False, "source": "disk", "storage_error": None}
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)) as db:
            deadline = time.monotonic() + 2
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            result = storage_metadata(db.cursor())
            db.rollback()
        return {**result, "source": "disk"}
    except (sqlite3.Error, OSError, ValueError, OverflowError) as exc:
        return {"available": False, "source": "disk", "storage_error": type(exc).__name__}


def stored_facts(path: Path, *, acknowledge: list[tuple[str, str]] | None = None) -> list[tuple[str, str]]:
    """Replay only projected business facts; never open an SDK session or move pts.

    Acknowledgement follows the PG commit and compares the exact stored version.
    A concurrent producer's newer fact survives. A missing session is not created.
    """
    if path.is_symlink() or not path.is_file():
        return []
    mode = "rw" if acknowledge is not None else "ro"
    with closing(sqlite3.connect(path.resolve().as_uri()+"?mode="+mode, uri=True, timeout=0.2)) as db:
        deadline = time.monotonic()+2
        db.set_progress_handler(lambda: int(time.monotonic()>deadline), 10000)
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='vanguard_listener_facts'").fetchone():
            return []
        if acknowledge is not None:
            with db:
                db.executemany("DELETE FROM vanguard_listener_facts WHERE fact_key=? AND value=?", acknowledge)
            return []
        return db.execute("SELECT fact_key,value FROM vanguard_listener_facts ORDER BY fact_key LIMIT 100").fetchall()
