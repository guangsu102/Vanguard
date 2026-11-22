"""Optional measurement lifecycle; never initialize application resources.

The idle source heartbeat owns one synchronous Redis connection in its own
thread. Database writing lives only in the application's current async loop
and borrows sessions from its already configured factory. No business actor
waits for measurement I/O.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
import json
import os
import re
import threading
from typing import Any, Callable
from uuid import uuid4

REGISTRY_KEY = "qualification:ready:sla:sources:v1"
REGISTRY_ERROR_KEY = REGISTRY_KEY + ":error"
MAX_SOURCES = 512
MAX_SOURCE_GAPS = 128
HEARTBEAT_SECONDS = 20
SOCKET_TIMEOUT = .2
ROLE_RE = re.compile(r"[a-z][a-z0-9_.:-]{0,63}\Z")
_lifecycle_failure_since: datetime | None = None
_lifecycle_failure_pid: int | None = None

PUBLISH_LUA = """
local present = redis.call('HEXISTS', KEYS[1], ARGV[1])
if present == 0 and redis.call('HLEN', KEYS[1]) >= tonumber(ARGV[3]) then
  local items = redis.call('HGETALL', KEYS[1])
  local removed = 0
  for i = 1, #items, 2 do
    local ok, item = pcall(cjson.decode, items[i+1])
    local epoch = ok and tonumber(item.last_seen_epoch)
    local clean = ok and item.clean_closed == true and item.status_valid == true
      and item.pending_count == 0 and (item.lost_since == nil or item.lost_since == cjson.null)
      and item.history_loss ~= true and type(item.gaps) == 'table' and next(item.gaps) == nil
      and type(item.closed_at) == 'string' and epoch and epoch < tonumber(ARGV[5])-60
    if ok and epoch and (epoch < tonumber(ARGV[4]) or clean) and removed < 128 then
      redis.call('HDEL', KEYS[1], items[i])
      removed = removed + 1
    end
  end
end
if present == 0 and redis.call('HLEN', KEYS[1]) >= tonumber(ARGV[3]) then
  redis.call('SET', KEYS[2], 'source_registry_capacity_exceeded')
  return 0
