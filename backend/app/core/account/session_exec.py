"""Forward session-bound stage quanta to the process that owns the session.

The exclusive Telegram session lease (``session_lease``) guarantees one client
per auth key.  A long-lived listener process therefore owns the sessions of its
accounts, and work lanes running in other processes can never create clients
for them.  Instead of negotiating a lease handoff, the celery quantum task
forwards the whole stage execution to the owner: the owner runs the identical
service code with its already-connected client and returns the result.  All
one-shot contracts (receipt verification, budgets, leases) live in the service
layer and apply unchanged on either side.

The channel is deliberately small: one Redis request list, one per-request
reply key, and an owner registry maintained alongside the session lease.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from typing import Any, Awaitable, Callable

import structlog

from app.core.account.session_lease import OWNER_PREFIX, PROCESS_ID

logger = structlog.get_logger()

REQUEST_KEY = "vanguard:session-exec:requests"
REPLY_PREFIX = "vanguard:session-exec:reply:"
REPLY_TTL_SECONDS = 300
# The celery quantum task has a 330s soft limit; leave room for reporting.
FORWARD_TIMEOUT_SECONDS = 270.0
# Non-blocking poll cadence on the owner side; blocking BRPOP would hold a
# pooled connection and collide with the shared client's socket timeout.
POLL_INTERVAL_SECONDS = 0.5


async def owner_id(client: Any, account_id: int) -> str | None:
    """Return the owning process id for an account session, if any."""
    try:
        value = await client.get(f"{OWNER_PREFIX}{int(account_id)}")
    except Exception:
        return None
    return value if isinstance(value, str) and value else None


def _dedicated_client() -> Any:
    """A private connection for blocking waits; never shares the app pool."""
    import redis.asyncio as redis_asyncio

    from app.core.config import settings

    return redis_asyncio.from_url(
        settings.REDIS_URL,
        password=settings.effective_redis_password,
        encoding="utf-8",
        decode_responses=True,
        # The only blocking call is BRPOP on the reply key bounded by the
        # forward timeout itself; a socket timeout must not preempt it.
        socket_timeout=None,
        socket_connect_timeout=10,
    )


async def forward(
    account_id: int,
    stage: str,
    *,
    client: Any = None,
    timeout: float = FORWARD_TIMEOUT_SECONDS,
) -> dict[str, Any] | None:
    """Ask the session owner to execute one stage quantum.

    Returns the owner's reply dict, or ``None`` when no owner answers before
    the deadline (owner down or hung).  ``{"error": "not_owner"}`` means the
    registry entry was stale; the caller should execute locally.
    """
    own_client = client is None
    if own_client:
        client = _dedicated_client()
    request_id = uuid.uuid4().hex
    reply_key = REPLY_PREFIX + request_id
    try:
        await client.lpush(
            REQUEST_KEY,
            json.dumps({"id": request_id, "account_id": int(account_id), "stage": stage}),
        )
        item = await client.brpop(reply_key, timeout=int(timeout))
    finally:
        if own_client:
            with _suppress_close_errors():
                await client.aclose()
    if item is None:
        return None
    try:
        reply = json.loads(item[1])
    except (TypeError, ValueError):
        return None
    return reply if isinstance(reply, dict) else None


async def serve_requests(
    client: Any,
    *,
    owns: Callable[[int], bool],
    execute_stage: Callable[[int, str], Awaitable[tuple[dict[str, Any], int]]],
    poll_interval: float = POLL_INTERVAL_SECONDS,
    max_concurrency: int | None = None,
) -> None:
    """Owner-side consumer loop; runs until cancelled.

    ``owns`` decides whether this process holds a connected session for the
    account; ``execute_stage`` runs the stage and returns ``(result,
    interval)`` exactly like ``growth_dispatch.execute``.  Polling keeps every
    Redis call short; a blocking BRPOP would monopolize a pooled connection.

    Different accounts execute concurrently (their sessions, database sessions
    and one-shot contracts are independent) while one account stays serial
    through a per-account lock.  Concurrency is capped by
    ``SESSION_EXEC_CONCURRENCY`` so the shared database pool cannot exhaust.
    """
    from app.core.config import settings

    if max_concurrency is None:
        max_concurrency = max(1, int(getattr(settings, "SESSION_EXEC_CONCURRENCY", 2)))
    semaphore = asyncio.Semaphore(max_concurrency)
    account_locks: dict[int, asyncio.Lock] = {}
    inflight: set[asyncio.Task[None]] = set()

    async def execute_and_reply(request: dict[str, Any], reply_key: str) -> None:
        account_id = int(request.get("account_id") or 0)
        stage = str(request.get("stage") or "")
        lock = account_locks.setdefault(account_id, asyncio.Lock())
        async with semaphore:
            # One account, one stage at a time: its client, read budget lane
            # and one-shot contracts are not safe to interleave.
            async with lock:
                try:
                    result, interval = await execute_stage(account_id, stage)
                    await _reply(client, reply_key, {"result": result, "interval": int(interval)})
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "session_exec_stage_failed",
                        account_id=account_id,
                        stage=stage,
                        error_type=type(exc).__name__,
                        error=str(exc)[:300],
                    )
                    await _reply(
                        client,
                        reply_key,
                        {
                            "error": "execution_failed",
                            "error_type": type(exc).__name__,
                            "error_message": str(exc)[:500],
                        },
                    )

    while True:
        # Reap only when the in-flight set is saturated; done callbacks keep
        # it accurate otherwise and polling never busy-spins.
        if len(inflight) >= max_concurrency * 2:
            await asyncio.wait(
                set(inflight), timeout=poll_interval, return_when=asyncio.FIRST_COMPLETED
            )
            continue
        try:
            item = await client.rpop(REQUEST_KEY)
        except Exception:
            await asyncio.sleep(poll_interval)
            continue
        if item is None:
            await asyncio.sleep(poll_interval)
            continue
        try:
            request = json.loads(item)
        except (TypeError, ValueError):
            continue
        if not isinstance(request, dict):
            continue
        try:
            account_id = int(request.get("account_id") or 0)
        except (TypeError, ValueError):
            continue
        stage = str(request.get("stage") or "")
        reply_key = REPLY_PREFIX + str(request.get("id"))
        if not account_id or not stage or not owns(account_id):
            await _reply(client, reply_key, {"error": "not_owner"})
            continue
        task = asyncio.create_task(execute_and_reply(request, reply_key))
        inflight.add(task)
        task.add_done_callback(inflight.discard)


async def _reply(client: Any, reply_key: str, payload: dict[str, Any]) -> None:
    try:
        await client.lpush(reply_key, json.dumps(payload, default=str))
        await client.expire(reply_key, REPLY_TTL_SECONDS)
    except Exception:
        logger.warning("session_exec_reply_failed", reply_key=reply_key)


@contextlib.contextmanager
def _suppress_close_errors():
    try:
        yield
    except Exception:
        pass
