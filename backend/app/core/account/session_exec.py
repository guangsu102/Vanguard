"""Bounded, fair execution using the process that owns each Telegram session.

The service layer retains its budgets, receipts and operation leases. This
channel only admits work, serializes an account, and propagates cancellation.
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
ACTIVE_PREFIX = "vanguard:session-exec:active:"
REPLY_TTL_SECONDS = 300
FORWARD_TIMEOUT_SECONDS = 270.0
# One slow stage must yield before the Celery task or its caller expires.
# Services retain attempted writes for reconciliation when cancellation occurs.
STAGE_TIMEOUT_SECONDS = 60.0
QUEUE_TIMEOUT_SECONDS = 90.0
REPLY_MARGIN_SECONDS = 15.0
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

CLEANUP_TIMEOUT_SECONDS = 2.0
PRIORITY_BURST_LIMIT = 1

# Fixed identifiers only: never serialize exception text, paths or locals.
_TIMEOUT_TRACE_MODULES = {
    "app.core.account.session_exec": 1,
    "app.core.scheduler.growth_dispatch": 2,
    "app.modules.acquisition.qualification_service": 3,
    "app.modules.acquisition.qualification_engine": 4,
    "app.modules.acquisition.qualification_renewal": 5,
    "app.modules.acquisition.review_read_plan": 6,
    "app.modules.acquisition.review_read_runtime": 7,
    "app.modules.acquisition.review_batch": 8,
    "app.modules.acquisition.review_fact_owner": 9,
    "app.modules.acquisition.group_qualification": 10,
    "app.core.account.pool": 11,
    "app.core.account.rpc_governor": 12,
    "app.core.account.read_work": 13,
    "app.core.account.budget_ledger": 14,
    "app.core.account.session_guard": 15,
    "app.core.account.session_lease": 16,
    "app.core.account.operation_lease": 17,
    "app.modules.acquisition.review_evidence_handoff": 18,
    "app.modules.acquisition.qualification_events": 19,
    "app.modules.acquisition.qualification_system_identity": 20,
    "app.modules.acquisition.qualification_identity_refresh": 21,
    "app.modules.acquisition.qualification_ai": 22,
    "app.modules.acquisition.qualification_provider": 23,
}


def _timeout_trace(error: BaseException) -> dict[str, Any]:
    """Bounded in-memory traceback projection for one canary timeout log."""
    try:
        pending = deque([error])
        seen: set[int] = set()
        frames: deque[dict[str, int]] = deque(maxlen=8)
        matched = 0
        scanned = 0
        limited = False
        while pending and len(seen) < 4:
            current = pending.popleft()
            if id(current) in seen:
                continue
            seen.add(id(current))
            chain = len(seen) - 1
            trace = current.__traceback__
            for _ in range(64 - scanned):
                if trace is None:
                    break
                scanned += 1
                module = _TIMEOUT_TRACE_MODULES.get(trace.tb_frame.f_globals.get("__name__"), 0)
                if module:
                    frames.append({"chain_code": chain, "module_code": module, "line": int(trace.tb_lineno)})
                    matched += 1
                trace = trace.tb_next
            limited = limited or trace is not None
            for linked in (current.__cause__, current.__context__):
                if linked is not None and id(linked) not in seen:
                    pending.append(linked)
        limited = limited or matched > 8 or any(id(item) not in seen for item in pending)
        return {"timeout_app_frames": list(frames), "timeout_trace_limited": int(limited)}
    except Exception:
        # Optional diagnostics cannot replace the original timeout or reply.
        return {"timeout_app_frames": [], "timeout_trace_limited": 1}



_REPLY_IF_ACTIVE = """
-- session_exec_reply_if_active_v1
if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
redis.call('LPUSH', KEYS[2], ARGV[1])
redis.call('EXPIRE', KEYS[2], ARGV[2])
return 1
"""
_CLEANUP = """
-- session_exec_cleanup_v1
redis.call('DEL', KEYS[1], KEYS[2])
return redis.call('LREM', KEYS[3], 0, ARGV[1])
"""


async def owner_id(client: Any, account_id: int) -> str | None:
    try:
        value = await client.get(f"{OWNER_PREFIX}{int(account_id)}")
    except Exception:
        return None
    return value if isinstance(value, str) and value else None


def _dedicated_client() -> Any:
    import redis.asyncio as redis_asyncio

    from app.core.config import settings

    return redis_asyncio.from_url(
        settings.REDIS_URL,
        password=settings.effective_redis_password,
        encoding="utf-8",
        decode_responses=True,
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
    """Forward once; remove the request and cancel its owner work on exit.

    A not_owner reply still requires the dispatcher's ownership gate; it never
    authorizes creating another client for an existing session owner.
    """
    timeout = float(timeout)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("session execution timeout must be finite and positive")
    own_client = client is None
    if own_client:
        client = _dedicated_client()
    request_id = uuid.uuid4().hex
    reply_key = REPLY_PREFIX + request_id
    active_key = ACTIVE_PREFIX + request_id
    request_json = json.dumps(
        {
            "id": request_id,
            "account_id": int(account_id),
            "stage": stage,
            "expires_at": time.time() + timeout,
            "lifecycle": 1,
        }
    )
    item = None
    try:
        # The outer timeout bounds both connection setup and BRPOP, including
        # fractional deadlines. Redis timeout=0 would instead wait forever.
        async with asyncio.timeout(timeout):
            await client.set(active_key, "1", ex=max(1, math.ceil(timeout) + 30))
            await client.lpush(REQUEST_KEY, request_json)
            item = await client.brpop(reply_key, timeout=max(1, math.ceil(timeout)))
    except TimeoutError:
        return None
    finally:
        try:
            async with asyncio.timeout(CLEANUP_TIMEOUT_SECONDS):
                # Atomic with _REPLY_IF_ACTIVE: either cleanup removes the reply,
                # or a later reply observes no active caller and cannot write it.
                await client.eval(_CLEANUP, 3, active_key, reply_key, REQUEST_KEY, request_json)
        except Exception:
            # Markers/replies have bounded TTLs if Redis itself is unavailable.
            logger.warning("session_exec_caller_cleanup_failed", request_id=request_id)
        if own_client:
            with contextlib.suppress(Exception):
                async with asyncio.timeout(CLEANUP_TIMEOUT_SECONDS):
                    await client.aclose()
    if item is None:
        return None
    try:
        reply = json.loads(item[1])
    except (TypeError, ValueError):
        return None
    return reply if isinstance(reply, dict) else None


@dataclass
class _Request:
    payload: dict[str, Any]
    account_id: int
    stage: str
    reply_key: str
    expires_at: float
    queued_at: float
    wait_phase: str = "executor_capacity"

    @property
    def active_key(self) -> str | None:
        if self.payload.get("lifecycle") == 1:
            return ACTIVE_PREFIX + str(self.payload["id"])
        # Old in-flight publishers remain compatible during a rolling update.
        return None


def _wait(reason: str, phase: str, interval: int = CAPACITY_WAIT_RETRY_SECONDS) -> dict[str, Any]:
    return {"result": {"processed": 0, "reason": reason, "retryable": True, "wait_phase": phase}, "interval": interval}


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
    queue_timeout: float = QUEUE_TIMEOUT_SECONDS,
    stopping: Callable[[], bool] | None = None,
) -> None:
    """Run bounded stages; admit only after both account and slot are free.

    Background work can use at most cap-reserved slots. A running protected
    task does not block a different account from using another available slot.
    When both classes are queued, one protected admission is followed by one
    eligible background admission; the reserved protected slot remains intact.
    Within each class accounts rotate in FIFO order. Waiting work occupies no
    task, DB connection, semaphore, or account lock.

    Cancellation is cooperative: a slot/account stays occupied until service
    cleanup unwinds. A coroutine swallowing cancellation is quarantined in its
    existing slot; releasing it early would permit concurrent session use.
    """
    from app.core.config import settings

    if max_concurrency is None:
        max_concurrency = int(getattr(settings, "SESSION_EXEC_CONCURRENCY", 2))
    max_concurrency = max(1, int(max_concurrency))
    if not math.isfinite(queue_timeout) or queue_timeout <= 0:
        raise ValueError("queue timeout must be finite and positive")
    priority_stages = frozenset(priority_stages or ())
    if (parallel_stages is not None or priority_stages) and not defer_if_busy:
        raise ValueError("restricted stage concurrency requires busy deferral")
    reserved = min(max(0, int(priority_reserved_slots)), max_concurrency - 1)
    queued: deque[_Request] = deque()
    inflight: dict[asyncio.Task[None], _Request] = {}
    busy_accounts: set[int] = set()
    cancellation_requested: set[asyncio.Task[None]] = set()
    account_locks: dict[int, asyncio.Lock] = {}
    semaphore = asyncio.Semaphore(max_concurrency)
    queue_limit = max_concurrency * 4
    priority_streak = 0
    last_account: dict[bool, int] = {}

    def completed(task: asyncio.Task[None]) -> None:
        request = inflight.pop(task, None)
        cancellation_requested.discard(task)
        if request is not None:
            busy_accounts.discard(request.account_id)
            account_locks.pop(request.account_id, None)
        if not task.cancelled():
            # Retrieve failures even when shutdown races the callback.
            task.exception()

    def blocked_phase(request: _Request) -> str | None:
        if request.account_id in busy_accounts:
            return "account_session"
        if len(inflight) >= max_concurrency:
            return "executor_capacity"
        if request.stage not in priority_stages and priority_stages:
            background = sum(item.stage not in priority_stages for item in inflight.values())
            if background >= max_concurrency - reserved:
                return "protected_slot_reservation"
        if (
            inflight
            and parallel_stages is not None
            and (
                request.stage not in parallel_stages
                or any(item.stage not in parallel_stages for item in inflight.values())
            )
        ):
            return "exclusive_stage"
        return None

    async def live_requests() -> None:
        all_requests = list(queued) + list(inflight.values())
        keys = [item.active_key for item in all_requests if item.active_key]
        if not keys:
            return
        values = await client.mget(keys)
        active = {key for key, value in zip(keys, values, strict=True) if value}
        for request in list(queued):
            if request.active_key and request.active_key not in active:
                queued.remove(request)
        for task, request in list(inflight.items()):
            if request.active_key and request.active_key not in active and task not in cancellation_requested:
                cancellation_requested.add(task)
                logger.info(
                    "session_exec_cancellation_requested",
                    stage=request.stage,
                    request_id=request.payload["id"],
                    wait_phase="cancellation_cleanup",
                )
                task.cancel()

    async def execute_and_reply(request: _Request) -> None:
        async with semaphore:
            async with account_locks.setdefault(request.account_id, asyncio.Lock()):
                try:
                    if request.active_key and not await client.get(request.active_key):
                        return
                    # Ownership can change while a request waits in the queue.
                    if not owns(request.account_id):
                        await _reply(client, request, {"error": "not_owner"})
                        return
                    started_at = time.time()
                    remaining = request.expires_at - started_at
                    if remaining <= 0:
                        await _reply(client, request, _wait("session_owner_request_expired", "queue", 15))
                        return
                    budget = min(STAGE_TIMEOUT_SECONDS, remaining - min(REPLY_MARGIN_SECONDS, remaining / 10))
                    logger.info(
                        "session_exec_stage_started",
                        account_id=request.account_id,
                        stage=request.stage,
                        request_id=request.payload["id"],
                        started_at=started_at,
                        deadline_at=started_at + budget,
                        queued_seconds=round(time.monotonic() - request.queued_at, 3),
                    )
                    timer = asyncio.timeout(budget)
                    try:
                        async with timer:
                            result, interval = await _execute_in_actor(execute_stage, request.account_id, request.stage)
                    except TimeoutError as exc:
                        if not timer.expired():
                            raise
                        trace = (_timeout_trace(exc)
                                 if request.account_id == 33 and request.stage == "review" else {})
                        logger.warning(
                            "session_exec_stage_timed_out",
                            stage=request.stage,
                            request_id=request.payload["id"],
                            wait_phase="execution",
                            elapsed_seconds=round(time.time() - started_at, 3),
                            **trace,
                        )
                        await _reply(client, request, _wait("session_owner_stage_timeout", "execution", 60))
                        return
                    if timer.expired():
                        # A stage may suppress CancelledError while unwinding.
                        # Its late return still is not a successful quantum.
                        await _reply(client, request, _wait("session_owner_stage_timeout", "execution", 60))
                        return
                    logger.info(
                        "session_exec_stage_completed",
                        stage=request.stage,
                        request_id=request.payload["id"],
                        elapsed_seconds=round(time.time() - started_at, 3),
                    )
                    await _reply(client, request, {"result": result, "interval": int(interval)})
                except asyncio.CancelledError:
                    # The active marker is gone for caller cancellations. During
                    # owner shutdown a live caller receives a bounded retry.
                    await _reply(client, request, _wait("session_owner_shutdown", "owner_shutdown", 15))
                    raise
                except Exception as exc:
                    logger.warning(
                        "session_exec_stage_failed",
                        stage=request.stage,
                        request_id=request.payload["id"],
                        error_type=type(exc).__name__,
                        error=str(exc),
                        exc_info=True,
                    )
                    await _reply(client, request, {"error": "execution_failed", "error_type": type(exc).__name__})

    try:
        while True:
            if stopping is not None and stopping():
                # Do not claim queued work during a release; let observed RPCs
                # finish and commit their results under their existing leases.
                for request in queued:
                    await _reply(client, request, _wait("session_owner_shutdown", "owner_shutdown", 15))
                queued.clear()
                if inflight:
                    await asyncio.gather(*list(inflight), return_exceptions=True)
                return
            try:
                await live_requests()
            except Exception:
                # Redis cannot confirm caller liveness: cancel once and retain
                # each slot until cleanup. Never start more session work here.
                for task in list(inflight):
                    if task not in cancellation_requested:
                        cancellation_requested.add(task)
                        task.cancel()
                await asyncio.sleep(poll_interval)
                continue

            for request in list(queued):
                if request.expires_at <= time.time():
                    queued.remove(request)
                    await _reply(client, request, _wait("session_owner_request_expired", request.wait_phase, 15))
                elif time.monotonic() - request.queued_at >= queue_timeout:
                    queued.remove(request)
                    await _reply(client, request, _wait("session_owner_queue_timeout", request.wait_phase, 15))

            eligible = [item for item in queued if blocked_phase(item) is None]
            if eligible:
                protected = [item for item in eligible if item.stage in priority_stages]
                background = [item for item in eligible if item.stage not in priority_stages]
                choose_priority = bool(protected) and (not background or priority_streak < PRIORITY_BURST_LIMIT)
                candidates = protected if choose_priority else background
                # Rotate accounts as well as classes; a burst of one account's
                # different stages cannot crowd another account indefinitely.
                selected = next(
                    (item for item in candidates if item.account_id != last_account.get(choose_priority)), candidates[0]
                )
                queued.remove(selected)
                priority_streak = priority_streak + 1 if choose_priority else 0
                last_account[choose_priority] = selected.account_id
                busy_accounts.add(selected.account_id)
                task = asyncio.create_task(execute_and_reply(selected))
                inflight[task] = selected
                task.add_done_callback(completed)
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
                payload = json.loads(item)
                if not isinstance(payload, dict):
                    continue
                account_id = int(payload.get("account_id") or 0)
                stage = str(payload.get("stage") or "")
                request_id = str(payload.get("id") or "")
                deadline = float(payload.get("expires_at", time.time() + FORWARD_TIMEOUT_SECONDS))
                if not request_id or len(request_id) > 256 or len(stage) > 256 or not math.isfinite(deadline):
                    continue
            except (TypeError, ValueError):
                continue
            request = _Request(payload, account_id, stage, REPLY_PREFIX + request_id, deadline, time.monotonic())
            if request.active_key and not await client.get(request.active_key):
                continue
            if not account_id or not stage or not owns(account_id):
                await _reply(client, request, {"error": "not_owner"})
                continue
            if request.expires_at <= time.time():
                await _reply(client, request, _wait("session_owner_request_expired", "queue", 15))
                continue
            duplicates = any(
                entry.account_id == account_id and entry.stage == stage
                for entry in list(queued) + list(inflight.values())
            )
            if duplicates:
                await _reply(client, request, _wait("session_owner_duplicate_wait", "account_stage", 15))
                continue
            phase = blocked_phase(request)
            if len(queued) >= queue_limit or (phase and defer_if_busy and stage not in (queued_stages or ())):
                await _reply(client, request, _wait("session_owner_capacity_wait", phase or "queue_full"))
                continue
            request.wait_phase = phase or "queue"
            queued.append(request)
            if phase:
                logger.info(
                    "session_exec_request_queued",
                    stage=stage,
                    request_id=request_id,
                    wait_phase=phase,
                    queue_depth=len(queued),
                )
    finally:
        for request in queued:
            await _reply(client, request, _wait("session_owner_shutdown", "owner_shutdown", 15))
        queued.clear()
        tasks = list(inflight)
        for task in tasks:
            if task not in cancellation_requested:
                cancellation_requested.add(task)
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


async def _reply(client: Any, request: _Request, payload: dict[str, Any]) -> None:
    try:
        async with asyncio.timeout(CLEANUP_TIMEOUT_SECONDS):
            encoded = json.dumps(payload, default=str)
            if request.active_key:
                await client.eval(
                    _REPLY_IF_ACTIVE, 2, request.active_key, request.reply_key, encoded, REPLY_TTL_SECONDS
                )
            else:
                await client.lpush(request.reply_key, encoded)
                await client.expire(request.reply_key, REPLY_TTL_SECONDS)
    except Exception:
        logger.warning("session_exec_reply_failed", request_id=request.payload["id"])