end
redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
return 1
"""

SNAPSHOT_LUA = """
local failure = redis.call('GET', KEYS[2])
if failure then return {0, failure} end
if redis.call('HLEN', KEYS[1]) > tonumber(ARGV[1]) then return {0, 'source_registry_capacity_exceeded'} end
return {1, redis.call('HVALS', KEYS[1])}
"""


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _date(value: Any) -> datetime | None:
    try:
        value = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
    except (ValueError, TypeError, AttributeError):
        return None


def _mark_fatal() -> None:
    global _lifecycle_failure_since, _lifecycle_failure_pid
    try:
        at, pid = datetime.now(timezone.utc).replace(tzinfo=None), os.getpid()
        _lifecycle_failure_since = min(_lifecycle_failure_since or at, at)
        _lifecycle_failure_pid = pid
        if _process is not None and _process.pid == pid:
            _process.fatal_since = _process.fatal_since or at
    except Exception:
        pass


def _best_effort(fallback=None):
    def decorate(fn):
        @wraps(fn)
        def guarded(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception:
                _mark_fatal()
                return fallback
        return guarded
    return decorate


@_best_effort(False)
def enabled() -> bool:
    return os.getenv("QUALIFICATION_SLA_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


@_best_effort()
def get_expected_sources() -> dict[str, int] | None:
    try:
        value = json.loads(os.getenv("QUALIFICATION_SLA_EXPECTED_SOURCES", ""))
    except (ValueError, TypeError):
        return None
    if not isinstance(value, dict) or not value or any(
        not isinstance(role, str) or not ROLE_RE.fullmatch(role)
        or type(count) is not int or not 1 <= count <= MAX_SOURCES
        for role, count in value.items()
    ) or sum(value.values()) > MAX_SOURCES:
        return None
    return value


@_best_effort()
def configured_role(default: str = "celery") -> str | None:
    value = os.getenv("QUALIFICATION_SLA_SOURCE_ROLE", os.getenv("QUALIFICATION_SLA_ROLE", default)).strip()
    return value if ROLE_RE.fullmatch(value) else None


def _runtime_module():
    from app.modules.acquisition import qualification_sla_runtime
    return qualification_sla_runtime


def _database_factory():
    from app.core import database
    return database.async_session_factory


@asynccontextmanager
async def existing_sessions():
    """Resolve lazily so Celery task initialization can happen after start."""
    factory = _database_factory()
    if factory is None:
        raise RuntimeError("qualification_sla_database_not_initialized")
    async with factory() as db:
        yield db


def _new_client():
    import redis
    from redis.backoff import NoBackoff
    from redis.retry import Retry
    from app.core.config import settings
    return redis.Redis.from_url(settings.REDIS_URL, password=settings.effective_redis_password,
        decode_responses=True, max_connections=1, socket_connect_timeout=SOCKET_TIMEOUT,
        socket_timeout=SOCKET_TIMEOUT, retry_on_timeout=False, retry=Retry(NoBackoff(), 0),
        health_check_interval=0)


async def _current_redis():
    from app.core.redis import get_redis
    return await get_redis()


def _runtime_status() -> dict:
    try:
        provider = getattr(_runtime_module(), "process_source_status", None)
        value = provider() if provider is not None else {}
        pending = value.get("pending_count")
        if type(pending) is not int or pending < 0:
            raise ValueError("source_status_invalid")
        return {**value, "status_valid": value.get("status_valid", True) is True}
    except Exception:
        return {"pending_count": 1, "lost_since": _now().isoformat(),
            "loop_active": True, "status_valid": False}


@dataclass
class ProcessSource:
    role: str
    pid: int
    generation: str
    started_at: datetime
    status_provider: Callable[[], dict]
    halt: threading.Event
    thread: Any = None
    failure_since: datetime | None = None
    fatal_since: datetime | None = None
    gaps: list[dict] = field(default_factory=list)
    history_loss: bool = False

    def snapshot(self, *, closing: bool = False, transport_recovered_at: datetime | None = None) -> dict:
        try:
            status = dict(self.status_provider())
        except Exception:
            status = {"pending_count": 1, "loop_active": True, "status_valid": False}
        now = _now()
        pending = status.get("pending_count")
        valid = status.get("status_valid", True) is True and type(pending) is int and pending >= 0
        confirmed = _date(status.get("confirmed_through"))
        valid = valid and confirmed is not None and confirmed <= now
        lost = _date(status.get("lost_since"))
        if status.get("lost_since") is not None and lost is None:
            valid = False
        if self.failure_since is not None and transport_recovered_at is None:
            lost = min(lost or self.failure_since, self.failure_since)
        gaps = list(self.gaps)
        if self.failure_since is not None and transport_recovered_at is not None:
            gaps.append({"start": self.failure_since.isoformat(), "end": transport_recovered_at.isoformat(),
                "reason": "source_heartbeat_transport_failed"})
        history_loss = self.history_loss or len(gaps) > MAX_SOURCE_GAPS
        valid = valid and self.fatal_since is None and not history_loss
        if self.fatal_since is not None:
            lost = min(lost or self.fatal_since, self.fatal_since)
        active = status.get("loop_active") is not False
        return {"role": self.role, "pid": self.pid, "generation": self.generation,
            "started_at": self.started_at.isoformat(), "last_seen_at": now.isoformat(),
            "last_seen_epoch": now.replace(tzinfo=timezone.utc).timestamp(),
            "pending_count": pending if valid else 1, "lost_since": lost.isoformat() if lost else None,
            "loop_active": active, "status_valid": valid,
            "confirmed_through": confirmed.isoformat() if confirmed else None,
            "gaps": gaps[-MAX_SOURCE_GAPS:], "history_loss": history_loss,
            "clean_closed": closing and valid and not active and pending == 0 and lost is None,
            "closed_at": now.isoformat() if closing else None}

    def pulse(self, client: Any, *, closing: bool = False) -> bool:
        recovered_at = _now() if self.failure_since is not None else None
        value = self.snapshot(closing=closing, transport_recovered_at=recovered_at)
        result = client.eval(PUBLISH_LUA, 2, REGISTRY_KEY, REGISTRY_ERROR_KEY,
            self.generation, json.dumps(value, sort_keys=True), MAX_SOURCES,
            value["last_seen_epoch"]-7*86400, value["last_seen_epoch"])
        if result == 1 and recovered_at is not None:
            # Recovery evidence and removal of the open failure are published
            # in the same atomic registry update. A failed write keeps both.
            self.gaps = value["gaps"]
            self.history_loss = value["history_loss"]
            self.failure_since = None
        return result == 1

    def serve(self) -> None:
        client = None
        try:
            while not self.halt.is_set():
                try:
                    if client is None:
                        client = _new_client()
                    if not self.pulse(client):
                        self.failure_since = self.failure_since or _now()
                except Exception:
                    self.failure_since = self.failure_since or _now()
                self.halt.wait(HEARTBEAT_SECONDS)
        except Exception:
            self.failure_since = self.failure_since or _now()
        finally:
            if client is not None:
                with suppress(Exception):
                    self.pulse(client, closing=True)
                with suppress(Exception):
                    client.close()


@dataclass
class LoopSource:
    loop: asyncio.AbstractEventLoop
    role: str
    bootstrap: asyncio.Task | None = None
    producer_started: bool = False
    stopping: bool = False


_process: ProcessSource | None = None
_loop: LoopSource | None = None


@_best_effort()
def declare_process(role: str = "celery", *, status_provider: Callable[[], dict] | None = None) -> dict | None:
    """Called in each prefork child, including one that never receives a task."""
    global _process, _loop, _lifecycle_failure_since, _lifecycle_failure_pid
    if not enabled():
        return None
    role = configured_role(role)
    if role is None:
        return None
    pid = os.getpid()
    if _lifecycle_failure_pid is not None and _lifecycle_failure_pid != pid:
        _lifecycle_failure_since, _lifecycle_failure_pid = None, None
    if _lifecycle_failure_since is not None:
        return None
    if _process is not None and _process.pid != pid:
        # Discard inherited references without touching the parent's thread or
        # clients. New resources are created only inside this child's thread.
        _process, _loop = None, None
    if _process is not None and _process.halt.is_set():
        if _process.thread is not None and _process.thread.is_alive():
            return None
        _process = None
    if _process is not None:
        if _process.fatal_since is not None:
            return None
        if _process.role != role:
            _process.fatal_since = _process.fatal_since or _now()
            return None
        if status_provider is not None:
            _process.status_provider = status_provider
        return source_identity()
    value = ProcessSource(role, pid, str(pid)+"-"+uuid4().hex, _now(),
        status_provider or _runtime_status, threading.Event())
    _process = value
    value.thread = threading.Thread(target=value.serve, name="qualification-sla-source", daemon=True)
    value.thread.start()
    return source_identity()


@_best_effort()
def source_identity() -> dict | None:
    value = _process
    if value is None or value.pid != os.getpid():
        return None
    return {"role": value.role, "pid": value.pid, "generation": value.generation,
        "started_at": value.started_at.isoformat()}


def own_source_provider() -> dict:
    """Process-local evidence only; never Redis, SQL or another source."""
    value = _process
    if value is None or value.pid != os.getpid():
        return {}
    return value.snapshot()


async def source_provider() -> dict:
    """Use only this loop's existing application Redis client; no new pool."""
    observed = _now()
    failed = {"complete": False, "sources": [], "observed_at": observed.isoformat()}
    try:
        async with asyncio.timeout(.3):
            cache = await _current_redis()
            result = await cache.eval(SNAPSHOT_LUA, 2, REGISTRY_KEY, REGISTRY_ERROR_KEY, MAX_SOURCES)
        observed = _now()
        if not isinstance(result, (list, tuple)) or len(result) != 2 or result[0] != 1:
            return {**failed, "reason": "source_registry_unavailable"}
        if not isinstance(result[1], (list, tuple)) or len(result[1]) > MAX_SOURCES:
            return {**failed, "reason": "source_registry_capacity_exceeded"}
        rows, identities = [], set()
        for raw in result[1]:
            row = json.loads(raw)
            role, generation = row.get("role"), row.get("generation")
            first, last = _date(row.get("started_at")), _date(row.get("last_seen_at"))
            if (not isinstance(role, str) or not ROLE_RE.fullmatch(role)
                or not isinstance(generation, str) or not 1 <= len(generation) <= 128
                or generation in identities
                or first is None or last is None or first > last or last > observed
                or (row.get("lost_since") is not None and _date(row["lost_since"]) is None)):
                return {**failed, "reason": "source_registry_invalid"}
            gaps = row.get("gaps", [])
            if not isinstance(gaps, list) or len(gaps) > MAX_SOURCE_GAPS or any(
                not isinstance(gap, dict) or _date(gap.get("start")) is None or _date(gap.get("end")) is None
                or not _date(gap["start"]) <= _date(gap["end"]) <= last for gap in gaps
            ):
                return {**failed, "reason": "source_registry_invalid"}
            identities.add(generation)
            rows.append(row)
        active = [row for row in rows if not row.get("clean_closed")
            and 0 <= (observed-_date(row["last_seen_at"])).total_seconds() <= 60]
        valid = all(row.get("status_valid") is True and not row.get("history_loss")
            and type(row.get("pending_count")) is int and row["pending_count"] >= 0
            and _date(row.get("confirmed_through")) is not None
            and _date(row["confirmed_through"]) <= _date(row["last_seen_at"]) for row in active)
        local_failure = (_lifecycle_failure_pid == os.getpid() and _lifecycle_failure_since is not None
            or _process is not None and _process.pid == os.getpid() and _process.fatal_since is not None)
        valid = valid and not local_failure
        minimum = min((_date(row["confirmed_through"]) for row in active), default=None) if valid else None
        return {"complete": valid, "sources": rows, "observed_at": observed.isoformat(),
            "min_confirmed_through": minimum.isoformat() if minimum else None,
            **({"reason": "local_source_lifecycle_failure" if local_failure else "active_source_status_invalid"} if not valid else {})}
    except Exception:
        return {**failed, "reason": "source_registry_unavailable"}


