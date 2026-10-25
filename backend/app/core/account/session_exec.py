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
import math
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import structlog

from app.core.account.session_lease import OWNER_PREFIX
from app.core.account.session_lease import PROCESS_ID as _PROCESS_ID

# Backwards-compatible re-export used by the scheduler's owner comparison.
PROCESS_ID = _PROCESS_ID

logger = structlog.get_logger()

REQUEST_KEY = "vanguard:session-exec:requests"
REPLY_PREFIX = "vanguard:session-exec:reply:"
REPLY_TTL_SECONDS = 300
# The celery quantum task has a 330s soft limit; leave room for reporting.
FORWARD_TIMEOUT_SECONDS = 270.0
# Finish execution before the forwarding caller and its Celery task expire.
STAGE_TIMEOUT_SECONDS = 240.0
REPLY_MARGIN_SECONDS = 15.0
# Non-blocking poll cadence on the owner side; blocking BRPOP would hold a
# pooled connection and collide with the shared client's socket timeout.
POLL_INTERVAL_SECONDS = 0.5
# Capacity contention is transient: retry sooner than the old 30 second
# cadence so a freed owner slot is not left idle for a full scheduler tick.
# Keep this bounded and explicit; callers still apply their own queue/backoff.
CAPACITY_WAIT_RETRY_SECONDS = 15


@dataclass
class _ActorContext:
    account_id: int
    active: bool = True


_actor_context: ContextVar[_ActorContext | None] = ContextVar("session_exec_actor", default=None)


def current_actor_account() -> int | None:
    """Account whose serialized owner stage is executing in this context.

    The mutable active flag also expires copies inherited by child tasks when
    their parent stage finishes, so detached reads cannot retain owner access.
    """
    context = _actor_context.get()
    return context.account_id if context is not None and context.active else None


