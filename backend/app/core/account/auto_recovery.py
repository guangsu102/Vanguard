"""Bounded ERROR recovery driven by persisted, generation-bound failures.

The worker scans only its existing ERROR inventory.  No ordinary connection
gate is relaxed: a due retry invokes the same fenced identity verification as
an explicit recovery, on one background task, without a database transaction
held during the network probe.
"""
from __future__ import annotations

import asyncio
import json
import math
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import func, select

from app.core.account.models import AccountEnvironmentEvent, AccountStatus, TelegramAccount
from app.core.account.recovery import (
    RECOVERY_RETRYABLE_REASONS,
    RECOVERY_WAIT_REASONS,
    AccountRecoveryService,
    RecoveryResult,
)
from app.core.database import get_db_session

logger = structlog.get_logger()

RETRY_DELAYS = (60, 300, 900)
MAX_ATTEMPTS = len(RETRY_DELAYS)
MAX_WAIT_RESULTS = 48
EPISODE_SECONDS = 24 * 60 * 60
WAIT_SECONDS = 5 * 60
PROBE_TIMEOUT_SECONDS = 140
LOCK_SECONDS = 240
STATE_PREFIX = "vanguard:account:auto-recovery:state:"
LOCK_PREFIX = "vanguard:account:auto-recovery:lock:"
AUTOMATIC_SOURCE = "automatic_error_recovery"

RETRYABLE_REASONS = RECOVERY_RETRYABLE_REASONS
WAIT_REASONS = RECOVERY_WAIT_REASONS

_WRITE_IF_OWNED = """
-- auto_recovery_write_v1
if redis.call('get', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('set', KEYS[2], ARGV[2])
return 1
"""
_RELEASE_IF_OWNED = """
-- auto_recovery_release_v1
if redis.call('get', KEYS[1]) ~= ARGV[1] then return 0 end
return redis.call('del', KEYS[1])
"""


def _iso_timestamp(value: float | None) -> str | None:
    return datetime.fromtimestamp(value, UTC).isoformat() if value else None


def _reason(value: Any) -> str:
    # No exception text, session strings or proxy credentials enter heartbeat.
    return value if isinstance(value, str) and value.replace("_", "").isalnum() and len(value) <= 64 else "verification_failed"


@dataclass(frozen=True)
class FailureTrigger:
    account_id: int
    event_id: int
    reason: str
    generation: str
    error_updated_at: str
    quarantine_token: str | None
    blocked_reason: str | None = None