def _activate(value: LoopSource) -> None:
    runtime = _runtime_module()
    runtime.start_producer(existing_sessions, role=value.role,
        expected_source_counts=get_expected_sources(), source_provider=source_provider,
        source_identity=source_identity(), own_source_provider=own_source_provider)
    value.producer_started = True
    runtime.request_census()


async def _bootstrap(value: LoopSource) -> None:
    try:
        while not value.stopping:
            if _database_factory() is not None:
                _activate(value)
                return
            await asyncio.sleep(.05)
    except asyncio.CancelledError:
        raise
    except Exception:
        if _process is not None:
            _process.fatal_since = _process.fatal_since or _now()


async def start(role: str = "celery") -> bool:
    """Schedule measurement without delaying application initialization."""
    global _loop
    if not enabled():
        return False
    try:
        identity = declare_process(role)
        if identity is None:
            return False
        current = asyncio.get_running_loop()
        if _loop is not None:
            if _loop.loop is current:
                return True
            if _process is not None:
                _process.fatal_since = _process.fatal_since or _now()
            return False  # Never reuse an async producer from another loop.
        value = LoopSource(current, identity["role"])
        _loop = value
        if _database_factory() is not None:
            _activate(value)
        else:
            value.bootstrap = asyncio.create_task(_bootstrap(value), name="qualification-sla-bootstrap")
        return True
    except Exception:
        if _process is not None:
            _process.fatal_since = _process.fatal_since or _now()
        return False


