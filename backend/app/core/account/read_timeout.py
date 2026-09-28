"""Bound operation time while excluding local pacing from its network deadline."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar

_network_timeout: ContextVar[asyncio.Timeout | None] = ContextVar('read_network_timeout', default=None)


@asynccontextmanager
async def read_operation_timeout(network_seconds: float = 20, total_seconds: float = 90) -> AsyncIterator[None]:
    async with asyncio.timeout(total_seconds):
        async with asyncio.timeout(network_seconds) as deadline:
            token = _network_timeout.set(deadline)
            try:
                yield
            finally:
                _network_timeout.reset(token)


async def paced_sleep(seconds: float) -> None:
    deadline = _network_timeout.get()
    due = deadline.when() if deadline is not None else None
    if deadline is None or deadline.expired() or due is None:
        await asyncio.sleep(seconds)
        return
    loop = asyncio.get_running_loop()
    started = loop.time()
    if due <= started:
        raise TimeoutError("read operation network deadline exceeded")
    # The outer deadline still bounds the entire operation. Exclude actual
    # scheduler wait, including event-loop scheduling delay, from network time.
    deadline.reschedule(None)
    try:
        await asyncio.sleep(seconds)
    finally:
        if not deadline.expired():
            deadline.reschedule(due + loop.time() - started)
