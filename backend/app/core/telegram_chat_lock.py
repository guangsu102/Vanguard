"""Serialization primitives for Telegram chat ownership and message writes.

The transaction-scoped helper remains the cheapest option for database-only
ownership changes. The session-level context deliberately uses a dedicated
connection so callers can commit before a Telegram network call without
opening a cross-domain race.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from weakref import WeakValueDictionary

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession

_SESSION_LOCKS_KEY = "vanguard_telegram_chat_transaction_locks"
_SESSION_LISTENER_KEY = "vanguard_telegram_chat_lock_listener"
_fallback_locks: WeakValueDictionary[tuple[int, int], asyncio.Lock] = (
    WeakValueDictionary()
)
_session_lock_keys: ContextVar[frozenset[int]] = ContextVar(
    "vanguard_telegram_chat_session_lock_keys",
    default=frozenset(),
)
_account_lock_keys: ContextVar[frozenset[int]] = ContextVar(
    "vanguard_telegram_account_session_lock_keys",
    default=frozenset(),
)


def _advisory_key(telegram_chat_id: int) -> int:
    """Return a stable signed bigint in a namespace private to Vanguard."""

    digest = hashlib.blake2b(
        str(int(telegram_chat_id)).encode("ascii"),
        digest_size=8,
        person=b"vngrd-chat-lock",
    ).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


def _account_advisory_key(account_id: int) -> int:
    digest = hashlib.blake2b(
        str(int(account_id)).encode("ascii"),
        digest_size=8,
        person=b"vngrd-acct-lock",
    ).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


def _release_fallback_locks(sync_session, transaction) -> None:  # type: ignore[no-untyped-def]
    """Mirror PostgreSQL xact-lock release for non-PostgreSQL test engines."""

    if transaction.parent is not None:
        return
    held = sync_session.info.pop(_SESSION_LOCKS_KEY, {})
    for lock in held.values():
        if lock.locked():
            lock.release()


async def acquire_telegram_chat_transaction_lock(
    db: AsyncSession,
    telegram_chat_id: int,
) -> None:
    """Serialize one chat until the current outer database transaction ends.

    Production PostgreSQL sessions use ``pg_advisory_xact_lock``. SQLite and
    other test engines use an event-loop-local asyncio lock released by the
    SQLAlchemy outer-transaction hook. Repeated acquisition by one session is
    intentionally re-entrant.
    """

    key = _advisory_key(telegram_chat_id)
    if key in _session_lock_keys.get():
        return
    bind = db.get_bind()
    if bind.dialect.name == "postgresql":
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": key},
        )
        return

    held: dict[tuple[int, int], asyncio.Lock] = db.info.setdefault(
        _SESSION_LOCKS_KEY, {}
    )
    loop_key = (id(asyncio.get_running_loop()), key)
    if loop_key in held:
        return

    lock = _fallback_locks.get(loop_key)
    if lock is None:
        lock = asyncio.Lock()
        _fallback_locks[loop_key] = lock
    await lock.acquire()
    held[loop_key] = lock

    if not db.info.get(_SESSION_LISTENER_KEY):
        event.listen(
            db.sync_session,
            "after_transaction_end",
            _release_fallback_locks,
        )
        db.info[_SESSION_LISTENER_KEY] = True


@asynccontextmanager
async def telegram_chat_advisory_lock(
    db: AsyncSession,
    telegram_chat_id: int,
) -> AsyncIterator[None]:
    """Hold the shared chat lock across commits and external network I/O.

    PostgreSQL uses a session-scoped advisory lock on a dedicated pooled
    connection. The acquisition transaction is committed immediately; the
    advisory lock remains held while no database transaction stays open.
    SQLite and other test engines use the same event-loop lock namespace as
    the transaction-scoped fallback.
    """

    key = _advisory_key(telegram_chat_id)
    held_keys = _session_lock_keys.get()
    if key in held_keys:
        yield
        return

    token = _session_lock_keys.set(held_keys | {key})
    bind = db.get_bind()
    try:
        if bind.dialect.name == "postgresql":
            async_bind = db.bind
            if async_bind is None or not hasattr(async_bind, "connect"):
                raise RuntimeError(
                    "an AsyncEngine-bound session is required for chat advisory lock"
                )
            connection = await async_bind.connect()
            try:
                await connection.execute(
                    text("SELECT pg_advisory_lock(:lock_key)"),
                    {"lock_key": key},
                )
                await connection.commit()
                try:
                    yield
                finally:
                    await connection.execute(
                        text("SELECT pg_advisory_unlock(:lock_key)"),
                        {"lock_key": key},
                    )
                    await connection.commit()
            finally:
                await connection.close()
            return

        loop_key = (id(asyncio.get_running_loop()), key)
        lock = _fallback_locks.get(loop_key)
        if lock is None:
            lock = asyncio.Lock()
            _fallback_locks[loop_key] = lock
        await lock.acquire()
        try:
            yield
        finally:
            if lock.locked():
                lock.release()
    finally:
        _session_lock_keys.reset(token)


telegram_chat_session_lock = telegram_chat_advisory_lock


@asynccontextmanager
async def telegram_account_advisory_lock(
    db: AsyncSession,
    account_id: int,
) -> AsyncIterator[None]:
    """Serialize phase-two account-wide daily-limit checks across chats."""

    key = _account_advisory_key(account_id)
    held_keys = _account_lock_keys.get()
    if key in held_keys:
        yield
        return

    token = _account_lock_keys.set(held_keys | {key})
    bind = db.get_bind()
    try:
        if bind.dialect.name == "postgresql":
            async_bind = db.bind
            if async_bind is None or not hasattr(async_bind, "connect"):
                raise RuntimeError(
                    "an AsyncEngine-bound session is required for account advisory lock"
                )
            connection = await async_bind.connect()
            try:
                await connection.execute(
                    text("SELECT pg_advisory_lock(:lock_key)"),
                    {"lock_key": key},
                )
                await connection.commit()
                try:
                    yield
                finally:
                    await connection.execute(
                        text("SELECT pg_advisory_unlock(:lock_key)"),
                        {"lock_key": key},
                    )
                    await connection.commit()
            finally:
                await connection.close()
            return

        loop_key = (id(asyncio.get_running_loop()), key)
        lock = _fallback_locks.get(loop_key)
        if lock is None:
            lock = asyncio.Lock()
            _fallback_locks[loop_key] = lock
        await lock.acquire()
        try:
            yield
        finally:
            if lock.locked():
                lock.release()
    finally:
        _account_lock_keys.reset(token)


__all__ = [
    "acquire_telegram_chat_transaction_lock",
    "telegram_chat_advisory_lock",
    "telegram_chat_session_lock",
    "telegram_account_advisory_lock",
]