async def start_current_process() -> bool:
    """Database initialization fallback; an undeclared process stays unknown."""
    identity = source_identity()
    return await start(identity["role"]) if identity is not None else False


async def stop(*, close_process: bool = False) -> None:
    global _loop
    value = _loop
    try:
        if value is not None and value.loop is asyncio.get_running_loop():
            value.stopping = True
            if value.bootstrap is not None:
                value.bootstrap.cancel()
                with suppress(asyncio.CancelledError):
                    await value.bootstrap
            if value.producer_started:
                await _runtime_module().stop_producer()
            _loop = None
    except Exception:
        if _process is not None:
            _process.fatal_since = _process.fatal_since or _now()
    finally:
        if close_process:
            process_shutdown()


@_best_effort()
def process_shutdown(**_kwargs) -> None:
    """Bounded sync prefork shutdown; never run an async engine in a thread."""
    global _process
    value = _process
    if value is None or value.pid != os.getpid():
        return
    value.halt.set()
    if value.thread is not None:
        value.thread.join(timeout=.5)
    # An unresponsive heartbeat stays discoverable as an expired source. Do
    # not start a second thread/client while the first one still owns it.
    if value.thread is None or not value.thread.is_alive():
        _process = None


@_best_effort()
def celery_process_init(**_kwargs) -> None:
    declare_process("celery")


@_best_effort()
def install_celery_signals() -> None:
    from celery.signals import worker_process_init, worker_process_shutdown
    worker_process_init.connect(celery_process_init, weak=False,
        dispatch_uid="qualification-sla-source-init-v1")
    worker_process_shutdown.connect(process_shutdown, weak=False,
        dispatch_uid="qualification-sla-source-shutdown-v1")