async def _execute_in_actor(
    execute_stage: Callable[[int, str], Awaitable[tuple[dict[str, Any], int]]],
    account_id: int,
    stage: str,
) -> tuple[dict[str, Any], int]:
    context = _ActorContext(account_id)
    token = _actor_context.set(context)
    try:
        return await execute_stage(account_id, stage)
    finally:
        context.active = False
        _actor_context.reset(token)


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
            json.dumps({"id": request_id, "account_id": int(account_id), "stage": stage,
                        "expires_at": time.time() + timeout}),
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
    defer_if_busy: bool = False,
    parallel_stages: frozenset[str] | None = None,
    queued_stages: frozenset[str] | None = None,
    priority_stages: frozenset[str] | None = None,
    priority_reserved_slots: int = 1,
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
    With ``parallel_stages``, only those stages may share the executor; other
    stages reserve it exclusively. ``priority_stages`` additionally reserves a
    bounded executor slot for the listed stages. Background work can therefore
    make progress without consuming the last slot needed by a join or an
    advertisement. This requires busy deferral. Requests for
    ``queued_stages`` are retained in a small local wait queue while the owner
    is busy, so a protected stage cannot be repeatedly dropped behind a stream
    of review requests. Other blocked requests keep the original immediate
    ``session_owner_capacity_wait`` response and are rescheduled by their
    caller.
    """
    from app.core.config import settings

    if max_concurrency is None:
        max_concurrency = max(1, int(getattr(settings, "SESSION_EXEC_CONCURRENCY", 2)))
    priority_stages = frozenset(priority_stages or ())
    if (parallel_stages is not None or priority_stages) and not defer_if_busy:
        raise ValueError("restricted stage concurrency requires busy deferral")
    reserved_slots = min(
        max(0, int(priority_reserved_slots)),
        max(0, max_concurrency - 1),
    )
    semaphore = asyncio.Semaphore(max_concurrency)
    account_locks: dict[int, asyncio.Lock] = {}
    inflight: set[asyncio.Task[None]] = set()
    inflight_stages: dict[asyncio.Task[None], str] = {}
    queued = deque[tuple[dict[str, Any], str]]()
    # A worker must never become an unbounded request buffer if the owner is
    # unhealthy.  The forwarding caller still has its own deadline and will
    # retry after this bounded queue fills.
    queue_limit = max(1, max_concurrency * 4)

    def completed(task: asyncio.Task[None]) -> None:
        inflight.discard(task)
        inflight_stages.pop(task, None)

    async def execute_and_reply(request: dict[str, Any], reply_key: str) -> None:
        account_id = int(request.get("account_id") or 0)
        stage = str(request.get("stage") or "")
        lock = account_locks.setdefault(account_id, asyncio.Lock())
        async with semaphore:
            # One account, one stage at a time: its client, read budget lane
            # and one-shot contracts are not safe to interleave.
            async with lock:
                try:
                    started_at = time.time()
                    expires_at = float(request.get("expires_at", started_at + FORWARD_TIMEOUT_SECONDS))
                    if not math.isfinite(expires_at):
                        raise ValueError("invalid session execution deadline")
                    remaining = expires_at - started_at
                    if remaining <= 0:
                        await _reply(client, reply_key, {"result": {"processed": 0,
                            "reason": "session_owner_request_expired"}, "interval": 15})
                        return
                    budget = min(STAGE_TIMEOUT_SECONDS, remaining - min(REPLY_MARGIN_SECONDS, remaining / 10))
                    logger.info("session_exec_stage_started", account_id=account_id, stage=stage,
                                request_id=request.get("id"), started_at=started_at,
                                deadline_at=started_at + budget)
                    timer = asyncio.timeout(budget)
                    try:
                        async with timer:
                            result, interval = await _execute_in_actor(execute_stage, account_id, stage)
                    except TimeoutError:
                        if not timer.expired():
                            raise
                        # Cancellation unwinds the stage's DB/pool/lease contexts.
                        # Durable attempted writes stay reserved for reconciliation.
                        logger.warning("session_exec_stage_timed_out", account_id=account_id,
                                       stage=stage, request_id=request.get("id"),
                                       elapsed_seconds=round(time.time() - started_at, 3))
                        await _reply(client, reply_key, {"result": {"processed": 0,
                            "reason": "session_owner_stage_timeout", "retryable": True}, "interval": 60})
                        return
                    logger.info("session_exec_stage_completed", account_id=account_id, stage=stage,
                                request_id=request.get("id"),
                                elapsed_seconds=round(time.time() - started_at, 3))
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

    def stage_conflicts(stage: str) -> bool:
        # A protected stage may use any free executor slot. Its account-level
        # lock still serializes operations that share one Telegram session.
        if priority_stages and stage in priority_stages:
            return False
        if priority_stages:
            active_priority = any(active in priority_stages for active in inflight_stages.values())
            if active_priority or len(inflight) >= max_concurrency - reserved_slots:
                return True
        return bool(inflight) and parallel_stages is not None and (
            stage not in parallel_stages
            or any(active not in parallel_stages for active in inflight_stages.values())
        )

    def can_start(stage: str) -> bool:
        return len(inflight) < max_concurrency and not stage_conflicts(stage)

    async def expire_queued() -> None:
        """Reply to queued requests whose forwarding deadline has elapsed."""
        if not queued:
            return
        now = time.time()
        retained = deque[tuple[dict[str, Any], str]]()
        while queued:
            request, reply_key = queued.popleft()
            try:
                expires_at = float(request.get("expires_at", now + FORWARD_TIMEOUT_SECONDS))
            except (TypeError, ValueError):
                expires_at = now
            if not math.isfinite(expires_at) or expires_at <= now:
                await _reply(client, reply_key, {"result": {"processed": 0,
                    "reason": "session_owner_request_expired", "retryable": True}, "interval": 15})
            else:
                retained.append((request, reply_key))
        queued.extend(retained)

    try:
        while True:
            await expire_queued()

            # Start retained priority requests as soon as a slot opens. They
            # are intentionally considered before Redis so a protected stage
            # cannot repeatedly lose the first free slot to review work.
            started_queued = False
            if queued:
                queued_items = list(queued)
                if priority_stages:
                    queued_items.sort(
                        key=lambda item: str(item[0].get("stage") or "") not in priority_stages
                    )
                for request, reply_key in queued_items:
                    stage = str(request.get("stage") or "")
                    if not can_start(stage):
                        continue
                    queued.remove((request, reply_key))
                    task = asyncio.create_task(execute_and_reply(request, reply_key))
                    inflight.add(task)
                    inflight_stages[task] = stage
                    task.add_done_callback(completed)
                    started_queued = True
                    # Keep polling the queue only after this task has been
                    # admitted; the next pass can fill another free slot.
                    break
            if started_queued:
                continue

            # Reap only when the in-flight set is saturated; done callbacks keep
            # it accurate otherwise and polling never busy-spins.
            if not defer_if_busy and len(inflight) >= max_concurrency * 2:
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
            if defer_if_busy and not can_start(stage):
                if queued_stages and stage in queued_stages and len(queued) < queue_limit:
                    queued.append((request, reply_key))
                    continue
                await _reply(client, reply_key, {"result": {"processed": 0,
                    "reason": "session_owner_capacity_wait", "retryable": True},
                    "interval": CAPACITY_WAIT_RETRY_SECONDS})
                continue
            task = asyncio.create_task(execute_and_reply(request, reply_key))
            inflight.add(task)
            inflight_stages[task] = stage
            task.add_done_callback(completed)
    finally:
        for task in inflight:
            task.cancel()
        if inflight:
            await asyncio.gather(*list(inflight), return_exceptions=True)


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
