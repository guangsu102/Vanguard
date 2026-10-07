"""Guard Telegram RPC execution for one account.

This module is deliberately independent of ``AccountPool`` internals.  The
pool or RPC governor can wrap its single Telegram call path with
``AccountSessionGuard.execute``.  A wrong-session error invalidates the local
client and lease and raises ``SessionQuarantined``; the guard never retries the
call because the original outcome may already have reached Telegram.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from .session_gate import AccountSessionGate
from .session_lease import SessionLease

T = TypeVar("T")

_PROCESS_SESSION_GATE = AccountSessionGate()

_WRONG_SESSION_MARKERS = (
    "wrong session id",
    "wrong session_id",
    "auth key duplicated",
    "auth key unregistered",
    "session revoked",
)


class SessionQuarantined(RuntimeError):
    """Raised after a wrong-session failure; caller must not replay the RPC."""


def is_wrong_session_error(error: BaseException) -> bool:
    text = str(error).lower()
    return any(marker in text for marker in _WRONG_SESSION_MARKERS)


class AccountSessionGuard:
    """Lease + process-local serialization for one Telegram account."""

    def __init__(
        self,
        *,
        redis: Any,
        account_id: int | str,
        client: Any | None = None,
        gate: AccountSessionGate | None = None,
        on_quarantine: Callable[[int | str, BaseException], Awaitable[None] | None]
        | None = None,
    ) -> None:
        self.account_id = account_id
        self.client = client
        self.lease = SessionLease(redis, account_id)
        self.gate = gate or _PROCESS_SESSION_GATE
        self._on_quarantine = on_quarantine
        self._quarantined = False
        self._failure_lock = asyncio.Lock()

    @property
    def quarantined(self) -> bool:
        return self._quarantined

    async def acquire(self) -> bool:
        if self._quarantined:
            return False
        acquired = await self.lease.acquire()
        if acquired:
            self.lease.start_renewal()
        return acquired

    async def execute(self, operation: Callable[[], Awaitable[T]]) -> T:
        """Execute exactly once under the account gate.

        A failed call is surfaced to the durable operation ledger.  This
        wrapper intentionally does not retry, reconnect, or replay any RPC.
        """

        if self._quarantined:
            raise SessionQuarantined(f"account {self.account_id} is quarantined")
        if self.lease.lost.is_set():
            raise SessionQuarantined(f"lease lost for account {self.account_id}")
        async with self.gate.operation(self.account_id):
            try:
                return await operation()
            except Exception as error:
                if not is_wrong_session_error(error):
                    raise
                await self._quarantine(error)
                raise SessionQuarantined(
                    f"wrong Telegram session for account {self.account_id}; replay forbidden"
                ) from error

    async def _quarantine(self, error: BaseException) -> None:
        async with self._failure_lock:
            if self._quarantined:
                return
            self._quarantined = True
            self.lease.stop_renewal()
            if self.client is not None:
                disconnect = getattr(self.client, "disconnect", None)
                if disconnect is not None:
                    with contextlib.suppress(Exception):
                        result = disconnect()
                        if asyncio.iscoroutine(result):
                            await result
            with contextlib.suppress(Exception):
                await self.lease.release()
            if self._on_quarantine is not None:
                result = self._on_quarantine(self.account_id, error)
                if asyncio.iscoroutine(result):
                    await result

    async def close(self) -> None:
        if self.client is not None:
            disconnect = getattr(self.client, "disconnect", None)
            if disconnect is not None:
                with contextlib.suppress(Exception):
                    result = disconnect()
                    if asyncio.iscoroutine(result):
                        await result
        with contextlib.suppress(Exception):
            await self.lease.close()

