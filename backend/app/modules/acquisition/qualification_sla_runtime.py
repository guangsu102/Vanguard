"""Draft nonblocking SLA hooks; no business permission or budget operations.

The lifecycle starts this producer with a separate session from the existing
application session factory. No new engine or pool is created. Unconfigured
or interrupted transport never yields a passing SLO.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from contextlib import suppress
from datetime import datetime, timedelta
from functools import wraps
import hashlib
import json
import os
import time
import threading
from typing import Any, Callable
from uuid import uuid4

from app.modules.acquisition import qualification_ready_origin as ready_origin

STAGED = "qualification.ready.sla.staged.v1"
QUEUE_LIMIT = 256
WRITE_TIMEOUT = 0.5
_binding: ContextVar[dict | None] = ContextVar("qualification_ready_sla_binding", default=None)
_producer: Producer | None = None
_unconfigured_losses: dict[int, datetime] = {}
SOURCE_PREFIX = "qualification.ready.sla.source.v1."
SOURCE_LIMIT = 512
CENSUS_PREFIX = "qualification.ready.sla.scan.v1."
CENSUS_PAGE_SIZE = 16
CENSUS_WRITE_WORKS = 4
CENSUS_PRESSURE_WORKS = 1
CAPACITY_PRESSURE = "qualification.telemetry.capacity_pressure"
WRITE_BATCH_SIZE = 8
WRITE_ATTEMPTS = 2
CENSUS_WORK_LIMIT = 4096
CENSUS_CURSOR_LIMIT = 512
CENSUS_INTERVAL_SECONDS = 20
# One optional off-thread ring append per process/minute. No diagnostic is
# awaited by a writer or owns a database transaction.
WRITE_DIAGNOSTIC_INTERVAL_SECONDS = 60
_diagnostic_pid = os.getpid()
_diagnostic_last_at = 0.0
_diagnostic_thread = None
_diagnostic_schedule_lock = threading.Lock()
WRITE_DIAGNOSTIC_PHASES = {1: "session", 2: "statement_setup", 3: "measurement_lock",
    4: "lost_event_gap", 5: "census_page", 6: "business_event", 7: "source_coverage", 8: "commit"}
WRITE_DIAGNOSTIC_MODULES = {"app.modules.acquisition."+name: code for code, name in enumerate(
    ("qualification_sla_runtime", "qualification_sla", "qualification_cost", "qualification_ready_origin"), 201)}
WRITE_DIAGNOSTIC_FUNCTIONS = {name: code for code, name in enumerate(
    ("run", "_write", "_census", "_source_coverage", "observe_work", "resolve_event", "record_ready",
     "note_gap", "_get_or_create", "_capacity", "annotate_existing", "reconcile_pending"), 201)}
_staged_count = 0
_process_pid = os.getpid()
_global_loss_since: datetime | None = None


def enabled() -> bool:
    return os.getenv("QUALIFICATION_SLA_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def _best_effort(fallback=None):
    """Optional synchronous hooks cannot change a business call's outcome."""
    def decorate(fn):
        @wraps(fn)
        def guarded(*args, **kwargs):
            global _global_loss_since
            if not enabled():
                return fallback
            try:
                return fn(*args, **kwargs)
            except Exception:
                at = datetime.utcnow()
                _global_loss_since = min(_global_loss_since or at, at)
                _binding.set(None)
                return fallback
        return guarded
    return decorate


def process_source_status() -> dict:
    """Thread-safe scalar snapshot only; never touches a loop, DB or Redis."""
    value = _producer if _process_pid == os.getpid() else None
    losses = list(_unconfigured_losses.values()) if _process_pid == os.getpid() else []
    if _global_loss_since is not None and _process_pid == os.getpid():
        losses.append(_global_loss_since)
    if value is not None:
        losses.extend(value.lost.values())
    pending = _staged_count if _process_pid == os.getpid() else 0
    if value is not None:
        pending += value.queue.qsize() + value.in_flight
        pending += int(value.census_pending and not value.stopping)
    return {"pending_count": pending,
        "lost_since": min(losses).isoformat() if losses else None,
        "loop_active": value is not None and not value.stopping,
        "confirmed_through": (_now().isoformat() if not pending and not losses else
            value.confirmed_through.isoformat() if value and value.confirmed_through else None),
        "pid": os.getpid()}


def _now() -> datetime:
    return datetime.utcnow()


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else value if isinstance(value, str) else None


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _source_retirable(item: dict, now: datetime) -> bool:
    try:
        last = datetime.fromisoformat(item["last_seen_at"])
        if last < now-timedelta(days=7):
            return True
        closed = datetime.fromisoformat(item["closed_at"]) if item.get("closed_at") else None
        return (item.get("clean_closed") is True and item.get("status_valid") is True
            and type(item.get("pending_count")) is int and item["pending_count"] == 0
            and item.get("lost_since") is None and not item.get("history_loss")
            and item.get("gaps") == [] and closed is not None and closed <= last
            and last < now-timedelta(seconds=60))
    except (ValueError, TypeError, KeyError):
        return False


def actor_account() -> int | None:
    from app.core.account.session_exec import current_actor_account
    return current_actor_account()


@_best_effort()
def reference(work: Any) -> dict | None:
    """Reference a live episode only; this does not create historical evidence."""
    if work.ready_at is None or work.state not in {"READY", "WAIT_BUDGET", "WAIT_MATURITY", "RUNNING"}:
        return None
    event = {"account_id": work.account_id, "work_id": work.id, "revision": work.revision,
        "ready_sequence": 0, "ready_at": _iso(work.ready_at), "family": work.family,
        "scope_hash": _hash(work.scope_key), "origin_state": work.state,
        "membership_id": getattr(work, "membership_id", None),
        "membership_incarnation": _iso(getattr(work, "membership_joined_at", None)),
        "task_demand": {"work_kind": getattr(work, "kind", None),
            "policy_version": getattr(work, "policy_version", None),
            "profile_fingerprint": getattr(work, "profile_fingerprint", None),
            "task_complexity": "unknown", "complexity_source": "independent_whole_work_demand_unavailable"}}
    return ready_origin.enrich_reference(work, event)


