"""Shared idempotency primitives for Telegram reads and qualification checks.

The read/qualification pipelines are retried by several independent schedulers.
This module provides deterministic operation keys and a small async guard that
can be backed by Redis (or any key/value object exposing ``set``/``get``).
Callers must still persist the final result in their domain table; the guard is
only the cross-worker claim and short-lived lock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Protocol


class KeyValueStore(Protocol):
    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> Any: ...

    async def get(self, key: str) -> Any: ...

    async def delete(self, key: str) -> Any: ...

    async def eval(self, script: str, numkeys: int, *keys_and_args: str) -> Any: ...


def operation_key(
    *,
    account_id: int | str,
    operation: str,
    subject_id: int | str,
    version: str | int = 1,
) -> str:
    """Return a stable key for one logical operation.

    ``version`` must change when the input/algorithm changes.  It is therefore
    safe to reuse a completed result while preventing a new algorithm from
    being hidden by an old cache entry.
    """

    raw = f"{account_id}:{operation}:{subject_id}:{version}"
    digest = sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"vanguard:operation:{operation}:{digest}"


@dataclass(frozen=True, slots=True)
class OperationClaim:
    key: str
    token: str
    claimed_at: datetime


class OperationAlreadyRunning(RuntimeError):
    """Another worker currently owns the operation lock."""


def result_is_terminal(status: str | None) -> bool:
    """Return whether a durable result must never be read/reviewed again."""
    return (status or "").lower() in {
        "passed",
        "failed",
        "rejected",
        "excluded",
        "completed",
        "sent",
        "cancelled",
        "terminal_failed",
        "failed_reconciliation",
    }


def result_is_retryable(status: str | None, *, due: bool) -> bool:
    """Only budget/external waits can re-enter the scheduler."""
    return bool(due and (status or "").lower() in {"pending", "deferred_budget", "awaiting_external_approval"})


class OperationGuard:
    """Short-lived cross-worker claim for a logical operation.

    A successful claim is released by the owner in a ``finally`` block.  A
    bounded TTL prevents a dead worker from blocking the operation forever.
    Completed results belong in a durable table and should be checked before
    calling :meth:`claim`; this guard deliberately does not permit a second
    read merely because the lock expired.
    """

    def __init__(self, store: KeyValueStore, *, ttl_seconds: int = 300) -> None:
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be positive")
        self._store = store
        self._ttl = ttl_seconds

    async def claim(self, key: str, token: str) -> OperationClaim | None:
        claimed = await self._store.set(key, token, nx=True, ex=self._ttl)
        if not claimed:
            return None
        return OperationClaim(key=key, token=token, claimed_at=datetime.now(timezone.utc))

    async def release(self, claim: OperationClaim) -> None:
        # Compare-and-delete must be atomic: the key can expire and be claimed
        # by another worker between a GET and DEL.
        evaluator = getattr(self._store, "eval", None)
        if evaluator is not None:
            await evaluator(
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('del', KEYS[1]) else return 0 end",
                1,
                claim.key,
                claim.token,
            )
            return
        # Stores without an atomic primitive cannot safely release a shared
        # lock; leave the short TTL to expire instead of deleting a new owner's
        # lock.

