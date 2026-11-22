"""Fixed-size, process-shared read measurements, independent of budget quotas.

Only numeric aggregates are persisted. No messages, request IDs, credentials,
WAL, temporary copies or unbounded labels. Interrupted writes mark their hour
incomplete; a missing/incomplete source must never be presented as zero use.
"""

from __future__ import annotations

import asyncio
import contextlib
import mmap
import os
import secrets
import struct
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HOURS = 168
ACCOUNT_SLOTS = 8
STAGES = ("baseline", "phase1", "phase2", "unknown")
PURPOSES = ("review", "renewal", "advertising", "retention", "join", "sync", "listener", "reconciliation", "other")
METHODS = ("sync", "identity", "history", "entity", "other")
RPC_STATES = ("attempted", "entered_sdk", "success", "error")
PHASES = ("first", "continuation", "maturity", "event", "ai_wait", "budget_wait", "technical_wait", "other")
OUTCOMES = ("allowed", "trial", "observe", "wait", "reject", "protected", "technical_wait", "unknown", "interrupted", "other")
COMPLEXITIES = ("simple", "standard", "complex", "unknown")
REASONS = ("same_work", "resume", "push", "fast_path", "failure_backoff")
CACHE_GATE_REASONS = ("scope", "ingest", "sdk_state", "protocol", "pending_facts", "unavailable", "mismatch", "save_rejected", "hit")
CACHE_GATE_AT = 327
OPT_STATES = ("avoided_reads", "used", "fallback", "degraded")
METRICS = 336
HEADER_BYTES = 4096
HOUR_BYTES = (ACCOUNT_SLOTS + 1) * len(STAGES) * METRICS * 8
FILE_BYTES = HEADER_BYTES + HOURS * HOUR_BYTES
HARD_LIMIT_BYTES = 16 * 1024 * 1024
FILENAME = "review-read-metrics-v1.bin"
MAGIC = b"VGRMET1\0"
ACCOUNTS_AT, HOURS_AT = 64, 128
SEMANTICS_AT = 56  # Reserved v1 header double; monotone SDK-entry-v2 cutover.
BUSINESS_SEMANTICS_AT = 1648  # Transition-v2 fence; v1 used 1640. Same file size.
ATTRIBUTION_VERSION = "purpose_v2_lane_first"
COMPLETION_VERSION = "business_transition_v2"
GAPS_AT = HOURS_AT + HOURS * 8
PROCESS_AT, PROCESS_WIDTH, PROCESS_SLOTS = 2048, 32, 32
ROLES = ("backend", "growth", "celery", "reconciliation", "resource", "guardian")
ROLE_AT = 3072
EXTRA_ROLE_AT = 3280  # Preserve original role IDs/timestamps and diagnostic offsets.
SOURCE_LEASE_SECONDS = 120
# Reserved v1 header space; existing counters, offsets and file size stay intact.
GAP_REASONS = ("unclassified", "dirty_recovery", "source_expired", "source_slots_full",
               "semaphore_timeout", "io_timeout", "io_error", "cancelled_pending", "batch_overflow")
GAP_REASON_AT = 3104
LIFECYCLE_AT = 3184
LIFECYCLE_FIELDS = ("registered", "closed", "expired", "closed_pending")
WRITE_LATENCY_AT = 3224
WRITE_LATENCY_BOUNDS_MS = (5, 20, 100, 500, 2000)
BATCH_MAX_ROWS = 64
BATCH_QUEUE_LIMIT = 256
BATCH_DELAY_SECONDS = 0.005
_PROCESS_SEED = secrets.randbits(63) or 1
U64_MAX = (1 << 64) - 1
RPC_COUNT = len(PURPOSES) * len(METHODS) * len(RPC_STATES)  # 180
PENDING_RPC_LIMIT = 128
WORK_WIDTH = 56
OPT_AT = RPC_COUNT + 2 * WORK_WIDTH  # 292
assert FILE_BYTES <= HARD_LIMIT_BYTES
assert OPT_AT + len(REASONS) * len(OPT_STATES) <= METRICS
assert EXTRA_ROLE_AT + (len(ROLES) - 3) * 8 <= HEADER_BYTES


def _role_offset(index: int) -> int:
    return ROLE_AT + index * 8 if index < 3 else EXTRA_ROLE_AT + (index - 3) * 8


def _u64(data: Any, offset: int) -> int:
    return struct.unpack_from("<Q", data, offset)[0]


def _put(data: Any, offset: int, value: int) -> None:
    struct.pack_into("<Q", data, offset, value)


@contextlib.contextmanager
def _lock(fd: int):
    until = time.monotonic() + 0.25
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= until:
                    raise TimeoutError("metrics_lock_timeout") from None
                time.sleep(0.005)
        try:
            yield
        finally:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= until:
                    raise TimeoutError("metrics_lock_timeout") from None
                time.sleep(0.005)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)