def _lost(account: int, at: datetime) -> None:
    if not enabled():
        return
    values = _producer.lost if _producer is not None else _unconfigured_losses
    values[account] = min(values.get(account, at), at)


@_best_effort(False)
def emit(kind: str, event: dict) -> bool:
    """No await, SQL, lock, pool acquisition or external call on this path."""
    if not enabled():
        return False
    if _producer is None or getattr(_producer, "owner_pid", os.getpid()) != os.getpid():
        _lost(event["account_id"], _now())
        return False
    return _producer.offer(kind, event)


@_best_effort()
def stage(db: Any, kind: str, event: dict) -> None:
    global _staged_count
    if not enabled():
        return
    session = getattr(db, "sync_session", db)
    transaction = session.get_nested_transaction() or session.get_transaction()
    groups = session.info.setdefault(STAGED, {})
    values = groups.setdefault(transaction, [])
    if _staged_count >= QUEUE_LIMIT:
        _lost(event["account_id"], _now())
        return
    values.append((kind, dict(event)))
    _staged_count += 1


@_best_effort()
def stage_ready(db: Any, work: Any, *, proof: dict | None = None,
                origin_unknown: bool = False, dependency_ready_verified: str | None = None) -> None:
    if work.state not in {"READY", "WAIT_BUDGET", "WAIT_MATURITY"}:
        return
    event = reference(work)
    if event is not None:
        ready_origin.capture_ready(work, event, _now())
        event = reference(work) or event
        event["_ready_origin_work"] = work
        # Only called beside the actual READY assignment. The census separately
        # labels recovery rows unknown; it cannot invent this original event.
        if not event.get("ready_origin_epoch"):
            event.update(eligibility=dict(proof or {}), origin_unknown=origin_unknown or event.get("origin_unknown", False))
        if dependency_ready_verified == "provider_completed":
            event["dependency_ready_verified"] = dependency_ready_verified
        bound = _binding.get()
        if bound is not None and bound["work_id"] == work.id and bound["revision"] == work.revision:
            event["after_claim_token"] = bound.get("claim_token")
        stage(db, "ready", event)


@_best_effort()
def after_commit(session: Any) -> None:
    global _staged_count
    transaction = session.get_nested_transaction() or session.get_transaction()
    groups = session.info.get(STAGED, {})
    values = groups.pop(transaction, [])
    if session.in_nested_transaction():
        parent = transaction.parent
        groups.setdefault(parent, []).extend(values)
        return
    session.info.pop(STAGED, None)
    _staged_count = max(0, _staged_count-len(values))
    for kind, event in values:
        work = event.pop("_ready_origin_work", None)
        if work is not None:
            event = ready_origin.final_reference(work, event)
            if event is None:
                continue
        emit(kind, event)


@_best_effort()
def after_soft_rollback(session: Any, previous_transaction: Any) -> None:
    global _staged_count
    groups = session.info.get(STAGED, {})
    values = groups.pop(previous_transaction, [])
    _staged_count = max(0, _staged_count-len(values))
    if previous_transaction.parent is None:
        _staged_count = max(0, _staged_count-sum(len(group) for group in groups.values()))
        session.info.pop(STAGED, None)


@_best_effort()
def bind_claim(claim: Any, account_id: int):
    value = getattr(claim, "sla_episode", None)
    if value is not None:
        value = {**value, "claim_token": claim.token, "actor_account": account_id}
    return _binding.set(value)


@_best_effort()
def current_admission() -> dict | None:
    return (_binding.get() or {}).get("ready_origin_admission")


@_best_effort()
def reset_binding(token: Any) -> None:
    _binding.reset(token)


@_best_effort()
def observe_health(healthy: bool | None, observed_at: datetime) -> None:
    """Use an already loaded business-health decision; never do extra I/O."""
    value = _binding.get()
    if value is not None:
        _binding.set({**value, "health_verified": healthy is True,
            "health_observed_at": observed_at.isoformat()})


@_best_effort()
def observe_account_health(account: Any, service: Any) -> None:
    """Keep all optional health computation inside the isolation boundary."""
    from app.modules.acquisition.qualification_service import account_block_reason
    at = _now()
    healthy = None if account is None else not bool(account_block_reason(account, at)
        or service._join_review_account_block_reason(account, at))
    observe_health(healthy, at)


def _start_event(*, plan_fingerprint: str, budget_granted: bool,
                 explicit_local_zero_plan: bool, required_reads: int) -> dict | None:
    value = _binding.get()
    if value is None or actor_account() != value["account_id"] or value["actor_account"] != value["account_id"]:
        return None
    return {**value, "started_at": _now().isoformat(), "actor_verified": True,
        "step_entered": True, "budget_granted": budget_granted,
        "explicit_local_zero_plan": explicit_local_zero_plan, "plan_fingerprint": plan_fingerprint,
        "required_reads": required_reads, "budget_observed_at": _now().isoformat()}


@_best_effort(False)
def budget_step_entered(account_id: int, work: Any) -> bool:
    """Call only after existing Redis + durable budget reservation succeeded."""
    if not work.claimed or account_id != getattr(work, "account_id", None) or work.limit <= 0:
        return False
    value = _binding.get()
    if value is None or value["account_id"] != account_id:
        return False
    plan = _hash({"scope_hash": value["scope_hash"], "scope": work.scope, "kind": work.kind,
        "phase": work.phase, "required_reads": work.limit})
    event = _start_event(plan_fingerprint=plan, budget_granted=True, explicit_local_zero_plan=False,
        required_reads=work.limit)
    if event is not None:
        _binding.set({**value, "ready_origin_admission": event})
        event = {**event, "ready_origin_admission": event}
    return emit("started", event) if event else False


@_best_effort()
def stage_local_step(db: Any, *, plan_name: str) -> None:
    """A concrete local business-effect commit, never an implicit zero estimate."""
    if plan_name not in {"qualified_decision_commit"}:
        return
    event = _start_event(plan_fingerprint=_hash({"local_effect": plan_name, "required_reads": 0}),
        budget_granted=False, explicit_local_zero_plan=True, required_reads=0)
    if event:
        stage(db, "started", event)


