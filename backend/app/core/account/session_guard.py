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
    "wrong session",
    "auth key duplicated",
    "auth_key_duplicated",
    "auth key unregistered",
    "session revoked",
    "authorization key (session file) was used under two different ip",
    "authorization key was used under two different ip",
    "key is not registered in the system",
    "authorization has been invalidated",
)

_WRONG_SESSION_EXCEPTION_NAMES = frozenset(
    {
        "AuthKeyDuplicatedError",
        "AuthKeyNotFound",
        "AuthKeyUnregisteredError",
        "SessionRevokedError",
    }
)


class SessionQuarantined(RuntimeError):
    """Raised after a wrong-session failure; caller must not replay the RPC."""


def is_wrong_session_error(error: BaseException) -> bool:
    """Return whether Telegram has invalidated or split this auth session.

    Telethon exposes these failures through several different paths.  RPC
    failures are typed errors, while the update receiver reports a plain
    ``SecurityError`` whose message contains ``wrong session ID``.  Inspect
    both the exception class and its causal chain so every path reaches the
    same quarantine gate without relying on a retry.
    """

    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current).__name__ in _WRONG_SESSION_EXCEPTION_NAMES:
            return True
        text = str(current).lower()
        if any(marker in text for marker in _WRONG_SESSION_MARKERS):
            return True
        current = current.__cause__ or current.__context__
    return False


def install_session_failure_hook(client: Any) -> None:
    """Quarantine a client when Telethon's update receiver sees a bad session.

    ``MTProtoSender._recv_loop`` catches ``SecurityError`` from
    ``decrypt_message_data`` and keeps receiving, so an ordinary RPC wrapper
    cannot observe the failure.  Wrapping the state decoder lets us schedule
    the existing guard immediately while preserving Telethon's original
    exception handling and avoiding a second receive loop.

    The hook is intentionally best-effort for lightweight test clients and
    older Telethon versions that do not expose the private sender state.
    """

    sender = getattr(client, "_sender", None)
    state = getattr(sender, "_state", None)
    decoder = getattr(state, "decrypt_message_data", None)
    if state is None or decoder is None or getattr(state, "_vanguard_session_failure_hook", False):
        return

    def guarded_decoder(body: Any) -> Any:
        try:
            return decoder(body)
        except BaseException as error:
            if not is_wrong_session_error(error):
                raise
            failure = error
            client._vanguard_session_quarantined = True
            task = getattr(client, "_vanguard_session_failure_task", None)
            if task is None or task.done():
                guard = getattr(client, "_vanguard_session_guard", None)

                async def quarantine() -> None:
                    if guard is not None:
                        await guard._quarantine(failure)
                        return
                    disconnect = getattr(client, "disconnect", None)
                    if disconnect is not None:
                        result = disconnect()
                        if asyncio.iscoroutine(result):
                            await result

                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    # The decoder normally runs in Telethon's receive task. If
                    # a caller invokes it outside an event loop, preserve the
                    # original error and let the owning lifecycle close it.
                    task = None
                else:
                    task = loop.create_task(quarantine(), name="telegram-session-quarantine")
                if task is not None:
                    client._vanguard_session_failure_task = task
            raise

    state.decrypt_message_data = guarded_decoder
    state._vanguard_session_failure_hook = True


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
            await self.assert_owned()
            try:
                # A lease can expire while waiting for the process-local gate.
                # Re-check ownership immediately before the provider call so a
                # stale generation cannot issue an RPC after a replacement
                # session has acquired the auth key.
                await self.assert_owned()
                return await operation()
            except Exception as error:
                if not is_wrong_session_error(error):
                    raise
                await self._quarantine(error)
                raise SessionQuarantined(
                    f"wrong Telegram session for account {self.account_id}; replay forbidden"
                ) from error

    async def assert_owned(self, *, expected_token: str | None = None) -> str:
        """Fence this generation against the distributed session lease.

        ``lease.lost`` is only a local renewal signal; the Redis compare is
        required at the actual provider boundary because another process may
        have replaced the lease between renewal ticks.  A mismatch quarantines
        the client and raises without retrying the operation.
        """
        if self._quarantined or self.lease.lost.is_set():
            raise SessionQuarantined(f"lease lost for account {self.account_id}")
        token = str(self.lease.token or "")
        if not token or (expected_token is not None and token != str(expected_token)):
            raise SessionQuarantined(f"session generation mismatch for account {self.account_id}")
        try:
            current = await self.lease.redis.get(self.lease.key)
            owner = await self.lease.redis.get(self.lease.owner_key)
        except Exception as exc:
            # Treat an unavailable ownership check as unsafe. The caller can
            # enter reconciliation rather than issuing an unfenced RPC.
            raise SessionQuarantined(
                f"session lease ownership unavailable for account {self.account_id}"
            ) from exc
        if isinstance(current, bytes):
            current = current.decode("utf-8", "replace")
        if isinstance(owner, bytes):
            owner = owner.decode("utf-8", "replace")
        from .session_lease import PROCESS_ID

        if current != token or owner != PROCESS_ID:
            await self._quarantine(RuntimeError("session lease generation replaced"))
            raise SessionQuarantined(f"session generation fenced for account {self.account_id}")
        return token

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