class FixedReadMetrics:
    def __init__(self, directory: str | Path, *, role: str = "", expected_roles: tuple[str, ...] = ()):
        self.directory = Path(directory)
        if not self.directory.is_absolute():
            raise ValueError("metrics_path_not_absolute")
        if role not in (*ROLES, "") or any(value not in ROLES for value in expected_roles):
            raise ValueError("metrics_role_invalid")
        self.role = role
        self.expected_roles = tuple(dict.fromkeys(expected_roles))

    @contextlib.contextmanager
    def _open(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.is_symlink() or any(p.name != FILENAME for p in self.directory.iterdir()):
            raise ValueError("metrics_directory_not_dedicated")
        path = self.directory / FILENAME
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags, 0o600)
        try:
            with _lock(fd):
                info = os.fstat(fd)
                if info.st_nlink != 1:
                    raise ValueError("metrics_file_has_other_links")
                if info.st_size == 0:
                    if hasattr(os, "posix_fallocate"):
                        os.posix_fallocate(fd, 0, FILE_BYTES)
                    else:
                        os.ftruncate(fd, FILE_BYTES)
                    os.lseek(fd, 0, os.SEEK_SET)
                    os.write(fd, MAGIC + struct.pack("<Q", 1) + struct.pack("<d", time.time()))
                    os.fsync(fd)
                if os.fstat(fd).st_size != FILE_BYTES:
                    raise ValueError("metrics_file_size_invalid")
                with mmap.mmap(fd, FILE_BYTES) as data:
                    if data[:8] != MAGIC or _u64(data, 8) != 1:
                        raise ValueError("metrics_format_invalid")
                    if struct.unpack_from("<d", data, SEMANTICS_AT)[0] == 0:
                        # Preserve legacy aggregates but never call their old
                        # entry semantics a complete v2 measurement window.
                        struct.pack_into("<d", data, SEMANTICS_AT, time.time())
                        data.flush(0, HEADER_BYTES)
                    if struct.unpack_from("<d", data, BUSINESS_SEMANTICS_AT)[0] == 0:
                        struct.pack_into("<d", data, BUSINESS_SEMANTICS_AT, time.time())
                        data.flush(0, HEADER_BYTES)
                    # A previous process died inside its durable transaction.
                    dirty = _u64(data, 24)
                    if dirty:
                        self._gap(data, (dirty - 1) * 3600, time.time(), "dirty_recovery")
                        _put(data, 24, 0)
                        data.flush()
                    yield data
        finally:
            os.close(fd)

    @staticmethod
    def _slot(data: Any, account_id: int, *, allocate: bool) -> int | None:
        accounts = [_u64(data, ACCOUNTS_AT + index * 8) for index in range(ACCOUNT_SLOTS)]
        if account_id in accounts:
            return accounts.index(account_id)
        if allocate and 0 in accounts and account_id > 0:
            index = accounts.index(0)
            _put(data, ACCOUNTS_AT + index * 8, account_id)
            return index
        return ACCOUNT_SLOTS if allocate else None

    @staticmethod
    def _gap(data: Any, since: float, until: float, reason: str = "unclassified") -> None:
        code = GAP_REASONS.index(reason) if reason in GAP_REASONS else 0
        _put(data, GAP_REASON_AT + code * 8, min(U64_MAX, _u64(data, GAP_REASON_AT + code * 8) + 1))
        last = int(until // 3600)
        for hour in range(max(last - HOURS + 1, int(since // 3600)), last + 1):
            bucket = hour % HOURS
            label = _u64(data, HOURS_AT + bucket * 8)
            if label > hour:
                continue
            if label != hour:
                start = HEADER_BYTES + bucket * HOUR_BYTES
                data[start:start + HOUR_BYTES] = bytes(HOUR_BYTES)
                _put(data, HOURS_AT + bucket * 8, hour)
            data[GAPS_AT + bucket] = 1

    @classmethod
    def _sources(cls, data: Any, now: float, token: int | None = None, role: str = "",
                 pending_since: float | None = None) -> list[float]:
        confirmations = []
        selected = None
        empty = None
        for index in range(PROCESS_SLOTS):
            offset = PROCESS_AT + index * PROCESS_WIDTH
            identity = _u64(data, offset)
            confirmed = struct.unpack_from("<d", data, offset + 8)[0]
            if identity and now - confirmed > SOURCE_LEASE_SECONDS:
                pending = struct.unpack_from("<d", data, offset + 24)[0]
                cls._gap(data, min(confirmed, pending) if pending else confirmed, now, "source_expired")
                _put(data, LIFECYCLE_AT + 16, min(U64_MAX, _u64(data, LIFECYCLE_AT + 16) + 1))
                _put(data, offset, 0)
                _put(data, offset + 24, 0)
                identity = 0
            if identity == token:
                selected = offset
            elif identity:
                confirmations.append(confirmed)
            else:
                empty = offset if empty is None else empty
        if token is not None:
            selected = selected if selected is not None else empty
            if selected is None:
                _put(data, 48, 1)  # Sticky: an unregistered producer cannot prove coverage.
                cls._gap(data, now, now, "source_slots_full")
            else:
                if not _u64(data, selected):
                    _put(data, LIFECYCLE_AT, min(U64_MAX, _u64(data, LIFECYCLE_AT) + 1))
                _put(data, selected, token)
                struct.pack_into("<d", data, selected + 8, now)
                role_id = ROLES.index(role) + 1 if role in ROLES else 0
                _put(data, selected + 16, role_id)
                # The final eight bytes were reserved in v1. No aggregate or
                # existing source identity changes when adopting pending RPCs.
                struct.pack_into("<d", data, selected + 24, pending_since or 0)
                if role_id and struct.unpack_from("<d", data, _role_offset(role_id - 1))[0] == 0:
                    struct.pack_into("<d", data, _role_offset(role_id - 1), now)
                confirmations.append(now)
        return confirmations

    @staticmethod
    def _uncertain_since(data: Any, now: float, gap_since: float | None = None) -> float:
        starts = [now] if gap_since is None else [now, gap_since]
        for index in range(PROCESS_SLOTS):
            offset = PROCESS_AT + index * PROCESS_WIDTH
            confirmed = struct.unpack_from("<d", data, offset + 8)[0]
            if _u64(data, offset) and now - confirmed > SOURCE_LEASE_SECONDS:
                pending = struct.unpack_from("<d", data, offset + 24)[0]
                starts.append(min(confirmed, pending) if pending else confirmed)
        return min(starts)

    def add(self, account_id: int, stage: str, increments: dict[int, int], *, at: float | None = None,
            source_token: int | None = None, gap_since: float | None = None,
            source_at: float | None = None, pending_since: float | None = None,
            gap_reasons: dict[str, int] | None = None) -> None:
        instant = time.time() if at is None else at
        self.add_batch([(account_id, stage, increments, instant)],
                       source_token=source_token, gap_since=gap_since,
                       source_at=instant if source_at is None else source_at,
                       pending_since=pending_since, gap_reasons=gap_reasons)

    def add_batch(self, rows: list[tuple[int, str, dict[int, int], float]], *,
                  source_token: int | None = None, gap_since: float | None = None,
                  source_at: float | None = None, pending_since: float | None = None,
                  gap_reasons: dict[str, int] | None = None) -> None:
        """One bounded durable transaction; a crash cannot expose complete partial totals."""
        if not 1 <= len(rows) <= BATCH_MAX_ROWS:
            raise ValueError("metrics_batch_size_invalid")
        for account_id, stage, increments, instant in rows:
            if (type(account_id) is not int or account_id < 0 or not isinstance(instant, (int, float))
                    or not 0 < instant < float("inf")
                    or any(type(k) is not int or not 0 <= k < METRICS or type(v) is not int or v < 0
                           for k, v in increments.items())):
                raise ValueError("invalid_fixed_metric")
        source_instant = time.time() if source_at is None else source_at
        started = time.monotonic()
        with self._open() as data:
            _put(data, 24, int(self._uncertain_since(data, source_instant, gap_since) // 3600) + 1)
            data.flush(0, HEADER_BYTES)
            if gap_since is not None:
                self._gap(data, gap_since, source_instant)
            for reason, amount in (gap_reasons or {}).items():
                if reason in GAP_REASONS and type(amount) is int and amount > 0:
                    offset = GAP_REASON_AT + GAP_REASONS.index(reason) * 8
                    _put(data, offset, min(U64_MAX, _u64(data, offset) + amount))
            self._sources(data, source_instant, source_token, self.role, pending_since)
            for account_id, stage, increments, instant in rows:
                hour = int(instant // 3600)
                bucket = hour % HOURS
                label = _u64(data, HOURS_AT + bucket * 8)
                if label > hour:
                    raise ValueError("metrics_clock_regressed")
                if label != hour:
                    offset = HEADER_BYTES + bucket * HOUR_BYTES
                    data[offset:offset + HOUR_BYTES] = bytes(HOUR_BYTES)
                    _put(data, HOURS_AT + bucket * 8, hour)
                    data[GAPS_AT + bucket] = 0
                stage_id = STAGES.index(stage) if stage in STAGES else STAGES.index("unknown")
                slot = self._slot(data, account_id, allocate=True)
                if slot == ACCOUNT_SLOTS and account_id > 0 and increments:
                    _put(data, 32, min(U64_MAX, _u64(data, 32) + 1))
                for metric, amount in increments.items():
                    offset = HEADER_BYTES + bucket * HOUR_BYTES + (slot * len(STAGES) + stage_id) * METRICS * 8 + metric * 8
                    previous = _u64(data, offset)
                    if amount > U64_MAX - previous:
                        data[GAPS_AT + bucket] = 1
                        _put(data, 40, min(U64_MAX, _u64(data, 40) + 1))
                    _put(data, offset, min(U64_MAX, previous + amount))
            # Apply gap after rollover: preparing a new hourly bucket must never clear it.
            if gap_since is not None:
                last = int(source_instant // 3600)
                for hour in range(max(last - HOURS + 1, int(gap_since // 3600)), last + 1):
                    data[GAPS_AT + hour % HOURS] = 1
            data.flush()
            elapsed_ms = (time.monotonic() - started) * 1000
            bucket = next((i for i, bound in enumerate(WRITE_LATENCY_BOUNDS_MS) if elapsed_ms <= bound),
                          len(WRITE_LATENCY_BOUNDS_MS))
            offset = WRITE_LATENCY_AT + bucket * 8
            _put(data, offset, min(U64_MAX, _u64(data, offset) + 1))
            _put(data, 24, 0)
            data.flush(0, HEADER_BYTES)

    def close_source(self, token: int) -> None:
        with self._open() as data:
            for index in range(PROCESS_SLOTS):
                offset = PROCESS_AT + index * PROCESS_WIDTH
                if _u64(data, offset) == token:
                    pending = struct.unpack_from("<d", data, offset + 24)[0]
                    if pending:
                        self._gap(data, pending, time.time(), "cancelled_pending")
                    lifecycle = LIFECYCLE_AT + (24 if pending else 8)
                    _put(data, lifecycle, min(U64_MAX, _u64(data, lifecycle) + 1))
                    _put(data, offset, 0)
                    _put(data, offset + 24, 0)
            data.flush(0, HEADER_BYTES)

    def mark_semantics_cutover(self, now: float | None = None) -> float:
        """Advance after every execution role is upgraded; never reset counts.

        The first new writer fences legacy windows. Deployment advances this
        once more after the last old writer stops, excluding the mixed hour.
        """
        instant = time.time() if now is None else now
        if not isinstance(instant, (int, float)) or not 0 < instant < float("inf"):
            raise ValueError("invalid_measurement_cutover")
        with self._open() as data:
            instant = max(instant, struct.unpack_from("<d", data, SEMANTICS_AT)[0])
            struct.pack_into("<d", data, SEMANTICS_AT, instant)
            data.flush(0, HEADER_BYTES)
            return instant

    def mark_business_semantics_cutover(self, now: float | None = None) -> float:
        """Fence mixed attribution/completion writers after all roles upgrade."""
        instant = time.time() if now is None else now
        if type(instant) not in {int, float} or not 0 < instant < float("inf"):
            raise ValueError("invalid_measurement_cutover")
        with self._open() as data:
            instant = max(instant, struct.unpack_from("<d", data, BUSINESS_SEMANTICS_AT)[0])
            struct.pack_into("<d", data, BUSINESS_SEMANTICS_AT, instant)
            data.flush(0, HEADER_BYTES)
            return instant

    def snapshot(self, account_id: int | None = None, *, now: float | None = None, hours: int = 24) -> dict:
        end_hour = int((time.time() if now is None else now) // 3600)
        hours = min(HOURS, max(1, hours))
        start_hour = end_hour - hours
        with self._open() as data:
            instant = time.time() if now is None else now
            _put(data, 24, int(self._uncertain_since(data, instant) // 3600) + 1)
            data.flush(0, HEADER_BYTES)
            confirmations = self._sources(data, instant)
            data.flush()
            _put(data, 24, 0)
            data.flush(0, HEADER_BYTES)
            created = struct.unpack_from("<d", data, 16)[0]
            semantics_started = struct.unpack_from("<d", data, SEMANTICS_AT)[0]
            business_started = struct.unpack_from("<d", data, BUSINESS_SEMANTICS_AT)[0]
            active_roles = Counter()
            pending_sources = []
            for index in range(PROCESS_SLOTS):
                offset = PROCESS_AT + index * PROCESS_WIDTH
                role_id = _u64(data, offset + 16)
                if _u64(data, offset) and 1 <= role_id <= len(ROLES):
                    active_roles[ROLES[role_id - 1]] += 1
                pending = struct.unpack_from("<d", data, offset + 24)[0]
                if _u64(data, offset) and pending and pending < end_hour * 3600:
                    pending_sources.append(pending)
            missing_roles = [role for role in self.expected_roles if not active_roles[role]]
            coverage_start = max([created, semantics_started, business_started, *(struct.unpack_from("<d", data, _role_offset(ROLES.index(role)))[0]
                                            for role in self.expected_roles)])
            slot = self._slot(data, account_id, allocate=False) if account_id is not None else None
            if account_id is not None and slot is None:
                return {"available": False, "complete": False, "actual_reads": None,
                        "reason": "account_not_metered", "account_slots_limit": ACCOUNT_SLOTS}
            slots = [slot] if slot is not None else list(range(ACCOUNT_SLOTS + 1))
            totals = [[0] * METRICS for _ in STAGES]
            buckets = []
            gap_hours = []
            for hour in range(start_hour, end_hour):
                bucket = hour % HOURS
                if _u64(data, HOURS_AT + bucket * 8) != hour:
                    continue  # No events is a valid zero only after source creation.
                if data[GAPS_AT + bucket]:
                    gap_hours.append(hour * 3600)
                bucket_reads = 0
                for stage_id in range(len(STAGES)):
                    for selected in slots:
                        offset = HEADER_BYTES + bucket * HOUR_BYTES + (selected * len(STAGES) + stage_id) * METRICS * 8
                        values = struct.unpack_from(f"<{METRICS}Q", data, offset)
                        totals[stage_id] = [a + b for a, b in zip(totals[stage_id], values, strict=True)]
                        bucket_reads += sum(values[index] for index in range(1, RPC_COUNT, len(RPC_STATES)))
                buckets.append({"start": hour * 3600, "entered_sdk": bucket_reads})
            source_confirmed = min(confirmations, default=end_hour * 3600)
            complete = (coverage_start <= start_hour * 3600 and not gap_hours and not missing_roles and not pending_sources
                        and source_confirmed >= end_hour * 3600 and not _u64(data, 48))
            rows = [_decode_stage(stage, values) for stage, values in zip(STAGES, totals, strict=True)]
            known_reads = sum(row["entered_sdk"] for row in rows)
            reason = ("expected_source_roles_missing" if missing_roles else
                      "window_before_source" if created > start_hour * 3600 else
                      "window_before_semantics" if semantics_started > start_hour * 3600 else
                      "window_before_business_semantics" if business_started > start_hour * 3600 else
                      "window_before_source" if coverage_start > start_hour * 3600 else
                      "persisted_measurement_gap" if gap_hours else
                      "source_capacity_exceeded" if _u64(data, 48) else
                      "sdk_entry_pending" if pending_sources else
                      "sources_not_confirmed_through_window" if source_confirmed < end_hour * 3600 else None)
            return {"available": True, "complete": complete, "account_id": account_id,
                    "source": "fixed_read_metrics_v1", "reason": reason,
                    "measurement_version": "sdk_entry_v2",
                    "attribution_version": ATTRIBUTION_VERSION,
                    "completion_version": COMPLETION_VERSION,
                    "complexity_version": "business_demand_v1",
                    "business_semantics_started_at": business_started,
                    "measurement_semantics_started_at": semantics_started,
                    "window_start": start_hour * 3600, "window_end": end_hour * 3600,
                    "source_started_at": created, "current_hour_excluded": True,
                    "coverage_started_at": coverage_start, "expected_roles": list(self.expected_roles),
                    "active_roles": dict(active_roles), "missing_roles": missing_roles,
                    "sources_confirmed_through": source_confirmed,
                    "source_slots_limit": PROCESS_SLOTS, "source_capacity_exceeded": bool(_u64(data, 48)),
                    "pending_sdk_since": min(pending_sources, default=None),
                    "sdk_entry_semantics": "v2_pending_before_sdk_count_after_entry; exclude_mixed_deployment_windows",
                    "actual_reads": known_reads if complete else None, "known_entered_sdk": known_reads,
                    "attempted_reads": sum(row["attempted"] for row in rows),
                    "gap_hours": gap_hours, "by_stage": rows, "hourly": buckets,
                    "physical_file_bytes": FILE_BYTES, "physical_hard_limit_bytes": HARD_LIMIT_BYTES,
                    "account_slots_limit": ACCOUNT_SLOTS, "overflow_events": _u64(data, 32),
                    "counter_saturation_events": _u64(data, 40),
                    "scope": "all_governed_read_purposes; entered_sdk_is_not_wire_retry_count",
                    "source_coverage": "registered_sources_and_configured_execution_roles; deployment_must_verify_all_processes",
                    "net_savings": None,
                    "diagnostics": {
                        "scope": "file_lifetime_numeric_counters; historical_gaps_have_no_backfill",
                        "gap_reasons": {name: _u64(data, GAP_REASON_AT + i * 8) for i, name in enumerate(GAP_REASONS)},
                        "source_lifecycle": {name: _u64(data, LIFECYCLE_AT + i * 8) for i, name in enumerate(LIFECYCLE_FIELDS)},
                        "write_body_latency_ms": {str(bound): _u64(data, WRITE_LATENCY_AT + i * 8)
                                                  for i, bound in enumerate((*WRITE_LATENCY_BOUNDS_MS, "over_2000"))},
                        "batch_rows_limit": BATCH_MAX_ROWS, "queue_limit": BATCH_QUEUE_LIMIT,
                    }}


def _decode_stage(stage: str, values: list[int]) -> dict:
    rpc = []
    for purpose_id, purpose in enumerate(PURPOSES):
        for method_id, method in enumerate(METHODS):
            start = (purpose_id * len(METHODS) + method_id) * len(RPC_STATES)
            counts = dict(zip(RPC_STATES, values[start:start + len(RPC_STATES)], strict=True))
            if any(counts.values()):
                rpc.append({"purpose": purpose, "method_class": method, **counts})
    work = []
    for kind_id, kind in enumerate(("review", "renewal")):
        start = RPC_COUNT + kind_id * WORK_WIDTH
        counts = values[start:start + WORK_WIDTH]
        work.append({"workload": kind, "finished_slices": counts[0], "completed_slices": counts[1],
                     "completed_decisions": counts[54], "committed_elapsed_ms": counts[55],
                     "zero_read_slices": counts[2], "failed_slices": counts[3], "entered_sdk": counts[4],
                     "attempted": counts[5], "elapsed_ms": counts[6], "reserved_reads": counts[7],
                     "outcomes": dict(zip(OUTCOMES, counts[8:18], strict=True)),
                     "phases": {name: {"slices": counts[18 + i * 2], "entered_sdk": counts[19 + i * 2]} for i, name in enumerate(PHASES)},
                     "complexity": {name: dict(zip(("slices", "completed_decisions", "entered_sdk", "elapsed_ms", "failed_slices"),
                                                   counts[34 + i * 5:39 + i * 5], strict=True)) for i, name in enumerate(COMPLEXITIES)}})
    optimizations = {reason: dict(zip(OPT_STATES, values[OPT_AT + i * 4:OPT_AT + (i + 1) * 4], strict=True)) for i, reason in enumerate(REASONS)}
    return {"stage": stage, "attempted": sum(row["attempted"] for row in rpc),
            "entered_sdk": sum(row["entered_sdk"] for row in rpc), "rpc": rpc,
            "work": work, "optimizations": optimizations,
            "attribution_overflow": values[312], "extra_reads": values[313], "flood_errors": values[314],
            "scheduler_commits": {"decisions": values[315], "elapsed_ms": values[316],
                                  "qualifying_decisions": values[317] + values[318],
                                  "outcomes": dict(zip(OUTCOMES, values[317:327], strict=True)),
                                  "coverage": "scheduler_committed_review_decisions_only"},
            "business_completions": {"decisions": sum(item["completed_decisions"] for item in work),
                "elapsed_ms": sum(item["committed_elapsed_ms"] for item in work),
                "by_workload": [{"workload": item["workload"], "decisions": item["completed_decisions"],
                                 "outcomes": item["outcomes"], "complexity": {
                                     key: value["completed_decisions"] for key, value in item["complexity"].items()}}
                                for item in work],
                "version": COMPLETION_VERSION,
                "coverage": "new_review_revision_or_identity_change_or_consumed_event;after_transaction_commit;reuse_is_slice"},
            "history_cache_gates": dict(zip(CACHE_GATE_REASONS, values[CACHE_GATE_AT:CACHE_GATE_AT + len(CACHE_GATE_REASONS)], strict=True))}


def _store() -> FixedReadMetrics | None:
    from app.core.config import settings
    if not getattr(settings, "REVIEW_READ_METRICS_ENABLED", False):
        return None
    path = getattr(settings, "REVIEW_READ_METRICS_PATH", "")
    roles = tuple(value.strip() for value in getattr(settings, "REVIEW_READ_METRICS_EXPECTED_ROLES", "backend,growth,celery").split(",") if value.strip())
    role = getattr(settings, "REVIEW_READ_METRICS_ROLE", "")
    if not role or not roles:
        raise ValueError("metrics_execution_roles_not_configured")
    return FixedReadMetrics(path, role=role, expected_roles=roles) if path else None


def stage_for(account_id: int) -> str:
    try:
        from app.modules.acquisition.review_read_flags import measurement_stage
        stage = measurement_stage(account_id)
        return stage if stage in STAGES else "unknown"
    except Exception:
        return "unknown"


def set_work_plan(stage: str, phase: str | None = None, complexity: str = "unknown", planned_reads: int | None = None) -> None:
    from app.core.account.read_work import current
    work = current()
    if work is not None:
        work.measurement_stage = stage if stage in STAGES else "unknown"
        if phase is not None:
            work.measurement_phase = phase if phase in PHASES else "other"
        complexity = {"bounded": "simple", "full": "complex"}.get(complexity, complexity)
        if not getattr(work, "measurement_complexity_locked", False):
            work.measurement_complexity = complexity if complexity in COMPLEXITIES else "unknown"
        if planned_reads is not None:
            work.planned_reads = min(48, max(1, int(planned_reads)))


def lock_business_complexity(previous: dict, event: dict | None, *, force_refresh: bool = False) -> None:
    """Stratify by business demand, independent of optimization/read-plan flags."""
    from app.core.account.read_work import current
    work = current()
    if work is None:
        return
    kinds = set((event or {}).get("kinds") or [])
    known = {"membership", "rules", "gap", "message", "warning", "identity", "profile"}
    if kinds - known:
        complexity = "unknown"
    elif force_refresh or kinds & {"rules", "gap", "warning"} or not previous.get("checked_at"):
        complexity = "complex"
    elif kinds <= {"membership"}:
        complexity = "simple"
    else:
        complexity = "standard"
    work.measurement_complexity = complexity
    work.measurement_complexity_locked = True


def mark_optimization(reason: str, outcome: str = "used", extra_reads: int = 0) -> bool:
    from app.core.account.read_work import current
    work = current()
    if work is None or reason not in REASONS or outcome not in OPT_STATES[1:]:
        return False
    events = getattr(work, "optimization_counts", None)
    if events is None:
        work.optimization_counts = events = Counter()
    metric = OPT_AT + REASONS.index(reason) * 4 + OPT_STATES.index(outcome)
    events[metric] = events.get(metric, 0) + 1
    events[313] = events.get(313, 0) + max(0, min(1000, extra_reads))
    return True


def mark_avoided(reason: str, opportunity_id: Any, reads: int = 1) -> bool:
    """A proven eliminated request has one owner; IDs exist only in this work."""
    from app.core.account.read_work import current
    work = current()
    if work is None or reason not in REASONS or not 1 <= reads <= 64:
        return False
    seen = getattr(work, "avoided_opportunities", None)
    if seen is None:
        work.avoided_opportunities = seen = set()
    # Bounded keys are never serialized; overflow stops crediting savings.
    identity = repr(opportunity_id)[:256]
    if identity in seen:
        return False
    if len(seen) >= 512:
        events = getattr(work, "optimization_counts", None) or Counter()
        events[312] = events.get(312, 0) + 1
        work.optimization_counts = events
        return False
    seen.add(identity)
    events = getattr(work, "optimization_counts", None) or Counter()
    metric = OPT_AT + REASONS.index(reason) * 4
    events[metric] = events.get(metric, 0) + reads
    work.optimization_counts = events
    return True


def _purpose(purpose: str, lane: str) -> str:
    # Internal sync/control reads can run while a wrapper's old active_purpose
    # still names a business operation. Their actual channel has precedence.
    if lane in {"listener", "sync"}:
        return lane
    if purpose in PURPOSES:
        return purpose
    if purpose == "group_qualification":
        return "review"
    if purpose == "ad_qualification_refresh":
        return "renewal"
    if "reconciliation" in purpose:
        return "reconciliation"
    if "join" in purpose:
        return "join"
    if purpose in {"growth_listener", "listener_control"}:
        return "listener"
    if purpose in {"account_dialog_membership_sync", "group_metadata_sync", "profile_bio_sync"}:
        return "sync"
    if lane == "survival":
        return "retention"
    if lane == "ad":
        return "advertising"
    return "other"


def _work_purpose(purpose: str, lane: str, work: Any) -> str:
    if lane not in {"sync", "listener"} and purpose in {
        "group_qualification", "ad_qualification_refresh", "qualification_identity_registration",
        "qualification_verification",
    } and work is not None and work.kind in {"review", "renewal"}:
        return work.kind
    return _purpose(purpose, lane)


def _method(method: str) -> str:
    if "GetState" in method or "Difference" in method:
        return "sync"
    if "Participant" in method or "GetUsers" in method:
        return "identity"
    if any(name in method for name in ("Message", "History", "Search", "Replies")):
        return "history"
    if "Resolve" in method or "Full" in method:
        return "entity"
    return "other"


def mark_history_cache_gate(reason: str) -> bool:
    """Fixed enum only; persisted once with the owning work slice, no labels."""
    from app.core.account.read_work import current

    work = current()
    if reason not in CACHE_GATE_REASONS or work is None or work.kind not in {"review", "renewal"}:
        return False
    counts = getattr(work, "optimization_counts", None)
    if counts is None:
        counts = work.optimization_counts = {}
    metric = CACHE_GATE_AT + CACHE_GATE_REASONS.index(reason)
    counts[metric] = min(U64_MAX, counts.get(metric, 0) + 1)
    return True


def _rpc_increments(methods: list[str] | tuple[str, ...], state: str, purpose: str, lane: str,
                    *, flood: bool = False) -> Counter:
    increments = Counter()
    channel = PURPOSES.index(_purpose(purpose, lane))
    for method in methods:
        increments[(channel * len(METHODS) + METHODS.index(_method(method))) * len(RPC_STATES) + RPC_STATES.index(state)] += 1
    if flood:
        increments[314] += 1
    return increments


@dataclass(slots=True)
class SdkEntry:
    account_id: int
    methods: tuple[str, ...]
    stage: str
    purpose: str
    lane: str
    at: float
    work: Any
    tracked: bool = True
    entered: bool = False


async def prepare_sdk_entry(account_id: int, methods: list[str], *, purpose: str = "", lane: str = "") -> SdkEntry | None:
    """Persist uncertainty, never an entered count, before the SDK can run."""
    from app.core.account.read_work import current

    selected = tuple(method for method in methods if method.split(".")[-1].startswith(("Get", "Search", "Resolve", "CheckChatInvite")))
    if type(account_id) is not int or account_id <= 0 or not selected:
        return None
    work = current(account_id)
    entry = SdkEntry(account_id, selected, getattr(work, "measurement_stage", None) or stage_for(account_id),
                     _work_purpose(purpose, lane, work), lane, time.time(), work)
    state = _state()
    if len(state.pending_sdk) < PENDING_RPC_LIMIT:
        state.pending_sdk[id(entry)] = entry
    else:
        entry.tracked = False
        _fault(state, entry.at)
    try:
        await _persist(account_id, entry.stage, {})
    except asyncio.CancelledError:
        # This coroutine has not returned to the SDK boundary. A second
        # cancellation or busy I/O can leave a conservative pending source.
        await _persist(account_id, entry.stage, {}, resolved=entry)
        raise
    return entry


def enter_sdk(entry: SdkEntry | None) -> None:
    """Call synchronously immediately before awaiting the original SDK call."""
    if entry is None or entry.entered:
        return
    entry.at = time.time()
    entry.stage = getattr(entry.work, "measurement_stage", None) or stage_for(entry.account_id)
    entry.entered = True
    if entry.work is not None:
        entry.work.entered_reads = getattr(entry.work, "entered_reads", 0) + len(entry.methods)


async def finish_sdk_entry(entry: SdkEntry | None, outcome: str, *, flood: bool = False) -> None:
    if entry is None:
        return
    increments = _rpc_increments(entry.methods, "entered_sdk", entry.purpose, entry.lane) if entry.entered else Counter()
    if entry.entered:
        increments.update(_rpc_increments(entry.methods, outcome, entry.purpose, entry.lane, flood=flood))
    await _persist(entry.account_id, entry.stage, increments, at=entry.at, resolved=entry)


async def record_rpc(account_id: int, methods: list[str], state: str, *, purpose: str = "", lane: str = "", flood: bool = False) -> None:
    if type(account_id) is not int or account_id <= 0:
        return
    from app.core.account.read_work import current
    methods = [method for method in methods if method.split(".")[-1].startswith(("Get", "Search", "Resolve", "CheckChatInvite"))]
    if not methods or state not in RPC_STATES:
        return
    work = current(account_id)
    if work is not None and state == "entered_sdk":
        work.entered_reads = getattr(work, "entered_reads", 0) + len(methods)
    increments = _rpc_increments(methods, state, _work_purpose(purpose, lane, work), lane, flood=flood)
    await _persist(account_id, getattr(work, "measurement_stage", None) or stage_for(account_id), increments)


async def record_work(work: Any, *, failed: bool = False) -> None:
    if work.kind not in {"review", "renewal"}:
        return
    offset = RPC_COUNT + (work.kind == "renewal") * WORK_WIDTH
    entered = getattr(work, "entered_reads", 0)
    elapsed = max(0, round((time.monotonic() - work.started) * 1000))
    outcome = work.outcome if work.outcome in OUTCOMES else "other"
    complete = outcome in {"allowed", "trial", "observe", "wait", "reject", "protected"} and not failed
    phase = getattr(work, "measurement_phase", "other")
    phase_id = PHASES.index(phase) if phase in PHASES else PHASES.index("other")
    complexity = getattr(work, "measurement_complexity", "unknown")
    complexity_id = COMPLEXITIES.index(complexity) if complexity in COMPLEXITIES else COMPLEXITIES.index("unknown")
    values = [1, int(complete), int(entered == 0), int(failed), entered, work.reads, elapsed, work.limit]
    increments = Counter({offset + i: value for i, value in enumerate(values)})
    # Outcome and per-complexity decision counters are written by the caller's
    # after_commit hook. A completed slice alone is never a business completion.
    increments[offset + 18 + phase_id * 2] += 1
    increments[offset + 19 + phase_id * 2] += entered
    for i, value in enumerate((1, 0, entered, elapsed, int(failed))):
        increments[offset + 34 + complexity_id * 5 + i] += value
    increments.update(getattr(work, "optimization_counts", {}))
    await _persist(work.account_id, getattr(work, "measurement_stage", None) or stage_for(work.account_id), increments)


async def record_committed_review(account_id: int, decision: str, *, stage: str, elapsed_seconds: float = 0) -> None:
    """Caller invokes after its DB commit; not unique groups or all entrypoints."""
    outcome = decision if decision in OUTCOMES else "other"
    await _persist(account_id, stage, {315: 1, 316: max(0, round(elapsed_seconds * 1000)),
                                       317 + OUTCOMES.index(outcome): 1})


def _state() -> Any:
    loop = asyncio.get_running_loop()
    state = getattr(loop, "_vanguard_read_metrics_state", None)
    if state is None:
        from types import SimpleNamespace
        state = SimpleNamespace(semaphore=asyncio.Semaphore(1), pending_gap=None, pending_sdk={}, failures=0,
                                fault_reasons=Counter(), batch_queue=[], batch_runner=None,
                                inflight=None, health=None,
                                token=(_PROCESS_SEED ^ (os.getpid() << 16) ^ id(loop)) & U64_MAX or 1)
        loop._vanguard_read_metrics_state = state
    return state


def _fault(state: Any, at: float, reason: str = "io_error") -> None:
    state.failures += 1
    state.fault_reasons[reason if reason in GAP_REASONS else "unclassified"] += 1
    state.pending_gap = at if state.pending_gap is None else min(state.pending_gap, at)


def start_metrics_health() -> None:
    """One bounded heartbeat per event loop; install_governor calls this lazily."""
    try:
        if _store() is None:
            return
        state = _state()
        if state.health is None or state.health.done():
            state.health = asyncio.create_task(_health(), name="review-read-metrics-health")
    except (RuntimeError, ValueError):
        return


async def _health() -> None:
    while True:
        await _persist(0, "unknown", {}, start_health=False)
        await asyncio.sleep(30)


async def stop_metrics_health() -> None:
    state = _state()
    if state.batch_runner is not None and not state.batch_runner.done():
        try:
            await asyncio.wait_for(asyncio.shield(state.batch_runner), 2)
        except TimeoutError:
            _fault(state, time.time(), "io_timeout")
            return  # Unclosed source retains uncertainty until the writer or lease resolves.
    if state.health is not None:
        state.health.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await state.health
        state.health = None
    await _persist(0, "unknown", {}, start_health=False)
    if state.pending_gap is None and state.inflight is None:
        try:
            store = _store()
            if store is not None:
                await asyncio.wait_for(asyncio.to_thread(store.close_source, state.token), 2)
        except Exception:
            pass  # Unclosed source expires conservatively; never assume clean exit.


def _batching_enabled(account_id: int) -> bool:
    from app.modules.acquisition.review_read_flags import growth_enabled_for
    return account_id > 0 and growth_enabled_for(account_id)


async def _persist(account_id: int, stage: str, increments: dict[int, int], *, start_health: bool = True,
                   at: float | None = None, resolved: SdkEntry | None = None) -> None:
    try:
        batched = _batching_enabled(account_id)
    except (ImportError, AttributeError):
        batched = False
    if batched:
        await _queue_persist(account_id, stage, increments, start_health=start_health, at=at, resolved=resolved)
        return
    await _persist_direct(account_id, stage, increments, start_health=start_health, at=at, resolved=resolved)


async def _persist_direct(account_id: int, stage: str, increments: dict[int, int], *, start_health: bool = True,
                          at: float | None = None, resolved: SdkEntry | None = None) -> None:
    state = _state()
    at = time.time() if at is None else at
    try:
        store = _store()
        if store is None:
            if resolved is not None:
                state.pending_sdk.pop(id(resolved), None)
            return
        if start_health:
            start_metrics_health()
        try:
            # Keep acquisition in this task: Python 3.11 wait_for wraps it in a
            # child Task, whose completed permit can outlive caller cancellation.
            async with asyncio.timeout(0.25):
                await state.semaphore.acquire()
        except TimeoutError:
            _fault(state, at, "semaphore_timeout")
            return
        generation = state.failures
        gap = state.pending_gap
        reasons = dict(state.fault_reasons)
        pending = min((entry.at for key, entry in state.pending_sdk.items() if resolved is None or key != id(resolved)), default=None)
        # Shield retains exactly one background I/O after the 2s caller limit.
        # The semaphore is released only when that actual thread has stopped.
        task = asyncio.create_task(asyncio.to_thread(store.add, account_id, stage, increments,
                                                    at=at, source_token=state.token, gap_since=gap,
                                                    source_at=time.time(), pending_since=pending, gap_reasons=reasons))
        state.inflight = task

        def finished(done: Any) -> None:
            state.inflight = None
            state.semaphore.release()
            if done.cancelled() or done.exception() is not None:
                _fault(state, at)
            else:
                if resolved is not None:
                    state.pending_sdk.pop(id(resolved), None)
                state.fault_reasons.subtract(reasons)
                state.fault_reasons += Counter()
                if state.failures == generation:
                    state.pending_gap = None

        task.add_done_callback(finished)
        try:
            await asyncio.wait_for(asyncio.shield(task), 2)
        except TimeoutError:
            _fault(state, at, "io_timeout")
    except asyncio.CancelledError:
        if increments:
            _fault(state, at, "cancelled_pending")
        raise
    except Exception:
        _fault(state, at)
        # No RPC replay, new quota or business outcome change on telemetry failure.
        return


async def _queue_persist(account_id: int, stage: str, increments: dict[int, int], *,
                         start_health: bool, at: float | None, resolved: SdkEntry | None) -> None:
    state = _state()
    instant = time.time() if at is None else at
    if _store() is None:
        if resolved is not None:
            state.pending_sdk.pop(id(resolved), None)
        return
    if start_health:
        start_metrics_health()
    if len(state.batch_queue) >= BATCH_QUEUE_LIMIT:
        _fault(state, instant, "batch_overflow")
        return
    done = asyncio.get_running_loop().create_future()
    state.batch_queue.append((account_id, stage, dict(increments), instant, resolved, done))
    if state.batch_runner is None or state.batch_runner.done():
        state.batch_runner = asyncio.create_task(_drain_batches(state), name="review-metrics-writer")
    try:
        await asyncio.wait_for(asyncio.shield(done), 2)
    except TimeoutError:
        _fault(state, instant, "io_timeout")
    except asyncio.CancelledError:
        _fault(state, instant, "cancelled_pending")
        raise


async def _drain_batches(state: Any) -> None:
    # Each waiter stays independent of this writer: caller cancellation never cancels durable I/O.
    while state.batch_queue:
        await asyncio.sleep(BATCH_DELAY_SECONDS)
        batch = state.batch_queue[:BATCH_MAX_ROWS]
        del state.batch_queue[:len(batch)]
        task = None
        try:
            await state.semaphore.acquire()
            generation, reasons, gap = state.failures, dict(state.fault_reasons), state.pending_gap
            resolved_ids = {id(row[4]) for row in batch if row[4] is not None}
            pending = min((entry.at for key, entry in state.pending_sdk.items() if key not in resolved_ids), default=None)
            store = _store()
            rows = [(a, stage, increments, at) for a, stage, increments, at, _, _ in batch]
            task = asyncio.create_task(asyncio.to_thread(store.add_batch, rows, source_token=state.token,
                gap_since=gap, source_at=time.time(), pending_since=pending, gap_reasons=reasons))
            state.inflight = task

            def finished(done: Any, committed=batch, captured_reasons=reasons,
                         captured_generation=generation) -> None:
                # Only actual I/O completion releases the permit, including
                # repeated cancellation of the queue runner during shutdown.
                state.inflight = None
                state.semaphore.release()
                if done.cancelled() or done.exception() is not None:
                    _fault(state, min(row[3] for row in committed), "io_error")
                else:
                    for row in committed:
                        if row[4] is not None:
                            state.pending_sdk.pop(id(row[4]), None)
                    state.fault_reasons.subtract(captured_reasons)
                    state.fault_reasons += Counter()
                    if state.failures == captured_generation:
                        state.pending_gap = None
                for row in committed:
                    if not row[5].done():
                        row[5].set_result(None)

            task.add_done_callback(finished)
            await asyncio.shield(task)
        except BaseException as exc:
            _fault(state, min(row[3] for row in batch),
                   "cancelled_pending" if isinstance(exc, asyncio.CancelledError) else "io_error")
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            if task is None:
                for row in batch:
                    if not row[5].done():
                        row[5].set_result(None)


async def snapshot(account_id: int | None = None) -> dict:
    try:
        store = _store()
        if store is None:
            return {"available": False, "complete": False, "actual_reads": None, "reason": "metrics_disabled"}
        state = _state()
        if state.pending_gap is not None:
            await _persist(0, "unknown", {}, start_health=False)
        async with asyncio.timeout(0.25):
            await state.semaphore.acquire()
        task = asyncio.create_task(asyncio.to_thread(store.snapshot, account_id))
        state.inflight = task

        def finished(done: Any) -> None:
            state.inflight = None
            state.semaphore.release()
            # Consume a late exception after caller timeout; no unbounded tasks.
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)
        result = await asyncio.wait_for(asyncio.shield(task), 2)
        if state.pending_gap is not None:
            result.update(complete=False, actual_reads=None, reason="metrics_write_gap_pending")
        return result
    except Exception as exc:
        return {"available": False, "complete": False, "actual_reads": None, "reason": "metrics_source_unavailable", "error_type": type(exc).__name__}
