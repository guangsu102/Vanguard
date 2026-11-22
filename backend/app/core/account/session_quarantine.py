"""Small durable account fences, independent of long-running business transactions."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import update

QUARANTINE_PREFIX = "vanguard:telegram:quarantine:"
IO_TIMEOUT_SECONDS = 1.0
DB_TIMEOUT_SECONDS = 2.0
DB_RETRY_DELAYS = (0.0, 0.05, 0.15)
_TOKEN = re.compile(r"[0-9a-f]{32}")
_PUBLISH_LUA = """-- quarantine_publish_sentinel_v1
local current = redis.call('get', KEYS[1])
local previous = redis.call('get', KEYS[3])
if current == ARGV[2] or
   (not current and ARGV[3] == '' and (not previous or previous == ARGV[1])) or
   (ARGV[3] ~= '' and current == ARGV[3] and redis.call('get', KEYS[2]) == ARGV[4]) then
  redis.call('set', KEYS[3], ARGV[1])
  redis.call('set', KEYS[1], ARGV[2])
  redis.call('del', KEYS[2])
  return 1
end
return 0
"""
_CLEAR_LUA = """-- quarantine_compare_delete_v1
local marker = redis.call('get', KEYS[1])
local lease = redis.call('get', KEYS[2])
if (not marker or marker == ARGV[1]) and (not lease or lease == ARGV[2]) then
  redis.call('del', KEYS[1], KEYS[2], KEYS[3])
  return 1
end
return 0
"""


class QuarantineUnavailable(RuntimeError):
    """The shared fence cannot be safely inspected or published."""


class StaleQuarantineOwner(QuarantineUnavailable):
    """An old generation must not quarantine or overwrite a newer owner."""


@dataclass(frozen=True)
class PublishedFence:
    token: str
    file_confirmed: bool
    sentinel_confirmed: bool


def _root() -> Path:
    from app.core.config import settings

    return Path(settings.TELEGRAM_SESSION_DIR).resolve() / ".account-quarantine"


def _paths(account_id: int | str) -> tuple[Path, Path]:
    value = int(account_id)
    if value <= 0:
        raise QuarantineUnavailable("invalid quarantine account")
    root = _root()
    return root / f"{value}.json", root / f"{value}.lock"


def _lock(account_id: int | str):
    path, lock_path = _paths(account_id)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    handle = open(lock_path, "a+b")
    os.chmod(lock_path, 0o600)
    if lock_path.stat().st_size == 0:
        handle.write(b"0")
        handle.flush()
    deadline = time.monotonic() + IO_TIMEOUT_SECONDS
    while True:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError:
            if time.monotonic() >= deadline:
                handle.close()
                raise QuarantineUnavailable("quarantine lock unavailable") from None
            time.sleep(0.01)


@asynccontextmanager
async def _locked(account_id: int | str):
    # The worker may be cancelled while a thread is acquiring the OS lock.
    # Always retrieve and close that handle instead of leaking a process lock.
    task = asyncio.create_task(asyncio.to_thread(_lock, account_id))
    try:
        handle = await asyncio.shield(task)
    except asyncio.CancelledError:
        handle = await _settle(task)
        handle.close()
        raise
    except Exception as exc:
        raise QuarantineUnavailable("quarantine storage unavailable") from exc
    try:
        yield
    finally:
        handle.close()


async def _settle(task: asyncio.Task):
    """Finish bounded safety cleanup even when the caller cancels repeatedly."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