@_best_effort()
def stage_finished(db: Any, *, outcome: str, reason: str) -> None:
    value = _binding.get()
    if value is not None and actor_account() == value["account_id"]:
        stage(db, "finished", {**value, "closed_at": _now().isoformat(),
            "outcome": outcome[:64], "reason": reason[:160]})


@_best_effort(False)
def request_census() -> bool:
    """Schedule the read census; the dispatcher never waits for its SQL."""
    return bool(_producer and _producer.offer("census", {}))


@_best_effort(False)
def slice_started(work: Any) -> bool:
    value = _binding.get()
    if value is None or value["account_id"] != work.account_id or actor_account() != work.account_id:
        return False
    work._sla_slice_started_at = _now().isoformat()
    return emit("slice_started", {**value, "slice_token": work.token,
        "slice_started_at": work._sla_slice_started_at, "slice_kind": work.kind,
        "phase": work.phase, "complexity": getattr(work, "measurement_complexity", "unknown")})


@_best_effort(False)
def slice_finished(work: Any, *, failed: bool) -> bool:
    value = _binding.get()
    if value is None or value["account_id"] != work.account_id or actor_account() != work.account_id:
        return False
    return emit("slice_finished", {**value, "slice_token": work.token,
        "slice_started_at": getattr(work, "_sla_slice_started_at", None),
        "slice_finished_at": _now().isoformat(), "slice_kind": work.kind,
        "phase": work.phase, "complexity": getattr(work, "measurement_complexity", "unknown"),
        "sdk_entered": work.entered_reads, "ledger_id": work.ledger_id, "failed": failed,
        "reservation_not_taken": not work.claimed and work.reads == 0 and work.entered_reads == 0})


