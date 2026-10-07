"""Distributed ownership lease for Telegram account sessions.

Telegram auth keys are stateful: using one account session from two clients at
the same time causes ``wrong session ID`` replies and invalidates otherwise
healthy work.  The account pool should acquire this lease before creating a
client and renew it while the client is connected.  Release is owner checked
so an expired lease can never be deleted by a stale worker.

The class intentionally depends only on the small async Redis interface used
by redis-py/aioredis.  This keeps it easy to unit test with a fake Redis.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import uuid
from dataclasses import dataclass, field
from typing import Any

# Identity of this process for the session-owner registry.  Work lanes compare
# it against the registry entry to decide whether to execute a stage locally
# or forward it to the session owner (see ``session_exec``).
PROCESS_ID = f"{uuid.uuid4().hex}"

OWNER_PREFIX = "vanguard:telegram:owner:"


_RENEW_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""

# Renew the lease and, only while it is still ours, refresh the owner marker
# with the same TTL.  A stale marker can never outlive its lease.
_RENEW_OWNER_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  redis.call('expire', KEYS[1], ARGV[2])
  redis.call('set', KEYS[2], ARGV[3], 'EX', ARGV[2])
  return 1
end
return 0
"""

_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  redis.call('del', KEYS[1])
  redis.call('del', KEYS[2])
  return 1
end
return 0
"""


@dataclass(slots=True)
class SessionLease:
    """A renewable, owner checked lease for one account session."""

    redis: Any
    account_id: int | str
    ttl_seconds: int = 90
    namespace: str = "vanguard:telegram:lease"
    token: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    _renew_task: asyncio.Task[None] | None = field(default=None, init=False)
    _lost: asyncio.Event = field(default_factory=asyncio.Event, init=False)

    @property
    def key(self) -> str:
        return f"{self.namespace}:{self.account_id}"

    @property
    def owner_key(self) -> str:
        return f"{OWNER_PREFIX}{self.account_id}"

    @property
    def lost(self) -> asyncio.Event:
        """Set when Redis no longer confirms ownership."""

        return self._lost

    async def acquire(self) -> bool:
        # Rotate the owner token for every new generation.  A stale task from
        # a prior connection must never be able to act as the new owner.
        self.token = secrets.token_urlsafe(24)
        ok = await self.redis.set(
            self.key,
            self.token,
            nx=True,
            ex=max(10, int(self.ttl_seconds)),
        )
        if not ok:
            return False
        await self.redis.set(
            self.owner_key,
            PROCESS_ID,
            ex=max(10, int(self.ttl_seconds)),
        )
        self._lost.clear()
        return True

    async def renew(self) -> bool:
        result = await self.redis.eval(
            _RENEW_OWNER_LUA,
            2,
            self.key,
            self.owner_key,
            self.token,
            max(10, int(self.ttl_seconds)),
            PROCESS_ID,
        )
        renewed = bool(result)
        if not renewed:
            self._lost.set()
        return renewed

    async def release(self) -> bool:
        self.stop_renewal()
        result = await self.redis.eval(
            _RELEASE_LUA,
            2,
            self.key,
            self.owner_key,
            self.token,
            PROCESS_ID,
        )
        self._lost.set()
        return bool(result)

    def start_renewal(self, *, interval_seconds: float | None = None) -> None:
        """Start one renewal loop; repeated calls are idempotent."""

        if self._renew_task is not None and not self._renew_task.done():
            return
        interval = interval_seconds or max(1.0, self.ttl_seconds / 3)
        self._renew_task = asyncio.create_task(self._renew_loop(interval))

    def stop_renewal(self) -> None:
        task = self._renew_task
        self._renew_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _renew_loop(self, interval_seconds: float) -> None:
        try:
            while not self._lost.is_set():
                await asyncio.sleep(interval_seconds)
                try:
                    if not await self.renew():
                        return
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A transient Redis failure must not leave a stale worker
                    # believing it still owns the session.
                    self._lost.set()
                    return
        except asyncio.CancelledError:
            return

    async def close(self) -> None:
        """Stop renewal and release the lease, ignoring shutdown races."""

        self.stop_renewal()
        with contextlib.suppress(Exception):
            await self.release()

