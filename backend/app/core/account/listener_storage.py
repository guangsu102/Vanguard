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
            "AND name IN ('vanguard_raw_updates','update_state')"
        )
    }
    states, oldest = {}, None
    if "vanguard_raw_updates" in tables:
        states = dict(
            cursor.execute(
                "SELECT state,count(*) FROM vanguard_raw_updates GROUP BY state"
            ).fetchall()
        )
        oldest = cursor.execute(
            "SELECT min(received_at) FROM vanguard_raw_updates WHERE state='pending'"
        ).fetchone()[0]
    checkpoints = (
        list(cursor.execute("SELECT id,pts,qts,date,seq FROM update_state ORDER BY id"))
        if "update_state" in tables
        else []
    )
    latest = max((row[3] or 0 for row in checkpoints), default=0)
    return {
        "available": True,
        "states": states,
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