def _schedule_write_diagnostic(error: BaseException, *, role: str, stage: int, phase: int, batch_size: int) -> None:
    """Submit numeric evidence only; diagnostic I/O cannot hold up the writer."""
    global _diagnostic_pid, _diagnostic_last_at, _diagnostic_thread, _diagnostic_schedule_lock
    try:
        if _diagnostic_pid != os.getpid():
            _diagnostic_pid, _diagnostic_last_at = os.getpid(), 0.0
            _diagnostic_thread = None
            _diagnostic_schedule_lock = threading.Lock()
        schedule_lock = _diagnostic_schedule_lock
        if not schedule_lock.acquire(blocking=False):
            return
        try:
            now = time.monotonic()
            if (_diagnostic_thread is not None and _diagnostic_thread.is_alive()
                    or _diagnostic_last_at and now-_diagnostic_last_at < WRITE_DIAGNOSTIC_INTERVAL_SECONDS):
                return
            from app.modules.acquisition import review_diagnostics as diagnostics
            reason = type(error).__name__
            reason = reason if reason in diagnostics.REASONS else "other"
            counts = {"stage_code": stage, "phase_code": phase, "count": batch_size,
                      "role_code": {"backend": 1, "growth": 2, "celery": 3}.get(role, 0)}
            import traceback
            frames = []
            for current in (error, error.__cause__):
                if current is None:
                    continue
                for frame, line in traceback.walk_tb(current.__traceback__):
                    module = WRITE_DIAGNOSTIC_MODULES.get(frame.f_globals.get("__name__"), 0)
                    if module:
                        frames.append({"module_code": module,
                            "function_code": WRITE_DIAGNOSTIC_FUNCTIONS.get(frame.f_code.co_name, 0), "line": int(line)})
            for index, frame in enumerate(frames[-8:]):
                counts.update({f"frame_{index}_{field}": value for field, value in frame.items()})
            origin = getattr(error, "orig", None)
            sqlstate = getattr(origin, "sqlstate", None) or getattr(error, "sqlstate", None)
            if isinstance(sqlstate, str) and len(sqlstate) == 5 and all(char in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ" for char in sqlstate):
                counts["sqlstate_code"] = int(sqlstate, 36)
            def append_once():
                try:
                    diagnostics.append("review_metrics_manager_health_failed", counts=counts, reason=reason)
                except Exception:
                    pass
            _diagnostic_thread = threading.Thread(target=append_once, name="qualification-sla-diag", daemon=True)
            _diagnostic_last_at = now
            _diagnostic_thread.start()
        finally:
            schedule_lock.release()
    except Exception:
        pass  # Optional diagnostics never replace failure or change coverage.


class Producer:
    def __init__(self, session_factory: Callable, *, role: str,
                 origin_proof: Callable | None = None,
                 expected_source_counts: dict[str, int] | None = None,
                 source_provider: Callable | None = None, source_identity: dict | None = None,
                 own_source_provider: Callable | None = None):
        self.session_factory = session_factory
        self.role = role
        self.origin_proof = origin_proof
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_LIMIT)
        self.lost = dict(_unconfigured_losses)
        self.task: asyncio.Task | None = None
        self.accounts: set[int] = set()
        self.stopping = False
        self.owner_pid = os.getpid()
        self.in_flight = 0
        self.in_flight_business = 0
        self.queued_business = 0
        self.confirmed_through: datetime | None = None
        self.started_at = _now()
        self.generation = uuid4().hex
        self.source_provider = source_provider
        self.own_source_provider = own_source_provider
        self.source_identity = source_identity or {}
        self.generation = self.source_identity.get("generation", self.generation)
        if self.source_identity.get("started_at"):
            self.started_at = datetime.fromisoformat(self.source_identity["started_at"])
        self.expected_source_counts = expected_source_counts
        census_role = os.getenv("QUALIFICATION_SLA_CENSUS_ROLE", "").strip()
        self.census_owner = (bool(census_role) and census_role == self.role
            and isinstance(expected_source_counts, dict) and expected_source_counts.get(self.role) == 1)
        self.last_heartbeat = datetime.min
        self.last_heartbeat_attempt = datetime.min
        self.census_at: datetime | None = None
        self.census_accounts: set[int] = set()
        self.census_pending = False
        self.census_cursor: dict | None = None
        self._census_queued = False
        self._census_requested_at = datetime.min
        self._write_phase = 0

    def offer(self, kind: str, event: dict) -> bool:
        if kind == "census" and not self.census_owner:
            return False
        if kind == "census" and self._census_queued:
            return True
        try:
            self.queue.put_nowait((kind, dict(event)))
            if kind == "census":
                self._census_queued = True
                self._census_requested_at = _now()
            else:
                self.queued_business += 1
            if "account_id" in event:
                self.accounts.add(event["account_id"])
            return True
        except asyncio.QueueFull:
            for account in ({event["account_id"]} if "account_id" in event else self.accounts):
                self.lost[account] = min(self.lost.get(account, _now()), _now())
            return False

    async def _census(self, db: Any) -> dict:
        """One keyset page; the caller applies memory progress after commit.

        Stable role/cohort keys let successive prefork task loops (or another
        child holding the same account measurement locks) resume a pass. A
        completed pass deletes its cursor in the same transaction.
        """
        from sqlalchemy import select, func
        from app.core.settings_models import SystemSetting
        from app.modules.acquisition.models import QualificationWork
        from app.modules.acquisition.qualification_engine import enabled_account_ids
        from app.modules.acquisition import qualification_sla as ledger
        from app.modules.acquisition import qualification_cost as costs
        cohort = tuple(sorted(set(enabled_account_ids())))
        self.accounts.update(cohort)
        now = _now()
        if self.census_cursor and self.census_cursor.get("phase") == "scan" and self.census_cursor.get("cohort") != list(cohort):
            old = await db.get(SystemSetting, CENSUS_PREFIX+self.role+"."+_hash(tuple(self.census_cursor["cohort"])))
            if old is not None:
                # The write path already owns locks for previously discovered
                # accounts as well as the new cohort. Preserve interrupted
                # coverage before retiring this superseded pending pass.
                since = datetime.fromisoformat(self.census_cursor["started_at"])
                for account in sorted(set(self.census_cursor["cohort"]) | set(cohort)):
                    await ledger.note_gap(db, account, now, "census_cohort_changed", since=since)
                await db.delete(old)
        key = CENSUS_PREFIX+self.role+"."+_hash(cohort)
        cursor = await db.get(SystemSetting, key)
        saved = json.loads(cursor.value or "{}") if cursor is not None else {}
        predicates = (
            QualificationWork.account_id.in_(cohort),
            QualificationWork.state.in_(("READY", "WAIT_BUDGET", "WAIT_MATURITY", "RUNNING")),
            QualificationWork.ready_at.is_not(None),
        )
        if not saved:
            # Only pending passes occupy cursor capacity; completed passes
            # leave no generation-shaped history behind.
            keys = (await db.scalars(select(SystemSetting.key).where(
                SystemSetting.key.startswith(CENSUS_PREFIX)).limit(CENSUS_CURSOR_LIMIT+1))).all()
            if cursor is None and len(keys) >= CENSUS_CURSOR_LIMIT:
                for account in cohort:
                    await ledger.note_gap(db, account, now, "cursor_capacity_exhausted")
                return {"phase": "capped", "cohort": list(cohort), "after_id": 0,
                    "completed_at": None, "reason": "census_cursor_capacity_exhausted"}
            upper = await db.scalar(select(func.max(QualificationWork.id)).where(*predicates))
            saved = {"cohort": list(cohort), "after_id": 0, "upper_id": upper or 0,
                "observed_count": 0, "started_at": now.isoformat()}
        if saved.get("cohort") != list(cohort):
            raise ValueError("census_cursor_cohort_invalid")
        if (type(saved.get("after_id")) is not int or type(saved.get("upper_id")) is not int
                or type(saved.get("observed_count")) is not int
                or not 0 <= saved["after_id"] <= saved["upper_id"]
                or not 0 <= saved["observed_count"] <= CENSUS_WORK_LIMIT):
            raise ValueError("census_cursor_invalid")
        # A missing original event must remain unknown. Fresh snapshots at census
        # time are only diagnostic and cannot replace the original READY proof.
        rows = (await db.scalars(select(QualificationWork).where(
            *predicates, QualificationWork.id > saved["after_id"],
            QualificationWork.id <= saved["upper_id"],
        ).order_by(QualificationWork.id).limit(min(CENSUS_PAGE_SIZE, CENSUS_WRITE_WORKS)))).all()
        allowed = max(0, CENSUS_WORK_LIMIT-saved["observed_count"])
        observed = rows[:allowed]
        processed = []
        for work in observed:
            processed.append(work)
            event = reference(work)
            if event is not None:
                await costs.observe_work(db, event, _now())
                if work.state != "RUNNING" or event.get("ready_origin_epoch"):
                    # The durable timer/READY time is preserved, even if it became
                    # due while this process was offline. Eligibility stays unknown.
                    if not event.get("ready_origin_epoch"):
                        event.update(origin_unknown=True, recovered_from_persisted_time=True, recovery_only=True)
                    canonical = await ledger.resolve_event(db, "ready", event, _now())
                    if canonical is not None:
                        await ledger.record_ready(db, canonical, _now())
                        from app.core.settings_models import SystemSetting
                        await ready_origin.annotate_existing(db, canonical, ledger._key(canonical), SystemSetting)
            if db.info.get(CAPACITY_PRESSURE) and len(processed) >= CENSUS_PRESSURE_WORKS:
                break
        observed = processed
        after = observed[-1].id if observed else saved["after_id"]
        remaining = await db.scalar(select(QualificationWork.id).where(
            *predicates, QualificationWork.id > after,
            QualificationWork.id <= saved["upper_id"],
        ).order_by(QualificationWork.id).limit(1))
        count = saved["observed_count"]+len(observed)
        capped = remaining is not None and count >= CENSUS_WORK_LIMIT
        complete = remaining is None and not capped
        if capped:
            for account in cohort:
                await ledger.note_gap(db, account, _now(), "census_truncated")
        result = {**saved, "after_id": after, "observed_count": count,
            "last_page_at": _now().isoformat(),
            "phase": "complete" if complete else "capped" if capped else "scan",
            "completed_at": _now().isoformat() if complete else None}
        if complete or capped:
            if cursor is not None:
                await db.delete(cursor)
            if complete:
                await costs.reconcile_pending(db, set(cohort), _now(), limit=16)
        else:
            if cursor is None:
                cursor = SystemSetting(key=key, value="{}")
                db.add(cursor)
            cursor.value = json.dumps(result, sort_keys=True)
        await db.flush()
        return result

    async def _replace_source(self, db: Any, row: Any, raw: str, *, own: bool) -> None:
        from sqlalchemy import update
        from sqlalchemy.orm.attributes import set_committed_value
        from app.core.settings_models import SystemSetting
        before = row.value
        if before == raw:
            return
        result = await db.execute(update(SystemSetting).where(
            SystemSetting.key == row.key, SystemSetting.value == before
        ).values(value=raw).execution_options(synchronize_session=False))
        if result.rowcount == 1:
            set_committed_value(row, "value", raw)
        elif own:
            # Never confirm a batch whose own source marker did not commit.
            raise ValueError("source_marker_changed")
        else:
            # A newer process-local marker wins over the owner's registry read.
            await db.refresh(row)

    async def _source_coverage(self, db: Any, now: datetime, *, closing: bool = False,
                               census_at: datetime | None = None,
                               census_accounts: set[int] | None = None) -> None:
        from sqlalchemy import select, func, delete, text
        from app.core.settings_models import SystemSetting
        from app.modules.acquisition import qualification_sla as ledger
        key = SOURCE_PREFIX + self.role + "." + self.generation
        source = await db.get(SystemSetting, key)
        previous_source = json.loads(source.value or "{}") if source is not None else {}
        snapshot = {"complete": False, "sources": [], "reason": "source_provider_unconfigured"}
        if self.census_owner and self.source_provider is not None:
            try:
                snapshot = await self.source_provider()
            except Exception:
                snapshot = {"complete": False, "sources": [], "reason": "source_provider_failed"}
        elif not self.census_owner and self.own_source_provider is not None:
            try:
                own = self.own_source_provider()
                if (isinstance(own, dict) and own.get("generation") == self.generation
                        and own.get("role") == self.role):
                    snapshot = {"complete": False, "sources": [own], "reason": "own_source_only"}
            except Exception:
                pass  # Missing local proof remains pending, never a complete inventory.
        rows = []
        if self.census_owner:
            rows = (await db.scalars(select(SystemSetting).where(
                SystemSetting.key.startswith(SOURCE_PREFIX)).limit(SOURCE_LIMIT + 1))).all()
            retired = []
            for row in rows:
                item = json.loads(row.value or "{}")
                if row.key != key and _source_retirable(item, now) and len(retired) < 128:
                    removed = await db.execute(delete(SystemSetting).where(
                        SystemSetting.key == row.key, SystemSetting.value == row.value
                    ).execution_options(synchronize_session=False))
                    if removed.rowcount == 1:
                        retired.append(row)
                    else:
                        await db.refresh(row)
            rows = [row for row in rows if row not in retired]
        # Only a new generation pays a key-count admission read. Steady nonowners
        # neither read global source bodies nor edit another generation's facts.
        source_count = len(rows) if self.census_owner else (await db.scalar(
            select(func.count()).select_from(SystemSetting).where(SystemSetting.key.startswith(SOURCE_PREFIX))
        ) if source is None else 0)
        if source is None and source_count >= SOURCE_LIMIT:
            for account in sorted(self.accounts):
                # Empty nonowner heartbeats normally need no account lock, but
                # this exceptional branch writes shared account coverage.
                if (not self.census_owner and getattr(db.bind, "dialect", None) is not None
                        and db.bind.dialect.name == "postgresql"):
                    await db.execute(text("SELECT pg_advisory_xact_lock(572910,:account)"), {"account": account})
                await ledger.note_gap(db, account, now, "source_capacity_exceeded")
            return
        # Current batch is written atomically beside this source marker; queue
        # and staged events that arrived while writing remain pending.
        census_waiting = self.census_pending and census_at is None
        value = {"role": self.role, "generation": self.generation, "pid": self.owner_pid,
            "started_at": self.started_at.isoformat(), "last_seen_at": now.isoformat(),
            "pending_count": self.queue.qsize()+_staged_count+int(census_waiting),
            "lost_since": min(self.lost.values()).isoformat() if self.lost else None,
            "confirmed_through": now.isoformat() if self.queue.empty() and not _staged_count and not census_waiting else
                self.confirmed_through.isoformat() if self.confirmed_through else None,
            "loop_active": not closing,
            # A Celery task loop can close while its prefork process remains
            # registered and idle. Only the process registry closes that source.
            "clean_closed": False, "closed_at": None}
        own_registry = next((item for item in snapshot.get("sources", []) if item.get("generation") == self.generation), {})
        value["gaps"] = own_registry.get("gaps", previous_source.get("gaps", []))
        value["history_loss"] = bool(own_registry.get("history_loss") or previous_source.get("history_loss"))
        if not self.census_owner:
            # The local process snapshot includes Redis transport/fatal evidence;
            # its loss cannot be cleared by an empty async queue.
            value["status_valid"] = own_registry.get("status_valid") is True
            value["last_seen_at"] = own_registry.get("last_seen_at", value["last_seen_at"])
            local_pending = own_registry.get("pending_count")
            value["pending_count"] = max(value["pending_count"],
                local_pending if type(local_pending) is int and local_pending >= 0 else 1)
            local_confirmed = own_registry.get("confirmed_through")
            value["confirmed_through"] = (min(value["confirmed_through"], local_confirmed)
                if value["confirmed_through"] and local_confirmed else None)
            local_loss = own_registry.get("lost_since")
            if local_loss:
                value["lost_since"] = min(value["lost_since"] or local_loss, local_loss)
            if not own_registry or not value["status_valid"] or value["history_loss"]:
                value["pending_count"] = max(1, value["pending_count"])
                value["confirmed_through"] = previous_source.get("confirmed_through")
                value["lost_since"] = value["lost_since"] or previous_source.get("lost_since") or now.isoformat()
        source_was_new = source is None
        if source is None:
            source = SystemSetting(key=key, value="{}")
            db.add(source)
            rows.append(source)
        if source_was_new:
            source.value = json.dumps(value, sort_keys=True)
        else:
            await self._replace_source(db, source, json.dumps(value, sort_keys=True), own=True)
        if not self.census_owner:
            return
        # Persist the complete idle-process registry as evidence, not just the
        # PIDs that happened to send events. Timestamp order prevents an older
        # registry read from replacing a newer generation heartbeat.
        source_by_key = {row.key: row for row in rows}
        malformed = len(rows) > SOURCE_LIMIT
        for item in snapshot.get("sources", [])[:SOURCE_LIMIT]:
            if not isinstance(item, dict) or not item.get("role") or not item.get("generation"):
                malformed = True
                continue
            if _source_retirable(item, now):
                continue  # Do not re-add a clean/expired registry tombstone.
            item_key = SOURCE_PREFIX+item["role"]+"."+item["generation"]
            if item_key == key:
                continue
            other = source_by_key.get(item_key)
            other_was_new = other is None
            if other is None:
                if len(source_by_key) >= SOURCE_LIMIT:
                    malformed = True
                    break
                other = SystemSetting(key=item_key, value="{}")
                db.add(other)
                rows.append(other)
                source_by_key[item_key] = other
            old = json.loads(other.value or "{}")
            if str(item.get("last_seen_at", "")) > str(old.get("last_seen_at", "")):
                if other_was_new:
                    other.value = json.dumps(item, sort_keys=True)
                else:
                    await self._replace_source(db, other, json.dumps(item, sort_keys=True), own=False)
        states = [json.loads(row.value or "{}") for row in rows]
        def date(item, name):
            raw = item.get(name)
            return datetime.fromisoformat(raw) if raw else None
        active = {}
        gap_ranges = []
        for item in states:
            last = date(item, "last_seen_at")
            if item.get("clean_closed") or last is None:
                continue
            if 0 <= (now-last).total_seconds() <= 60:
                active[item.get("role")] = active.get(item.get("role"), 0) + 1
            else:
                replacements = [date(candidate, "started_at") for candidate in states
                    if candidate.get("role") == item.get("role") and date(candidate, "started_at")
                    and date(candidate, "started_at") > last]
                gap_ranges.append((last, min(replacements, default=now)))
        inventory_ok = (snapshot.get("complete") is True and not malformed
            and bool(self.expected_source_counts) and active == self.expected_source_counts)
        active_states = [item for item in states if not item.get("clean_closed")
            and date(item, "last_seen_at") and 0 <= (now-date(item, "last_seen_at")).total_seconds() <= 60]
        confirmed = [date(item, "confirmed_through") for item in active_states]
        confirmed_through = min(confirmed) if confirmed and all(item is not None and item <= now for item in confirmed) else None
        for account in sorted(self.accounts):
            marker = await db.get(SystemSetting, ledger.COVERAGE_PREFIX + str(account))
            coverage = json.loads(marker.value or "{}") if marker else {}
            if marker is None:
                marker = SystemSetting(key=ledger.COVERAGE_PREFIX + str(account), value="{}")
                db.add(marker)
            coverage.setdefault("started_at", now.isoformat())
            coverage["source_inventory_verified"] = inventory_ok
            # Source/queue watermarks do not prove the frontier of business
            # transactions that have not staged their READY event yet.
            coverage["transaction_frontier_verified"] = False
            coverage["complete_override"] = inventory_ok
            coverage["source_observed_at"] = now.isoformat()
            coverage["source_confirmed_through"] = confirmed_through.isoformat() if confirmed_through else None
            coverage["source_inventory_reason"] = snapshot.get("reason")
            coverage["source_pending_events"] = sum(item.get("pending_count", 0) for item in states
                if not item.get("clean_closed") and date(item, "last_seen_at")
                and 0 <= (now-date(item, "last_seen_at")).total_seconds() <= 60)
            coverage["expected_source_counts"] = self.expected_source_counts
            coverage["active_source_counts"] = active
            if not inventory_ok:
                coverage.setdefault("inventory_missing_since", now.isoformat())
            missing_since = date(coverage, "inventory_missing_since") if inventory_ok else None
            if missing_since is not None:
                coverage.pop("inventory_missing_since", None)
            marker.value = json.dumps(coverage, sort_keys=True)
            await db.flush()  # note_gap must see the same newly created marker.
            for item in states:
                if item.get("history_loss"):
                    # Heartbeat timestamps are not new loss facts. Persist the
                    # acknowledgement beside the account gap, atomically; no
                    # process-local cache can survive a rolled-back append.
                    signature = _hash([item.get("generation"), item.get("lost_since"), item.get("gaps", []), "history_loss"])
                    coverage = json.loads(marker.value or "{}")
                    seen = dict(coverage.get("source_history_loss_seen", {}))
                    generation = str(item.get("generation", ""))
                    if seen.get(generation) != signature:
                        if generation in seen or len(seen) < SOURCE_LIMIT:
                            await ledger.note_gap(db, account, now, "transport_event_lost")
                            coverage = json.loads(marker.value or "{}")
                            seen[generation] = signature
                            coverage["source_history_loss_seen"] = seen
                        else:
                            # Never evict historical signatures to make room.
                            # Source facts remain intact and overflow stays unknown.
                            coverage["source_history_loss_tracking_incomplete"] = True
                            coverage["denominator_loss"] = True
                        marker.value = json.dumps(coverage, sort_keys=True)
                for gap in item.get("gaps", []):
                    start, end = date(gap, "start"), date(gap, "end")
                    if start is not None and end is not None and start <= end <= now:
                        signature = _hash([item.get("generation"), start, end, "producer_transport_gap"])
                        coverage = json.loads(marker.value or "{}")
                        seen = coverage.get("source_gaps_seen", [])
                        if signature not in seen:
                            await ledger.note_gap(db, account, end, "producer_transport_gap", since=start)
                            coverage = json.loads(marker.value or "{}")
                            coverage["source_gaps_seen"] = [*seen, signature][-SOURCE_LIMIT:]
                            if len(seen) >= SOURCE_LIMIT:
                                coverage["denominator_loss"] = True
                            marker.value = json.dumps(coverage, sort_keys=True)
            for item in active_states:
                lost_at = date(item, "lost_since")
                if lost_at is not None:
                    await ledger.note_gap(db, account, now, "producer_unconfirmed_loss", since=lost_at)
            if missing_since is not None:
                await ledger.note_gap(db, account, now, "source_inventory_missing", since=missing_since)
            completed_scan = census_at or self.census_at
            scanned_accounts = census_accounts if census_accounts is not None else self.census_accounts
            if completed_scan is not None and account in scanned_accounts:
                previous = date(coverage, "last_scan_at")
                if previous is not None and (completed_scan-previous).total_seconds() > 120:
                    await ledger.note_gap(db, account, completed_scan, "census_gap", since=previous)
                coverage = json.loads(marker.value or "{}")
                coverage["last_scan_at"] = completed_scan.isoformat()
                marker.value = json.dumps(coverage, sort_keys=True)
            for start, end in gap_ranges:
                coverage = json.loads(marker.value or "{}")
                signature = _hash([start, end, "producer_source_expired"])
                seen = coverage.get("source_gaps_seen", [])
                if signature not in seen:
                    await ledger.note_gap(db, account, end, "producer_source_expired", since=start)
                    coverage = json.loads(marker.value or "{}")
                    coverage["source_gaps_seen"] = [*seen, signature][-SOURCE_LIMIT:]
                    if len(seen) >= SOURCE_LIMIT:
                        coverage["unknown_until"] = (now+timedelta(days=1)).isoformat()
                    marker.value = json.dumps(coverage, sort_keys=True)

    async def _write(self, batch: list) -> None:
        self._write_phase = 1
        global _global_loss_since
        from app.modules.acquisition import qualification_sla as ledger
        from app.modules.acquisition import qualification_cost as costs
        from sqlalchemy import text
        losses = dict(self.lost)
        global_loss = _global_loss_since
        census_page = None
        if self.census_owner and any(kind == "census" for kind, _ in batch):
            self.census_pending = True
            self._census_queued = False
        async with self.session_factory() as db:
            self._write_phase = 2
            # Pressure is only a work-size hint for this independent write, never
            # cached capacity evidence or progress across a transaction boundary.
            db.info.pop(CAPACITY_PRESSURE, None)
            if getattr(db.bind, "dialect", None) is not None and db.bind.dialect.name == "postgresql":
                await db.execute(text("SET LOCAL statement_timeout='300ms'"))
            # This lock belongs exclusively to measurement records, and is never
            # acquired by a business actor or across Telegram/AI waits.
            # An empty nonowner heartbeat writes only its own source row.
            # Real batches or any loss keep the original account-lock scope.
            own_heartbeat = not self.census_owner and not batch and not losses and global_loss is None
            cohort = set() if own_heartbeat else set(self.accounts)
            if self.census_owner and any(kind == "census" for kind, _ in batch):
                from app.modules.acquisition.qualification_engine import enabled_account_ids
                cohort.update(enabled_account_ids())
            accounts = sorted({event["account_id"] for _, event in batch if "account_id" in event} | set(self.lost) | cohort)
            for account in accounts:
                self._write_phase = 3
                if getattr(db.bind, "dialect", None) is not None and db.bind.dialect.name == "postgresql":
                    await db.execute(text("SELECT pg_advisory_xact_lock(572910,:account)"), {"account": account})
                self._write_phase = 4
                if account in losses:
                    await ledger.note_gap(db, account, _now(), "transport_event_lost", since=losses[account])
                if global_loss is not None:
                    await ledger.note_gap(db, account, _now(), "transport_event_lost", since=global_loss)
            for kind, event in batch:
                self._write_phase = 6
                if kind == "census":
                    if self.census_owner and census_page is None:
                        self._write_phase = 5
                        census_page = await self._census(db)
                elif kind == "ready":
                    # Origin proof is optional and must satisfy the ledger's
                    # original-time freshness/identity check. It is never taken
                    # from successful later budget admission.
                    if self.origin_proof is not None and not event.get("origin_unknown") and not event.get("eligibility"):
                        event = {**event, "eligibility": await self.origin_proof(db, event)}
                    canonical = await ledger.resolve_event(db, kind, event, _now())
                    if canonical is not None:
                        await ledger.record_ready(db, canonical, _now())
                        from app.core.settings_models import SystemSetting
                        await ready_origin.annotate_existing(db, canonical, ledger._key(canonical), SystemSetting)
                    await costs.observe_work(db, event, _now())
                elif kind == "started":
                    canonical = await ledger.resolve_event(db, kind, event, _now())
                    if canonical is not None:
                        await ledger.record_started(db, canonical, _now())
                        from app.core.settings_models import SystemSetting
                        await ready_origin.annotate_existing(db, canonical, ledger._key(canonical), SystemSetting)
                elif kind == "finished":
                    canonical = await ledger.resolve_event(db, kind, event, _now())
                    if canonical is not None:
                        await ledger.record_finished(db, canonical, _now())
                    await costs.refresh_business_state(db, event, _now())
                elif kind == "slice_started":
                    await costs.record_started(db, event, _now())
                elif kind == "slice_finished":
                    await costs.record_finished(db, event, _now())
                    await costs.refresh_business_state(db, event, _now())
            census_at = datetime.fromisoformat(census_page["completed_at"]) if census_page and census_page.get("completed_at") else None
            self._write_phase = 7
            await self._source_coverage(db, _now(), closing=self.stopping and self.queue.empty(),
                census_at=census_at,
                census_accounts=set(census_page["cohort"]) if census_at is not None else None)
            self._write_phase = 8
            await db.commit()
        if census_page is not None:
            self.census_cursor = census_page
            self.census_pending = census_page["phase"] == "scan"
            if census_page.get("completed_at"):
                self.census_at = datetime.fromisoformat(census_page["completed_at"])
                self.census_accounts = set(census_page["cohort"])
        if self.census_pending and not self.stopping:
            self.offer("census", {})
        for account in accounts:
            if self.lost.get(account) == losses.get(account):
                self.lost.pop(account, None)
            if _unconfigured_losses.get(account) == losses.get(account):
                _unconfigured_losses.pop(account, None)
        self.last_heartbeat = _now()
        self.confirmed_through = self.last_heartbeat
        if accounts and _global_loss_since == global_loss:
            _global_loss_since = None

    async def run(self) -> None:
        while not self.stopping or not self.queue.empty():
            # The dispatcher lives in Celery, which is deliberately not the
            # census owner. The long-lived owner must refresh its own census
            # after the startup pass and retry interrupted passes locally.
            if (self.census_owner and not self.stopping and not self._census_queued
                    and not self.queue.full()
                    and (_now()-self._census_requested_at).total_seconds() >= CENSUS_INTERVAL_SECONDS):
                self.offer("census", {})
            try:
                first = await asyncio.wait_for(self.queue.get(), timeout=0.05)
            except TimeoutError:
                if (_now()-max(self.last_heartbeat, self.last_heartbeat_attempt)).total_seconds() >= 20:
                    # Failure does not advance success/confirmed watermarks, but
                    # cannot turn the 50ms queue poll into a global-write storm.
                    self.last_heartbeat_attempt = _now()
                    try:
                        async with asyncio.timeout(WRITE_TIMEOUT):
                            await self._write([])
                    except Exception as exc:
                        _schedule_write_diagnostic(exc, role=self.role, stage=31, phase=self._write_phase, batch_size=0)
                        for account in self.accounts:
                            self.lost.setdefault(account, _now())
                continue
            batch = [first]
            while len(batch) < WRITE_BATCH_SIZE:
                try:
                    batch.append(self.queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            self.in_flight = len(batch)
            self.in_flight_business = sum(kind != "census" for kind, _ in batch)
            self.queued_business = max(0, self.queued_business-self.in_flight_business)
            try:
                for attempt in range(WRITE_ATTEMPTS):
                    try:
                        async with asyncio.timeout(WRITE_TIMEOUT):
                            await self._write(batch)
                        break
                    except Exception:
                        if self.stopping or attempt+1 >= WRITE_ATTEMPTS:
                            raise
                        # Keep the failed atomic batch in flight and ahead of
                        # newer events. One bounded retry uses a fresh session;
                        # source pending cannot confirm an uncommitted batch.
                        await asyncio.sleep(0.05)
            except BaseException as exc:
                if not isinstance(exc, asyncio.CancelledError):
                    _schedule_write_diagnostic(exc, role=self.role,
                        stage=32 if any(kind == "census" for kind, _ in batch) else 33,
                        phase=self._write_phase, batch_size=len(batch))
                for _, event in batch:
                    if "account_id" in event:
                        account = event["account_id"]
                        self.lost[account] = min(self.lost.get(account, _now()), _now())
                if isinstance(exc, asyncio.CancelledError):
                    raise
            finally:
                self.in_flight = 0
                self.in_flight_business = 0
                for _ in batch:
                    self.queue.task_done()
        try:
            async with asyncio.timeout(WRITE_TIMEOUT):
                await self._write([])
        except Exception as exc:
            _schedule_write_diagnostic(exc, role=self.role, stage=34, phase=self._write_phase, batch_size=0)
            if self.queued_business or self.in_flight_business or _staged_count:
                for account in self.accounts:
                    _unconfigured_losses[account] = _now()
            for account, lost_at in self.lost.items():
                _unconfigured_losses[account] = min(_unconfigured_losses.get(account, lost_at), lost_at)


def start_producer(session_factory: Callable, *, role: str,
                   origin_proof: Callable | None = None,
                   expected_source_counts: dict[str, int] | None = None,
                   source_provider: Callable | None = None, source_identity: dict | None = None,
                   own_source_provider: Callable | None = None) -> Producer:
    """Configure outside actor; use independent sessions from existing factory."""
    global _producer, _process_pid, _staged_count, _global_loss_since
    if _process_pid != os.getpid():
        _producer = None
        _staged_count = 0
        _unconfigured_losses.clear()
        _global_loss_since = None
        _process_pid = os.getpid()
    if _producer is not None:
        return _producer
    value = Producer(session_factory, role=role, origin_proof=origin_proof,
        expected_source_counts=expected_source_counts, source_provider=source_provider,
        source_identity=source_identity, own_source_provider=own_source_provider)
    _producer = value
    value.task = asyncio.create_task(value.run(), name="qualification-sla-writer")
    return value


async def stop_producer() -> None:
    global _producer
    value = _producer
    if value is None:
        return
    value.stopping = True
    if value.task:
        try:
            async with asyncio.timeout(WRITE_TIMEOUT + 0.1):
                await value.task
        except TimeoutError:
            value.task.cancel()
            if value.queued_business or value.in_flight_business or _staged_count:
                for account in value.accounts:
                    _unconfigured_losses[account] = _now()
            for account, lost_at in value.lost.items():
                _unconfigured_losses[account] = min(_unconfigured_losses.get(account, lost_at), lost_at)
            with suppress(asyncio.CancelledError, TimeoutError):
                async with asyncio.timeout(.2):
                    await value.task
    if _producer is value:
        _producer = None


def install_transaction_hooks() -> None:
    from sqlalchemy import event
    from sqlalchemy.orm import Session
    if not event.contains(Session, "after_commit", after_commit):
        event.listen(Session, "after_commit", after_commit)
        event.listen(Session, "after_soft_rollback", after_soft_rollback)


install_transaction_hooks()
