"""Pause update transports at a read boundary without discarding their session.

The saved cursor and queued updates survive budget pauses in the same process.
This is not a durable event inbox or an exactly-once delivery guarantee.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

from app.core.account.rpc_governor import RpcDeferred

logger = structlog.get_logger()


class ListenerPause:
    def __init__(self, client: Any, pause: Callable[[], Awaitable[None]]) -> None:
        self.client = client
        self.pause = pause
        self.task: asyncio.Task[None] | None = None
        self.reason: str | None = None
        self.resume_at: datetime | None = None
        self.started: float | None = None
        self.backlog_since: float | None = None
        self.error: str | None = None

    def request(self, exc: RpcDeferred) -> asyncio.Task[None]:
        self.reason = exc.reason
        self.resume_at = max(
            self.resume_at or datetime.min,
            datetime.utcnow() + timedelta(seconds=exc.retry_after_seconds + 1),
        )
        if self.started is None:
            self.started = time.monotonic()
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._pause())
        return self.task

    async def _pause(self) -> None:
        try:
            await self.pause()
            self.error = None
        except Exception as exc:
            self.error = type(exc).__name__
            logger.error("telegram_listener_pause_failed", error_type=self.error)

    def check_resume(self) -> None:
        if self.task is not None and not self.task.done():
            raise RpcDeferred("telegram_read_budget", 5)
        if self.resume_at and self.resume_at > datetime.utcnow():
            raise RpcDeferred(
                self.reason or "telegram_read_budget",
                max(1, int((self.resume_at - datetime.utcnow()).total_seconds()) + 1),
            )

    def resumed(self) -> None:
        self.reason = self.resume_at = self.started = self.error = None

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        queue = self.client._updates_queue
        size = queue.qsize()
        self.backlog_since = (self.backlog_since or now) if size else None
        waiting = getattr(self.client, "_vanguard_sync_wait", None) or {}
        started = waiting.get("started", self.started)
        return {
            "raw_journal": self.client.session.journal.snapshot() if getattr(getattr(self.client, "session", None), "journal", None) is not None else None,
            "sync_wait_scope": waiting.get("scope", "channel") if waiting else None,
            "sync_wait_reason": waiting.get("reason"),
            "sync_resume_at": waiting.get("resume_at"),
            "update_queue_size": size,
            # Observed nonempty duration, not the age of an individual message.
            "queue_nonempty_seconds": round(now - self.backlog_since, 1)
            if self.backlog_since is not None
            else 0,
            "update_handler_tasks": len(self.client._event_handler_tasks),
            "read_wait_seconds": round(now - started, 1) if started is not None else 0,
            "pause_error": self.error,
            "checkpoint_scope": "durable_session_and_inbox"
            if getattr(self.client, "_vanguard_durable_checkpoint", False) else "process_session",
        }


def process_memory() -> dict[str, int | None]:
    """Current Linux RSS, not ru_maxrss's lifetime peak; unavailable elsewhere."""
    try:
        rows = Path("/proc/self/status").read_text().splitlines()
        rss = next(int(row.split()[1]) * 1024 for row in rows if row.startswith("VmRSS:"))
    except (OSError, ValueError, StopIteration):
        rss = None
    return {"rss_bytes": rss}
