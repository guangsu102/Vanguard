"""Task-local durable join-attempt checkpoints at the governed SDK boundary."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime

import structlog

Checkpoint = Callable[[datetime], Awaitable[None]]
JOIN_METHODS = {"JoinChannelRequest", "ImportChatInviteRequest"}
_pending: set[asyncio.Task] = set()
logger = structlog.get_logger()


@dataclass(frozen=True)
class JoinExecutionObserver:
    on_sdk_entered: Checkpoint
    on_platform_responded: Checkpoint


_observer: ContextVar[JoinExecutionObserver | None] = ContextVar("join_execution_observer", default=None)


@contextmanager
def observe(*, on_sdk_entered: Checkpoint, on_platform_responded: Checkpoint) -> Iterator[None]:
    """Bind callbacks only to this operation; independent account tasks stay isolated."""
    token = _observer.set(JoinExecutionObserver(on_sdk_entered, on_platform_responded))
    try:
        yield
    finally:
        _observer.reset(token)


def observer_for(methods: list[str]) -> JoinExecutionObserver | None:
    if any(method.rsplit(".", 1)[-1] in JOIN_METHODS for method in methods):
        return _observer.get()
    return None


async def persist(observer: JoinExecutionObserver | None, entered_at: datetime | None,
                  responded_at: datetime | None) -> None:
    """Persist facts after SDK entry; a local await can never predeclare entry.

    Keep a strong reference when a second caller cancellation interrupts the
    shield. A process crash may leave NULL/unknown, never a false SDK count.
    """
    if observer is None or entered_at is None:
        return

    async def write() -> None:
        for callback, at in ((observer.on_sdk_entered, entered_at),
                             (observer.on_platform_responded, responded_at)):
            if at is not None:
                try:
                    await callback(at)
                except Exception as exc:
                    logger.warning("join_execution_checkpoint_failed", error_type=type(exc).__name__)

    task = asyncio.create_task(write())
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    await asyncio.shield(task)
