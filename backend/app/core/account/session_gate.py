"""Process local serialization for operations on one Telegram account.

The Redis session lease prevents multiple workers from owning an auth key.  A
per-account lock is still required inside one worker: event listeners and
business tasks may otherwise issue concurrent RPCs through the same client.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import AsyncIterator


class AccountSessionGate:
    def __init__(self) -> None:
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    @asynccontextmanager
    async def operation(self, account_id: int | str) -> AsyncIterator[None]:
        lock = self._locks[str(account_id)]
        async with lock:
            yield

    def lock(self, account_id: int | str) -> asyncio.Lock:
        """Return the stable lock for integration code needing manual use."""

        return self._locks[str(account_id)]

