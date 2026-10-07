"""Bounded connection reuse for serial survival reads, under the account pool lease."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any


class SurvivalReadBatch:
    """Keep one leased client; each RPC still passes through its normal governor.

    Rotate after five records or 60 seconds, at the next record boundary. Each
    inspection has its own 60-second timeout. No permissions or message facts are
    cached, and no lease/client escapes the caller's batch.
    """

    def __init__(self, pool: Any, *, raise_on_lease_failure: bool = True) -> None:
        self.pool = pool
        self.raise_on_lease_failure = raise_on_lease_failure
        self.wrapper: Any = None
        self.key: tuple[int, str] | None = None
        self.started_at = 0.0
        self.records = 0

    async def close(self) -> None:
        wrapper, self.wrapper = self.wrapper, None
        self.key = None
        self.records = 0
        if wrapper is not None:
            await self.pool.release(wrapper)

    @asynccontextmanager
    async def borrow(self, account_id: int, purpose: str) -> AsyncIterator[Any]:
        key = (account_id, purpose)
        try:
            if self.wrapper is not None:
                client = getattr(self.wrapper, "client", None)
                if (
                    self.key != key
                    or self.records >= 5
                    or time.monotonic() - self.started_at >= 60
                    or getattr(self.wrapper, "operation_lease_lost", False)
                    or client is None
                    or not client.is_connected()
                ):
                    await self.close()
            if self.wrapper is None:
                options = {"raise_on_lease_failure": True} if self.raise_on_lease_failure else {}
                self.wrapper = await self.pool.acquire_by_id(account_id, purpose=purpose, **options)
                if self.wrapper is None:
                    raise RuntimeError("account_unavailable")
                self.key = key
                self.started_at = time.monotonic()
            self.records += 1
            yield self.wrapper
        except BaseException:
            # Budget deferral, read failure and cancellation all drop the lease.
            await self.close()
            raise