async def load_failure_triggers(accounts: list[TelegramAccount]) -> dict[int, FailureTrigger]:
    """One batched, closed read transaction; stale/manual ERRORs stay manual."""
    if not accounts:
        return {}
    ids = [account.id for account in accounts]
    latest = select(func.max(AccountEnvironmentEvent.id)).where(
        AccountEnvironmentEvent.account_id.in_(ids),
        AccountEnvironmentEvent.event_type.in_(("account_connection_failure", "session_recovery")),
    ).group_by(AccountEnvironmentEvent.account_id, AccountEnvironmentEvent.event_type)
    async with get_db_session() as db:
        rows = (await db.execute(select(
            AccountEnvironmentEvent.id, AccountEnvironmentEvent.account_id,
            AccountEnvironmentEvent.event_type, AccountEnvironmentEvent.status,
            AccountEnvironmentEvent.reason, AccountEnvironmentEvent.details,
        ).where(AccountEnvironmentEvent.id.in_(latest)))).all()
    failures, recovery = {}, {}
    for row in rows:
        (failures if row.event_type == "account_connection_failure" else recovery)[row.account_id] = row
    triggers: dict[int, FailureTrigger] = {}
    for account in accounts:
        event = failures.get(account.id)
        if event is None or event.status != "failed":
            continue
        try:
            details = json.loads(event.details or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(details, dict) or details.get("source") != "account_pool":
            continue
        updated = getattr(account, "updated_at", None)
        if updated is None or details.get("error_updated_at") != updated.isoformat():
            continue
        # A later failed identity/authorization check must not be overruled by
        # the older transport error which originally made this a candidate.
        checked = recovery.get(account.id)
        blocked_reason = None if event.reason in RETRYABLE_REASONS else _reason(event.reason)
        if checked is not None and checked.id > event.id:
            try:
                checked_details = json.loads(checked.details or "{}")
            except (ValueError, TypeError):
                checked_details = {}
            automatic_cancel = (checked.reason == "verification_cancelled"
                and isinstance(checked_details, dict) and checked_details.get("source") == AUTOMATIC_SOURCE)
            if checked.status == "ok":
                blocked_reason = "unclassified_or_stale_error"
            elif not automatic_cancel and checked.reason not in RETRYABLE_REASONS | WAIT_REASONS:
                blocked_reason = _reason(checked.reason)
        token = details.get("quarantine_token")
        if token is not None and (not isinstance(token, str) or len(token) != 32
                                  or any(char not in "0123456789abcdef" for char in token)):
            continue
        triggers[account.id] = FailureTrigger(
            account.id, event.id, event.reason, token or str(event.id), updated.isoformat(), token, blocked_reason,
        )
    return triggers


class RecoveryStateUnavailable(RuntimeError):
    pass


class AutoRecoveryStore:
    """Two fixed keys per affected account; state has no expiry or reset timer."""
    def __init__(self, redis: Any) -> None:
        self.redis = redis

    async def acquire(self, account_id: int, token: str) -> bool:
        if self.redis is None:
            raise RecoveryStateUnavailable()
        return bool(await self.redis.set(f"{LOCK_PREFIX}{account_id}", token, nx=True, ex=LOCK_SECONDS))

    async def read(self, account_id: int) -> dict[str, Any] | None:
        raw = await self.redis.get(f"{STATE_PREFIX}{account_id}")
        if raw is None:
            return None
        try:
            if len(raw) > 1024:
                raise ValueError()
            value = json.loads(raw)
            if (not isinstance(value, dict) or value.get("v") != 1
                    or not isinstance(value.get("generation"), str)
                    or not isinstance(value.get("event_id"), int)
                    or not isinstance(value.get("attempts"), int) or not 0 <= value["attempts"] <= MAX_ATTEMPTS
                    or not isinstance(value.get("waits"), int) or not 0 <= value["waits"] <= MAX_WAIT_RESULTS
                    or not isinstance(value.get("started_at"), (int, float))
                    or not isinstance(value.get("next_at"), (int, float))
                    or not math.isfinite(value["started_at"]) or value["started_at"] < 0
                    or not math.isfinite(value["next_at"]) or value["next_at"] < 0
                    or value["next_at"] > value["started_at"] + 2 * EPISODE_SECONDS
                    or value.get("state") not in {"waiting", "inflight", "recovered", "exhausted", "manual_required"}):
                raise ValueError()
            return value
        except (ValueError, TypeError, UnicodeError):
            # A corrupt counter is not permission to start again from zero.
            raise RecoveryStateUnavailable() from None

    async def write(self, account_id: int, token: str, state: dict[str, Any]) -> None:
        value = json.dumps(state, separators=(",", ":"), sort_keys=True)
        if len(value) > 1024 or not await self.redis.eval(
            _WRITE_IF_OWNED, 2, f"{LOCK_PREFIX}{account_id}", f"{STATE_PREFIX}{account_id}", token, value,
        ):
            raise RecoveryStateUnavailable()

    async def release(self, account_id: int, token: str) -> None:
        await self.redis.eval(_RELEASE_IF_OWNED, 1, f"{LOCK_PREFIX}{account_id}", token)


def initial_state(trigger: FailureTrigger, now: float) -> dict[str, Any]:
    return {
        "v": 1, "generation": trigger.generation, "event_id": trigger.event_id,
        "attempts": 0, "waits": 0, "started_at": now, "next_at": now + RETRY_DELAYS[0],
        "state": "waiting", "reason": trigger.reason,
    }


def state_report(account_id: int, state: dict[str, Any]) -> dict[str, Any]:
    return {
        "account_id": account_id, "state": state["state"], "reason": _reason(state.get("reason")),
        "attempts": state["attempts"], "attempt_limit": MAX_ATTEMPTS,
        "waits": state["waits"], "next_attempt_at": _iso_timestamp(state["next_at"]),
        "manual_required": state["state"] in {"exhausted", "manual_required"},
    }


class AccountAutoRecovery:
    """One background probe at a time, while worker heartbeats keep running."""
    def __init__(self, pool: Any, *, redis: Any = None, clock: Any = time.time) -> None:
        self.pool, self._redis, self.clock = pool, redis, clock
        self._task: asyncio.Task[None] | None = None
        self._reports: dict[int, dict[str, Any]] = {}

    def snapshot(self) -> dict[str, Any]:
        return {
            "in_progress": bool(self._task and not self._task.done()),
            "accounts": list(self._reports.values()),
            "attempt_limit": MAX_ATTEMPTS,
            "retry_delays_seconds": list(RETRY_DELAYS),
        }

    def _store(self) -> AutoRecoveryStore:
        from app.core.redis import redis_client
        return AutoRecoveryStore(self._redis if self._redis is not None else redis_client)

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def tick(self, accounts: list[TelegramAccount]) -> dict[str, Any]:
        if self._task is not None and self._task.done():
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        candidates = [account for account in accounts if (
            account.status == AccountStatus.ERROR and account.is_active
            and bool(getattr(getattr(account, "operation_config", None), "enabled", False))
        )]
        candidate_ids = {account.id for account in candidates}
        self._reports = {key: value for key, value in self._reports.items() if key in candidate_ids}
        triggers = await load_failure_triggers(candidates)
        for account in candidates:
            trigger = triggers.get(account.id)
            if trigger is None:
                self._reports[account.id] = {"account_id": account.id, "state": "manual_required",
                    "reason": "unclassified_or_stale_error", "manual_required": True,
                    "next_attempt_at": None, "attempt_limit": MAX_ATTEMPTS}
                continue
            if trigger.blocked_reason:
                self._reports[account.id] = {"account_id": account.id, "state": "manual_required",
                    "reason": trigger.blocked_reason, "manual_required": True,
                    "next_attempt_at": None, "attempt_limit": MAX_ATTEMPTS}
                continue
            store, token = self._store(), secrets.token_hex(16)
            try:
                if not await store.acquire(account.id, token):
                    continue
                state = await store.read(account.id)
                if state is None or (state["generation"] != trigger.generation and state["event_id"] < trigger.event_id):
                    state = initial_state(trigger, self.clock())
                    await store.write(account.id, token, state)
                if state["generation"] != trigger.generation:
                    raise RecoveryStateUnavailable()
                now = self.clock()
                if state["state"] not in {"recovered", "exhausted", "manual_required"}:
                    if (state["attempts"] >= MAX_ATTEMPTS or state["waits"] >= MAX_WAIT_RESULTS
                            or now - state["started_at"] >= EPISODE_SECONDS):
                        state.update(state="exhausted", reason="automatic_recovery_exhausted", next_at=0)
                        await store.write(account.id, token, state)
                    elif state["next_at"] <= now and self._task is None:
                        # Reserve before network I/O. Cancellation, a crash or
                        # a Redis outage after the probe cannot erase the try.
                        state.update(state="inflight", attempts=state["attempts"] + 1,
                                     next_at=now + RETRY_DELAYS[min(state["attempts"] + 1, MAX_ATTEMPTS - 1)])
                        await store.write(account.id, token, state)
                        self._reports[account.id] = state_report(account.id, state)
                        self._task = asyncio.create_task(self._attempt(trigger, state, store, token))
                        token = ""  # The background attempt owns this lock.
                self._reports[account.id] = state_report(account.id, state)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._reports[account.id] = {"account_id": account.id, "state": "manual_required",
                    "reason": "recovery_state_unavailable", "manual_required": True,
                    "next_attempt_at": None, "attempt_limit": MAX_ATTEMPTS}
            finally:
                if token:
                    try:
                        await store.release(account.id, token)
                    except Exception:
                        pass
        return self.snapshot()

    async def _verify(self, trigger: FailureTrigger) -> RecoveryResult:
        async with get_db_session() as db:
            return await AccountRecoveryService(db, self.pool).recover(
                trigger.account_id, source=AUTOMATIC_SOURCE,
                expected_error_updated_at=trigger.error_updated_at, expected_failure_id=trigger.event_id,
            )

    async def _attempt(self, trigger: FailureTrigger, state: dict[str, Any], store: AutoRecoveryStore, token: str) -> None:
        try:
            try:
                async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
                    result = await self._verify(trigger)
            except TimeoutError:
                result = RecoveryResult(False, "connection_timeout")
            now = self.clock()
            reason = _reason(result.reason)
            if result.ok:
                state.update(state="recovered", reason="identity_verified", next_at=0)
            elif reason in WAIT_REASONS:
                # Only a known, returned non-attempt refunds the reservation.
                # Process death/cancellation leaves the reservation consumed.
                retry_after = getattr(result, "retry_after_seconds", None)
                if not isinstance(retry_after, (int, float)) or not math.isfinite(retry_after):
                    retry_after = 0
                delay = max(WAIT_SECONDS, min(EPISODE_SECONDS, retry_after or 0))
                state.update(state="waiting", reason=reason, attempts=state["attempts"] - 1,
                             waits=state["waits"] + 1, next_at=now + delay)
            elif reason in RETRYABLE_REASONS:
                state.update(state="waiting", reason=reason,
                             next_at=now + RETRY_DELAYS[min(state["attempts"], MAX_ATTEMPTS - 1)])
            else:
                state.update(state="manual_required", reason=reason, next_at=0)
            if (not result.ok and (state["attempts"] >= MAX_ATTEMPTS or state["waits"] >= MAX_WAIT_RESULTS
                                  or now - state["started_at"] >= EPISODE_SECONDS)):
                state.update(state="exhausted", reason="automatic_recovery_exhausted", next_at=0)
            await store.write(trigger.account_id, token, state)
            self._reports[trigger.account_id] = state_report(trigger.account_id, state)
            logger.info("account_automatic_recovery_result", **self._reports[trigger.account_id])
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # The pre-reserved state remains authoritative on a failed write.
            logger.warning("account_automatic_recovery_unavailable", account_id=trigger.account_id,
                           error_type=type(error).__name__)
        finally:
            try:
                await store.release(trigger.account_id, token)
            except Exception:
                pass