async def _file_call(function, *args):
    """Keep the OS lock until an already-started file operation completes."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await _settle(task)
        raise


def _read(account_id: int | str) -> dict[str, Any] | None:
    path, _ = _paths(account_id)
    try:
        with path.open("rb") as source:
            raw = source.read(513)
    except FileNotFoundError:
        return None
    try:
        value = json.loads(raw)
        if (len(raw) > 512 or not isinstance(value, dict)
                or not _TOKEN.fullmatch(str(value.get("token", "")))
                or value.get("reason") != "telegram_session_quarantined"
                or type(value.get("created_at")) not in (int, float)
                or not math.isfinite(value["created_at"])):
            raise ValueError
        return value
    except (ValueError, TypeError) as exc:
        raise QuarantineUnavailable("invalid quarantine fence") from exc


def _sync_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _write(account_id: int | str, token: str) -> None:
    path, _ = _paths(account_id)
    # All writers hold the per-account OS lock. A fixed pending file bounds
    # crash leftovers to one small file per account.
    temporary = path.with_name(f".{path.name}.pending")
    value = {"token": token, "reason": "telegram_session_quarantined", "created_at": time.time()}
    try:
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600,
        )
        with os.fdopen(descriptor, "wb") as target:
            target.write(json.dumps(value, separators=(",", ":")).encode())
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


async def publish(
    account_id: int | str, redis: Any, *, token: str | None = None,
    lease_token: str | None = None, owner: str | None = None,
) -> PublishedFence:
    """Publish before releasing ownership; either durable store is sufficient.

    The shared sessions mount protects restarts even while Redis and PG are
    unavailable. Redis deliberately has no TTL; normal owners never clear it.
    """
    token = token or secrets.token_hex(16)
    if not _TOKEN.fullmatch(token):
        raise QuarantineUnavailable("invalid quarantine token")
    local_ok = False
    sentinel_ok = False

    async def publish_redis() -> bool | None:
        if redis is None:
            return None
        try:
            async with asyncio.timeout(IO_TIMEOUT_SECONDS):
                return bool(await redis.eval(
                    _PUBLISH_LUA, 3, f"vanguard:telegram:lease:{account_id}",
                    f"vanguard:telegram:owner:{account_id}", f"{QUARANTINE_PREFIX}{account_id}",
                    token, f"quarantine:{token}", lease_token or "", owner or "",
                ))
        except Exception:
            return None

    try:
        async with _locked(account_id):
            local = await _file_call(_read, account_id)
            # A supplied token is an idempotent retry/recovery, never permission
            # to overwrite a newer file generation.
            if token is not None and local is not None and local["token"] != token and lease_token is None:
                raise StaleQuarantineOwner("quarantine generation changed")
            outcome = await publish_redis()
            if outcome is False:
                raise StaleQuarantineOwner("quarantine owner changed")
            if outcome is None and local is not None and local["token"] != token:
                raise QuarantineUnavailable("quarantine ownership unavailable")
            sentinel_ok = outcome is True
            await _file_call(_write, account_id, token)
            local_ok = True
    except asyncio.CancelledError:
        raise
    except StaleQuarantineOwner:
        raise
    except Exception:
        if not sentinel_ok:
            outcome = await publish_redis()
            if outcome is False:
                raise StaleQuarantineOwner("quarantine owner changed")
            sentinel_ok = outcome is True
    if not local_ok and not sentinel_ok:
        raise QuarantineUnavailable("quarantine fence not persisted")
    return PublishedFence(token, local_ok, sentinel_ok)


async def current(account_id: int | str, redis: Any) -> str | None:
    """Inspect both stores; a damaged/unreadable file fails closed."""
    async with _locked(account_id):
        local = await _file_call(_read, account_id)
        if redis is None:
            raise QuarantineUnavailable("quarantine Redis unavailable")
        try:
            async with asyncio.timeout(IO_TIMEOUT_SECONDS):
                remote = await redis.get(f"{QUARANTINE_PREFIX}{account_id}")
        except Exception as exc:
            raise QuarantineUnavailable("quarantine Redis unavailable") from exc
        if isinstance(remote, bytes):
            remote = remote.decode("ascii", "strict")
        if remote is not None and not _TOKEN.fullmatch(str(remote)):
            raise QuarantineUnavailable("invalid quarantine Redis fence")
        if local is not None and remote not in (None, local["token"]):
            raise QuarantineUnavailable("quarantine generations disagree")
        return local["token"] if local is not None else remote


async def clear_after_recovery(account_id: int | str, token: str, redis: Any) -> bool:
    """Compare under a cross-process lock, only after ONLINE has committed."""
    if not _TOKEN.fullmatch(token) or redis is None:
        return False
    async with _locked(account_id):
        return await _clear_locked(account_id, token, redis)


async def _clear_locked(account_id: int | str, token: str, redis: Any) -> bool:
    local = await _file_call(_read, account_id)
    if local is not None and local["token"] != token:
        return False
    async with asyncio.timeout(IO_TIMEOUT_SECONDS):
        key = f"{QUARANTINE_PREFIX}{account_id}"
        remote = await redis.get(key)
        if isinstance(remote, bytes):
            remote = remote.decode("ascii", "strict")
        if remote not in (None, token):
            return False
    # Keep a durable file until Redis positively acknowledges the clear. A
    # timed-out Redis request can still execute later on the server; that old
    # delete must never be able to remove our only remaining fence.
    if local is None:
        await _file_call(_write, account_id, token)

    async def remove() -> bool:
        async with asyncio.timeout(IO_TIMEOUT_SECONDS):
            confirmed = bool(await redis.eval(
                _CLEAR_LUA, 3, key, f"vanguard:telegram:lease:{account_id}",
                f"vanguard:telegram:owner:{account_id}", token, f"quarantine:{token}",
            ))
        if confirmed:
            path, _ = _paths(account_id)
            await _file_call(path.unlink)
            await _file_call(_sync_directory, path.parent)
        return confirmed

    async def restore() -> None:
        # This runs under the same OS lock, before another owner can inspect
        # the half-cleared fence. Either durable store continues to block use.
        file_ok = False
        try:
            await _file_call(_write, account_id, token)
            file_ok = True
        except Exception:
            pass
        try:
            async with asyncio.timeout(IO_TIMEOUT_SECONDS):
                confirmed = await redis.eval(
                    _PUBLISH_LUA, 3, f"vanguard:telegram:lease:{account_id}",
                    f"vanguard:telegram:owner:{account_id}", key,
                    token, f"quarantine:{token}", "", "",
                )
            if confirmed or file_ok:
                return
        except Exception:
            if file_ok:
                return
        raise QuarantineUnavailable("quarantine clear rollback unavailable")

    task = asyncio.create_task(remove())
    try:
        cleared = await asyncio.shield(task)
    except BaseException:
        # A shielded Redis delete must settle before restoring, otherwise its
        # late completion could remove the fence we have just restored.
        try:
            await _settle(task)
        except BaseException:
            pass
        await _settle(asyncio.create_task(restore()))
        raise
    if not cleared:
        await restore()
    return cleared


@asynccontextmanager
async def recovery_scope(account_id: int | str, token: str | None, redis: Any):
    """Serialize final recovery commit/clear against quarantine DB retries.

    Enter before opening the final DB transaction, and invoke the yielded
    clear callback only after that transaction and its refresh have closed.
    """
    async with _locked(account_id):
        local = await _file_call(_read, account_id)
        if local is not None and local["token"] != token:
            raise StaleQuarantineOwner("quarantine generation changed")
        if redis is None:
            raise QuarantineUnavailable("quarantine Redis unavailable")
        async with asyncio.timeout(IO_TIMEOUT_SECONDS):
            remote = await redis.get(f"{QUARANTINE_PREFIX}{account_id}")
            lease = await redis.get(f"vanguard:telegram:lease:{account_id}")
        if isinstance(remote, bytes):
            remote = remote.decode("ascii", "strict")
        if isinstance(lease, bytes):
            lease = lease.decode("ascii", "strict")
        expected_lease = f"quarantine:{token}" if token is not None else None
        if remote != token or lease != expected_lease:
            raise StaleQuarantineOwner("quarantine generation changed")

        async def clear() -> bool:
            return token is None or await _clear_locked(account_id, token, redis)

        yield clear


async def persist_error(
    account_id: int | str, *, token: str | None = None, redis: Any = None,
    failure_reason: str | None = None,
    expected_updated_at: Any = None,
) -> bool:
    """One conditional UPDATE per independent, bounded transaction attempt."""
    import json
    from datetime import datetime

    from sqlalchemy import select

    from app.core.account.models import AccountEnvironmentEvent, AccountStatus, TelegramAccount
    from app.core.database import get_db_session

    async def attempt() -> bool:
        async with asyncio.timeout(DB_TIMEOUT_SECONDS):
            async with get_db_session() as db:
                # Retrying a failed persistence or a recovery probe must not
                # create a fresh episode or invalidate its timestamp fence.
                error_at = datetime.utcnow()
                result = await db.execute(update(TelegramAccount).where(
                    TelegramAccount.id == int(account_id),
                    TelegramAccount.status.not_in((AccountStatus.ERROR, AccountStatus.BANNED,
                                                   AccountStatus.RESTRICTED)),
                    *([TelegramAccount.updated_at == expected_updated_at]
                      if expected_updated_at is not None else []),
                ).values(status=AccountStatus.ERROR, updated_at=error_at))
                matched = result.rowcount == 1
                if not matched:
                    return await db.scalar(select(TelegramAccount.status).where(
                        TelegramAccount.id == int(account_id),
                    )) == AccountStatus.ERROR
                if failure_reason is not None:
                    db.add(AccountEnvironmentEvent(
                        account_id=int(account_id), event_type="account_connection_failure",
                        status="failed", reason=failure_reason,
                        details=json.dumps({"source": "account_pool", "quarantine_token": token,
                                            "error_updated_at": error_at.isoformat()}),
                        created_at=error_at,
                    ))
            return matched

    for delay in DB_RETRY_DELAYS:
        if delay:
            await asyncio.sleep(delay)  # The previous session is already closed.
        try:
            if token is None:
                return await attempt()
            if not _TOKEN.fullmatch(token):
                return False
            async with _locked(account_id):
                local = await _file_call(_read, account_id)
                if local is not None:
                    if local["token"] != token:
                        return False
                    if redis is not None:
                        try:
                            async with asyncio.timeout(IO_TIMEOUT_SECONDS):
                                remote = await redis.get(f"{QUARANTINE_PREFIX}{account_id}")
                        except Exception:
                            # An intact shared file still authorizes ERROR
                            # persistence during a Redis outage, never recovery.
                            remote = token
                        if isinstance(remote, bytes):
                            remote = remote.decode("ascii", "strict")
                        if remote not in (None, token):
                            return False
                else:
                    if redis is None:
                        return False
                    async with asyncio.timeout(IO_TIMEOUT_SECONDS):
                        remote = await redis.get(f"{QUARANTINE_PREFIX}{account_id}")
                    if isinstance(remote, bytes):
                        remote = remote.decode("ascii", "strict")
                    if remote != token:
                        return False
                # The same OS lock spans the short DB commit. Recovery cannot
                # clear this token and publish ONLINE between check and UPDATE.
                return await attempt()
        except asyncio.CancelledError:
            raise
        except Exception:
            continue
    return False
