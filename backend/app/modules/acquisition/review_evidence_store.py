"""Bounded optional evidence cache; a miss never grants Telegram permission.

There is one fixed-size file, no database, WAL, append log or temporary copy.
Pages and the commit index have two banks. A page is flushed before an index
switch makes it visible. The previous committed bank survives interrupted writes.
All entry points hold both a process-local lock and an OS file lock.

Protected payloads are never TTL/LRU victims. Expiry prepares an UNKNOWN handoff;
the owner must durably transfer that state to the existing business workflow and
acknowledge the handoff before the slot can be reused. This module does not call
Telegram, PostgreSQL or Redis and does not decide qualification.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import struct
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

KIB = 1024
MIB = KIB * KIB
DAY = 24 * 3600
FILE_NAME = "review-evidence-v5.bin"
GUARD_BYTES = 4096
INDEX_BYTES = 64 * KIB
FRAME = struct.Struct("<8sQII32s32s")
MAGIC = b"VGEVID05"
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
COUNTER_LIMIT = 2**63 - 1
COUNTERS = frozenset({
    "cache_hits", "cache_misses", "cache_writes", "cache_replays",
    "observations_admitted", "observation_replays", "rejected_capacity",
    "rejected_invalid", "dependencies_added", "dependency_overflows",
    "control_updates", "maturity_scheduled", "maturity_delay_seconds_total",
    "maturity_delay_seconds_max", "deadline_unknown", "released_verified",
    "released_unknown", "released_edited", "released_deleted", "released_not_applicable",
    "legal_early_releases", "illegal_early_releases", "rejected_early_handoffs",
})


@dataclass(frozen=True, slots=True)
class EvidenceStoreConfig:
    directory: str | Path
    enabled: bool = False
    protected_slots: int = 96
    protected_slot_bytes: int = 2 * MIB
    protected_control_bytes: int = 64 * KIB
    protected_records_per_slot: int = 512
    protected_record_limit: int = 50_000
    slots_per_account: int = 32
    slots_per_account_group: int = 2
    ordinary_pages: int = 64
    ordinary_page_bytes: int = MIB
    ordinary_control_bytes: int = 4096
    ordinary_record_limit: int = 50_000
    ordinary_records_per_group: int = 1000
    max_body_bytes: int = 16 * KIB
    physical_limit_bytes: int = 1024 * MIB
    maturity_grace_seconds: int = 6 * 3600
    lock_timeout_seconds: float = 5

    @property
    def physical_size_bytes(self) -> int:
        return (GUARD_BYTES + 2 * INDEX_BYTES
                + 2 * self.protected_slots * self.protected_slot_bytes
                + 2 * self.ordinary_pages * self.ordinary_page_bytes)

    @property
    def protected_limit_bytes(self) -> int:
        return self.protected_slots * self.protected_slot_bytes

    @property
    def ordinary_limit_bytes(self) -> int:
        return self.ordinary_pages * self.ordinary_page_bytes

    def geometry(self) -> dict[str, int]:
        return {name: int(getattr(self, name)) for name in (
            "protected_slots", "protected_slot_bytes", "protected_control_bytes",
            "protected_records_per_slot", "protected_record_limit", "slots_per_account",
            "slots_per_account_group", "ordinary_pages", "ordinary_page_bytes",
            "ordinary_control_bytes", "ordinary_record_limit", "ordinary_records_per_group",
            "max_body_bytes", "physical_limit_bytes", "maturity_grace_seconds",
        )}

    def validate(self) -> None:
        values = self.geometry()
        if any(value <= 0 for value in values.values()) or self.lock_timeout_seconds <= 0:
            raise ValueError("invalid_store_geometry")
        ceilings = {"protected_slots": 96, "protected_slot_bytes": 2 * MIB,
                    "protected_control_bytes": 64 * KIB, "protected_records_per_slot": 512,
                    "protected_record_limit": 50_000, "slots_per_account": 32,
                    "slots_per_account_group": 2, "ordinary_pages": 64,
                    "ordinary_page_bytes": MIB, "ordinary_record_limit": 50_000,
                    "ordinary_records_per_group": 1000, "max_body_bytes": 16 * KIB,
                    "physical_limit_bytes": 1024 * MIB}
        if any(values[name] > ceiling for name, ceiling in ceilings.items()):
            raise ValueError("store_configuration_exceeds_hard_ceiling")
        if not FRAME.size + 1024 < self.protected_control_bytes < self.protected_slot_bytes:
            raise ValueError("invalid_protected_control_reserve")
        if not FRAME.size + 512 < self.ordinary_control_bytes < self.ordinary_page_bytes:
            raise ValueError("invalid_ordinary_control_reserve")
        if self.physical_size_bytes > self.physical_limit_bytes:
            raise ValueError("physical_layout_exceeds_limit")
        if self.protected_slots * self.protected_records_per_slot > self.protected_record_limit:
            raise ValueError("protected_record_reservations_exceed_limit")


@dataclass(frozen=True, slots=True)
class StoreResult:
    ok: bool
    reason: str
    handle: str | None = None
    data: dict[str, Any] | None = None


class _StoreFault(RuntimeError):
    pass


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _time(value: datetime | float | str | None) -> float:
    if value is None:
        return time.time()
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if isinstance(value, datetime):
        value = value.replace(tzinfo=UTC) if value.tzinfo is None else value
        value = value.timestamp()
    value = float(value)
    if not 0 <= value < 253402300800:
        raise ValueError("invalid_evidence_time")
    return value


def _label(value: Any, *, maximum: int = 256) -> str:
    result = str(value)
    if not result or len(result.encode("utf-8")) > maximum:
        raise ValueError("invalid_evidence_identifier")
    return result


class ReviewEvidenceStore:
    """Explicitly enabled fixed-layout cache with sync capture and async wrappers.

    ``capture_sync`` accepts a body plus bounded JSON metadata. Metadata may carry
    date/edit/from/reply/topic/media/replies, visibility, joined_at and pts versions.
    Ordinary messages should pass an empty body and only their required metadata.
    A successful cache write is not a proof of complete protocol/history coverage.
    """

    def __init__(self, config: EvidenceStoreConfig) -> None:
        self.config = config
        self.directory = Path(config.directory).absolute()
        self.path = self.directory / FILE_NAME
        self.last_reason = "not_initialized"
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(os.path.normcase(str(self.path)), threading.RLock())

    async def initialize(self) -> StoreResult:
        return await asyncio.to_thread(self.initialize_sync)

    def initialize_sync(self) -> StoreResult:
        return self._run(lambda _fd, _index: StoreResult(True, "fixed_layout_verified"))

    @staticmethod
    def _count(index: dict[str, Any], name: str, amount: int = 1) -> None:
        if name not in COUNTERS or amount < 0:
            raise ValueError("invalid_lifecycle_counter")
        counters = index.setdefault("lifecycle_counters", {})
        counters[name] = min(COUNTER_LIMIT, int(counters.get(name, 0)) + amount)

    def _counted(self, fd: int, index: dict[str, Any], result: StoreResult, name: str) -> StoreResult:
        self._count(index, name)
        self._commit(fd, index, [])
        return result

    @contextmanager
    def _file_lock(self, fd: int):
        deadline = time.monotonic() + self.config.lock_timeout_seconds
        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                elif os.name == "posix":
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:
                    raise _StoreFault("file_lock_not_supported")
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise _StoreFault("store_lock_busy") from exc
                time.sleep(0.01)
        try:
            yield
        finally:
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)

    def _check_directory(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.directory.is_symlink() or not self.directory.is_dir():
            raise _StoreFault("store_directory_not_private")
        if any(entry.name != FILE_NAME for entry in self.directory.iterdir()):
            raise _StoreFault("unverified_files_in_store_directory")
        if self.path.is_symlink():
            raise _StoreFault("store_file_is_symlink")

    @staticmethod
    def _write(fd: int, offset: int, value: bytes) -> None:
        os.lseek(fd, offset, os.SEEK_SET)
        view = memoryview(value)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise _StoreFault("short_store_write")
            view = view[count:]

    @staticmethod
    def _read(fd: int, offset: int, size: int) -> bytes:
        os.lseek(fd, offset, os.SEEK_SET)
        pieces = []
        while size:
            piece = os.read(fd, size)
            if not piece:
                raise _StoreFault("short_store_read")
            pieces.append(piece)
            size -= len(piece)
        return b"".join(pieces)

    def _fingerprint(self) -> str:
        return hashlib.sha256(_json(self.config.geometry())).hexdigest()

    def _initialize_file(self, fd: int) -> None:
        size = self.config.physical_size_bytes
        os.ftruncate(fd, size)
        # A fixed non-growing file is the bound. Allocate its blocks up front where
        # supported, so disk exhaustion cannot strand an already admitted slot.
        if hasattr(os, "posix_fallocate"):
            os.posix_fallocate(fd, 0, size)
        else:
            block = bytes(min(MIB, size))
            for offset in range(0, size, len(block)):
                self._write(fd, offset, block[:min(len(block), size - offset)])
        index = {"geometry": self._fingerprint(), "generation": 0,
                 "protected": [None] * self.config.protected_slots,
                 "ordinary": [None] * self.config.ordinary_pages}
        self._write_index(fd, index, 0)
        self._write_index(fd, index, 1)
        self._write_fence(fd, 0, dirty=False)
        os.fsync(fd)

    def _run(self, operation: Callable[[int, dict[str, Any]], StoreResult]) -> StoreResult:
        if not self.config.enabled:
            return StoreResult(False, "evidence_store_disabled")
        try:
            self.config.validate()
            with self._lock:
                self._check_directory()
                flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
                created = False
                try:
                    fd = os.open(self.path, flags | os.O_CREAT | os.O_EXCL, 0o600)
                    created = True
                    self._write(fd, 0, b"\0")
                except FileExistsError:
                    fd = os.open(self.path, flags)
                try:
                    with self._file_lock(fd):
                        info = os.fstat(fd)
                        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                            raise _StoreFault("unverified_store_file")
                        if created:
                            self._initialize_file(fd)
                        if os.fstat(fd).st_size != self.config.physical_size_bytes:
                            raise _StoreFault("store_size_mismatch")
                        self._check_directory()
                        index = self._read_index(fd)
                        fence = self._read_fence(fd)
                        if fence["dirty"]:
                            # An interrupted capture is a coverage gap. Retain
                            # admitted payloads but never resurrect a clear verdict.
                            self._recover_interrupted(fd, index)
                        elif fence["generation"] != index["generation"]:
                            raise _StoreFault("committed_index_lost")
                        try:
                            result = operation(fd, index)
                        except ValueError as exc:
                            result = self._counted(fd, index, StoreResult(False, str(exc)), "rejected_invalid")
                        if not result.ok and result.reason.endswith("capacity"):
                            result = self._counted(fd, index, result, "rejected_capacity")
                        if os.fstat(fd).st_size != self.config.physical_size_bytes:
                            raise _StoreFault("store_size_mismatch")
                        self.last_reason = result.reason
                        return result
                finally:
                    os.close(fd)
        except (OSError, ValueError, TypeError, _StoreFault, OverflowError) as exc:
            self.last_reason = str(exc) if isinstance(exc, (_StoreFault, ValueError)) else type(exc).__name__
            return StoreResult(False, self.last_reason)

    def _write_fence(self, fd: int, generation: int, *, dirty: bool) -> None:
        data = _json({"generation": generation, "dirty": dirty})
        frame = FRAME.pack(MAGIC, generation, 0, len(data), bytes(32),
                           hashlib.sha256(data).digest())
        self._write(fd, 128, frame + data)

    def _read_fence(self, fd: int) -> dict[str, Any]:
        magic, generation, control_len, size, _, digest = FRAME.unpack(
            self._read(fd, 128, FRAME.size))
        if magic != MAGIC or control_len or size > GUARD_BYTES - 128 - FRAME.size:
            raise _StoreFault("commit_fence_corrupt")
        data = self._read(fd, 128 + FRAME.size, size)
        if hashlib.sha256(data).digest() != digest:
            raise _StoreFault("commit_fence_corrupt")
        result = json.loads(data)
        if result.get("generation") != generation or type(result.get("dirty")) is not bool:
            raise _StoreFault("commit_fence_corrupt")
        return result

    def _recover_interrupted(self, fd: int, index: dict[str, Any]) -> None:
        changes = []
        for slot, entry in enumerate(index["protected"]):
            if not entry:
                continue
            control, data = self._load_page(fd, True, slot, entry)
            control["facts"]["gap"] = {"source_id": None, "at": time.time(),
                                       "reason": "interrupted_cache_transaction"}
            control["state"] = "needs_evidence"
            if control.get("handoff"):
                control.update(state="handoff_pending", handoff={
                    "token": uuid4().hex, "outcome": "unknown", "prepared_at": time.time(),
                    "reason": "interrupted_cache_transaction"})
            changes.append((True, slot, {**entry, "state": control["state"]}, control, data))
        index["ordinary"] = [None] * self.config.ordinary_pages
        self._commit(fd, index, changes)

    def _write_index(self, fd: int, index: dict[str, Any], bank: int) -> None:
        data = _json(index)
        if len(data) > INDEX_BYTES - FRAME.size:
            raise _StoreFault("commit_index_capacity")
        frame = FRAME.pack(MAGIC, index["generation"], 0, len(data), bytes(32),
                           hashlib.sha256(data).digest())
        self._write(fd, GUARD_BYTES + bank * INDEX_BYTES, frame + data)

    def _read_index(self, fd: int) -> dict[str, Any]:
        candidates = []
        for bank in range(2):
            try:
                offset = GUARD_BYTES + bank * INDEX_BYTES
                magic, generation, control_size, size, _, digest = FRAME.unpack(
                    self._read(fd, offset, FRAME.size))
                if magic != MAGIC or control_size or size > INDEX_BYTES - FRAME.size:
                    continue
                data = self._read(fd, offset + FRAME.size, size)
                if hashlib.sha256(data).digest() != digest:
                    continue
                value = json.loads(data)
                if (value.get("generation") == generation
                        and value.get("geometry") == self._fingerprint()
                        and len(value["protected"]) == self.config.protected_slots
                        and len(value["ordinary"]) == self.config.ordinary_pages):
                    candidates.append((generation, bank, value))
            except (ValueError, KeyError, TypeError, _StoreFault):
                continue
        if not candidates:
            raise _StoreFault("no_valid_commit_index_or_geometry_mismatch")
        _, bank, index = max(candidates, key=lambda entry: entry[0])
        index["_bank"] = bank
        return index

    def _page_geometry(self, protected: bool, slot: int, bank: int) -> tuple[int, int, int]:
        start = GUARD_BYTES + 2 * INDEX_BYTES
        if protected:
            size, control = self.config.protected_slot_bytes, self.config.protected_control_bytes
        else:
            start += 2 * self.config.protected_slots * self.config.protected_slot_bytes
            size, control = self.config.ordinary_page_bytes, self.config.ordinary_control_bytes
        return start + (2 * slot + bank) * size, size, control

    def _load_page(self, fd: int, protected: bool, slot: int,
                   entry: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        offset, size, reserve = self._page_geometry(protected, slot, entry["bank"])
        magic, generation, control_len, data_len, control_hash, data_hash = FRAME.unpack(
            self._read(fd, offset, FRAME.size))
        if (magic != MAGIC or generation != entry["generation"]
                or control_len > reserve - FRAME.size or data_len > size - reserve):
            raise _StoreFault("evidence_page_corrupt")
        control = self._read(fd, offset + FRAME.size, control_len)
        data = self._read(fd, offset + reserve, data_len)
        if (hashlib.sha256(control).digest() != control_hash
                or hashlib.sha256(data).digest() != data_hash):
            raise _StoreFault("evidence_page_corrupt")
        return json.loads(control), json.loads(data)

    def _commit(self, fd: int, index: dict[str, Any],
                changes: Sequence[tuple[bool, int, dict[str, Any], dict[str, Any], dict[str, Any]]]) -> None:
        generation = index["generation"] + 1
        self._write_fence(fd, index["generation"], dirty=True)
        os.fsync(fd)
        for protected, slot, entry, control, data in changes:
            family = "protected" if protected else "ordinary"
            old = index[family][slot]
            bank = 1 - old["bank"] if old else 0
            entry = {**entry, "bank": bank, "generation": generation}
            control_data, body = _json(control), _json(data)
            offset, size, reserve = self._page_geometry(protected, slot, bank)
            # The slot's control reservation also pays for its commit-index entry.
            if len(control_data) + FRAME.size + len(_json(entry)) > reserve:
                raise _StoreFault("control_reserve_exhausted")
            if len(body) > size - reserve:
                raise _StoreFault("evidence_payload_capacity")
            frame = FRAME.pack(MAGIC, generation, len(control_data), len(body),
                               hashlib.sha256(control_data).digest(), hashlib.sha256(body).digest())
            self._write(fd, offset + FRAME.size, control_data)
            self._write(fd, offset + reserve, body)
            self._write(fd, offset, frame)
            index[family][slot] = entry
        os.fsync(fd)
        old_bank = index.pop("_bank")
        index["generation"] = generation
        self._write_index(fd, index, 1 - old_bank)
        os.fsync(fd)
        self._write_fence(fd, generation, dirty=False)
        os.fsync(fd)
        index["_bank"] = 1 - old_bank

    def _record(self, key: str, body: str, metadata: Mapping[str, Any] | None,
                event_at: datetime | float | str | None, ttl_seconds: int) -> dict[str, Any]:
        if not isinstance(body, str) or len(body.encode("utf-8")) > self.config.max_body_bytes:
            raise ValueError("body_too_large_or_not_text")
        when = _time(event_at)
        if not 0 < ttl_seconds <= 72 * 3600:
            raise ValueError("invalid_cache_ttl")
        result = {"key": _label(key), "body": body, "metadata": dict(metadata or {}),
                  "event_at": when, "expires_at": when + ttl_seconds}
        if len(_json(result["metadata"])) > self.config.max_body_bytes:
            raise ValueError("record_metadata_too_large")
        _json(result)  # Reject unsupported metadata before reserving anything.
        return result

    async def capture(self, **kwargs: Any) -> StoreResult:
        return await asyncio.to_thread(self.capture_sync, **kwargs)

    def capture_sync(self, *, account_id: int, group_id: int | str, key: str,
                     body: str = "", metadata: Mapping[str, Any] | None = None,
                     event_at: datetime | float | str | None = None,
                     ttl_seconds: int = 72 * 3600) -> StoreResult:
        """Capture before ingress compaction. Failure means use the original path."""
        def operation(fd: int, index: dict[str, Any]) -> StoreResult:
            if account_id <= 0:
                raise ValueError("invalid_account_id")
            group = _label(group_id)
            record = self._record(key, body, metadata, event_at, ttl_seconds)
            now = time.time()
            if record["event_at"] > now:
                return StoreResult(False, "cache_event_from_future")
            if record["expires_at"] <= now:
                return StoreResult(False, "cache_record_already_expired")
            pages = index["ordinary"]
            slot = next((i for i, entry in enumerate(pages) if entry
                         and entry["account_id"] == account_id and entry["group_id"] == group), None)
            evicted_group = None
            if slot is None:
                slot = next((i for i, entry in enumerate(pages) if entry is None), None)
                if slot is None:
                    slot = min(range(len(pages)), key=lambda i: pages[i]["last_event_at"])
                    evicted_group = {"account_id": pages[slot]["account_id"],
                                     "group_id": pages[slot]["group_id"]}
                control = {"account_id": account_id, "group_id": group,
                           "coverage": "unknown", "evictions": 0}
                records: dict[str, Any] = {}
            else:
                control, records = self._load_page(fd, False, slot, pages[slot])
            existing = records.get(record["key"])
            if existing:
                if (existing["body"] != body or existing["metadata"] != dict(metadata or {})
                        or existing["event_at"] != record["event_at"]):
                    control["coverage"] = "unknown"
                    return StoreResult(False, "cache_key_version_collision")
                return self._counted(fd, index, StoreResult(True, "cache_replay", data=existing),
                                     "cache_replays")
            expired = [k for k, value in records.items() if value["expires_at"] <= now]
            for expired_key in expired:
                del records[expired_key]
            records[record["key"]] = record
            reserve = self.config.ordinary_control_bytes
            max_bytes = self.config.ordinary_page_bytes - reserve
            other_records = sum(entry["records"] for i, entry in enumerate(pages)
                                if entry and i != slot)
            while (len(records) + 1 > self.config.ordinary_records_per_group
                   or other_records + len(records) + 1 > self.config.ordinary_record_limit
                   or len(_json(records)) > max_bytes):
                victim = min(records, key=lambda item: (records[item]["event_at"], item))
                if victim == record["key"]:
                    return StoreResult(False, "ordinary_capacity")
                del records[victim]
                control["evictions"] += 1
                control["coverage"] = "unknown"
            entry = {"account_id": account_id, "group_id": group,
                     "records": len(records) + 1, "last_event_at": record["event_at"]}
            self._count(index, "cache_writes")
            self._commit(fd, index, [(False, slot, entry, control, records)])
            return StoreResult(True, "cached", data={**record,
                "coverage": "unknown", "evicted_group": evicted_group})
        return self._run(operation)

    async def get_cache(self, account_id: int, group_id: int | str, key: str) -> StoreResult:
        return await asyncio.to_thread(self.get_cache_sync, account_id, group_id, key)

    def get_cache_sync(self, account_id: int, group_id: int | str, key: str) -> StoreResult:
        def operation(fd: int, index: dict[str, Any]) -> StoreResult:
            group = _label(group_id)
            for slot, entry in enumerate(index["ordinary"]):
                if entry and entry["account_id"] == account_id and entry["group_id"] == group:
                    _control, records = self._load_page(fd, False, slot, entry)
                    result = records.get(key)
                    if result and result["expires_at"] > time.time():
                        return self._counted(fd, index, StoreResult(
                            True, "cache_hit", data={**result, "coverage": "unknown"}), "cache_hits")
            # A protected dependency remains usable after ordinary-page pressure.
            for slot, entry in enumerate(index["protected"]):
                if entry and entry["account_id"] == account_id and entry["group_id"] == group:
                    control, payload = self._load_page(fd, True, slot, entry)
                    if control.get("facts"):
                        continue
                    for result in payload.get("records", []):
                        if result["key"] == key:
                            return self._counted(fd, index, StoreResult(
                                True, "cache_hit", data={**result, "coverage": "unknown"}), "cache_hits")
            return self._counted(fd, index, StoreResult(False, "cache_miss"), "cache_misses")
        return self._run(operation)

    async def hint_refs(self, account_id: int, group_id: int | str, joined_at: Any,
                        message_ids: Sequence[int]) -> StoreResult:
        return await asyncio.to_thread(self.hint_refs_sync, account_id, group_id, joined_at, message_ids)

    def hint_refs_sync(self, account_id: int, group_id: int | str, joined_at: Any,
                       message_ids: Sequence[int]) -> StoreResult:
        """Find exact push versions only; callers still check watermarks and freshness."""
        def operation(fd: int, index: dict[str, Any]) -> StoreResult:
            group, joined = _label(group_id), _time(joined_at)
            wanted = {mid for mid in list(message_ids)[:32] if type(mid) is int and mid > 0}
            refs: dict[str, dict] = {}
            conflicts: set[str] = set()
            for protected, family in ((False, "ordinary"), (True, "protected")):
                for slot, entry in enumerate(index[family]):
                    if not entry or entry["account_id"] != account_id or entry["group_id"] != group:
                        continue
                    control, payload = self._load_page(fd, protected, slot, entry)
                    if protected and control.get("facts"):
                        continue
                    records = payload.get("records", []) if protected else payload.values()
                    for record in records:
                        meta = record.get("metadata", {})
                        mid = meta.get("message_id")
                        if (mid not in wanted or meta.get("account_id") != account_id
                                or str(meta.get("group_id")) != group
                                or not meta.get("joined_at") or _time(meta["joined_at"]) != joined
                                or "dependencies" not in meta):
                            continue
                        key = str(mid)
                        ref = {name: meta[name] for name in (
                            "message_id", "version", "account_id", "group_id", "joined_at")}
                        ref["key"] = record["key"]
                        previous = refs.get(key)
                        if previous and previous != ref:
                            conflicts.add(key)
                        refs[key] = ref
            return StoreResult(True, "hint_refs", data={"refs": {
                mid: ref for mid, ref in refs.items() if mid not in conflicts}})
        return self._run(operation)

    def invalidate_messages_sync(self, account_id: int | None = None,
                                 group_id: int | str | None = None,
                                 message_ids: Sequence[int] | None = None) -> StoreResult:
        """Drop only ordinary reuse copies; protected bodies retain their promise."""
        def operation(fd: int, index: dict[str, Any]) -> StoreResult:
            group = _label(group_id) if group_id is not None else None
            identifiers = set(message_ids) if message_ids is not None else None
            changes = []
            removed = 0
            for slot, entry in enumerate(index["ordinary"]):
                if (not entry or account_id is not None and entry["account_id"] != account_id
                        or group is not None and entry["group_id"] != group):
                    continue
                control, payload = self._load_page(fd, False, slot, entry)
                kept = ({key: record for key, record in payload.items()
                         if record.get("metadata", {}).get("message_id") not in identifiers}
                        if identifiers is not None else {})
                removed += len(payload) - len(kept)
                if len(kept) == len(payload):
                    continue
                if kept:
                    changes.append((False, slot, {**entry, "records": len(kept)+1}, control, kept))
                else:
                    index["ordinary"][slot] = None
            if removed:
                self._commit(fd, index, changes)
            return StoreResult(True, "message_reuse_invalidated", data={"removed": removed})
        return self._run(operation)

    async def dependency_records(self, account_id: int, group_id: int | str,
                                 joined_at: Any, message_id: int) -> StoreResult:
        return await asyncio.to_thread(self.dependency_records_sync, account_id, group_id,
                                       joined_at, message_id)

    def dependency_records_sync(self, account_id: int, group_id: int | str,
                                joined_at: Any, message_id: int) -> StoreResult:
        """Bounded necessary materials already received for a new observation."""
        def operation(fd: int, index: dict[str, Any]) -> StoreResult:
            group, joined = _label(group_id), _time(joined_at)
            records = []
            for slot, entry in enumerate(index["ordinary"]):
                if not entry or entry["account_id"] != account_id or entry["group_id"] != group:
                    continue
                _control, payload = self._load_page(fd, False, slot, entry)
                for record in payload.values():
                    meta = record.get("metadata", {})
                    if (record["expires_at"] <= time.time() or not meta.get("joined_at")
                            or _time(meta["joined_at"]) != joined):
                        continue
                    if (meta.get("evidence_kind") in {"rule", "warning"}
                            or meta.get("reply_to_msg_id") == message_id):
                        records.append(record)
            return StoreResult(True, "dependency_records", data={"records": records})
        return self._run(operation)

    async def save_history_page(self, account_id: int, group_id: int | str, *, key: str,
                                records: Sequence[Mapping[str, Any]], metadata: Mapping[str, Any],
                                observed_at: Any = None, ttl_seconds: int = 300) -> StoreResult:
        return await asyncio.to_thread(self.save_history_page_sync, account_id, group_id,
            key=key, records=records, metadata=metadata, observed_at=observed_at, ttl_seconds=ttl_seconds)

    def save_history_page_sync(self, account_id: int, group_id: int | str, *, key: str,
                               records: Sequence[Mapping[str, Any]], metadata: Mapping[str, Any],
                               observed_at: Any = None, ttl_seconds: int = 300) -> StoreResult:
        """Atomic page facts, bounded by both observation and original event age."""
        def operation(fd: int, index: dict[str, Any]) -> StoreResult:
            if account_id <= 0:
                raise ValueError("invalid_account_id")
            if type(ttl_seconds) is not int or not 0 < ttl_seconds <= 72 * 3600:
                raise ValueError("invalid_history_page_ttl")
            group, page_key, instant = _label(group_id), _label(key, maximum=128), _time(observed_at)
            if not 0 <= time.time()-instant < 300:
                return StoreResult(False, "history_page_not_fresh")
            if len(records)+2 > self.config.ordinary_records_per_group:
                return StoreResult(False, "history_page_capacity")
            prefix = f"history:{page_key}:"
            bundle = {}
            expires_at = instant + ttl_seconds
            for raw in records:
                record_key = prefix + _label(raw["key"], maximum=96)
                if record_key in bundle:
                    return StoreResult(False, "history_page_duplicate_key")
                meta = raw.get("metadata") or {}
                dates = [meta[name] for name in ("date", "edit_date") if meta.get(name) is not None]
                if not dates and raw.get("event_at") is not None:
                    dates = [raw["event_at"]]
                if not dates and ttl_seconds > 300:
                    return StoreResult(False, "history_page_event_time_unknown")
                event_at = max((_time(value) for value in dates), default=instant)
                record_expiry = min(event_at + 72 * 3600, instant + ttl_seconds)
                if event_at > time.time() or record_expiry <= time.time():
                    return StoreResult(False, "history_page_event_expired_or_future")
                bundle[record_key] = self._record(record_key, raw.get("body", ""),
                    raw.get("metadata"), event_at, record_expiry - event_at)
                expires_at = min(expires_at, record_expiry)
            manifest_key = prefix+"manifest"
            manifest = self._record(manifest_key, "", {
                **dict(metadata), "history_page": True, "record_keys": list(bundle),
                "observed_at": instant, "history_page_ttl": ttl_seconds}, instant, expires_at - instant)
            if manifest_key in bundle:
                return StoreResult(False, "history_page_duplicate_key")
            bundle[manifest_key] = manifest
            maximum = self.config.ordinary_page_bytes-self.config.ordinary_control_bytes
            if len(_json(bundle)) > maximum:
                return StoreResult(False, "history_page_capacity")
            pages = index["ordinary"]
            slot = next((i for i, entry in enumerate(pages) if entry
                and entry["account_id"] == account_id and entry["group_id"] == group), None)
            if slot is None:
                slot = next((i for i, entry in enumerate(pages) if entry is None), None)
                if slot is None:
                    slot = min(range(len(pages)), key=lambda i: pages[i]["last_event_at"])
                control, existing = {"account_id": account_id, "group_id": group,
                                     "coverage": "unknown", "evictions": 0}, {}
            else:
                control, existing = self._load_page(fd, False, slot, pages[slot])
            kept = {name: record for name, record in existing.items() if record["expires_at"] > time.time()}
            old_manifest = kept.get(manifest_key)
            if old_manifest:
                old_meta = old_manifest.get("metadata", {})
                internal = {"history_page", "record_keys", "observed_at", "history_page_ttl"}
                same = ({k: v for k, v in old_meta.items() if k not in internal} == dict(metadata)
                    and old_meta.get("record_keys") == [name for name in bundle if name != manifest_key]
                    and all(name in kept and kept[name].get("body") == record.get("body")
                        and kept[name].get("metadata") == record.get("metadata")
                        for name, record in bundle.items() if name != manifest_key))
                if same:
                    return StoreResult(True, "history_page_replay", data={"key": page_key,
                        "records": len(records), "coverage": "unknown", "expires_at": old_manifest["expires_at"]})
                return StoreResult(False, "history_page_version_collision")
            for name, record in bundle.items():
                if name in kept and kept[name] != record:
                    return StoreResult(False, "history_page_version_collision")
            kept.update(bundle)
            others = sum(entry["records"] for i, entry in enumerate(pages) if entry and i != slot)
            while (len(kept)+1 > self.config.ordinary_records_per_group
                    or others+len(kept)+1 > self.config.ordinary_record_limit
                    or len(_json(kept)) > maximum):
                victims = [name for name in kept if name not in bundle]
                if not victims:
                    return StoreResult(False, "history_page_capacity")
                victim = min(victims, key=lambda name: (kept[name]["event_at"], name))
                del kept[victim]
                control["evictions"] += 1
            entry = {"account_id": account_id, "group_id": group,
                     "records": len(kept)+1, "last_event_at": instant}
            self._count(index, "cache_writes", len(bundle))
            self._commit(fd, index, [(False, slot, entry, control, kept)])
            return StoreResult(True, "history_page_saved", data={"key": page_key,
                "records": len(records), "coverage": "unknown", "expires_at": expires_at})
        return self._run(operation)

    async def load_history_page(self, account_id: int, group_id: int | str, *, key: str) -> StoreResult:
        return await asyncio.to_thread(self.load_history_page_sync, account_id, group_id, key=key)

    def load_history_page_sync(self, account_id: int, group_id: int | str, *, key: str) -> StoreResult:
        """Missing even one fact is a whole-page miss; no coverage is inferred."""
        def operation(fd: int, index: dict[str, Any]) -> StoreResult:
            group, page_key = _label(group_id), _label(key, maximum=128)
            for slot, entry in enumerate(index["ordinary"]):
                if not entry or entry["account_id"] != account_id or entry["group_id"] != group:
                    continue
                _control, payload = self._load_page(fd, False, slot, entry)
                manifest = payload.get(f"history:{page_key}:manifest")
                if not manifest or not manifest.get("metadata", {}).get("history_page"):
                    break
                metadata = manifest["metadata"]
                ttl = metadata.get("history_page_ttl", 300)
                if (type(ttl) is not int or not 0 < ttl <= 72 * 3600
                        or not 0 <= time.time()-metadata["observed_at"] < ttl
                        or manifest.get("expires_at", 0) <= time.time()):
                    break
                names = metadata["record_keys"]
                if any(name not in payload or payload[name]["expires_at"] <= time.time() for name in names):
                    break
                # Old v1 pages used observed_at as every record's event time.
                # Keep their short-lived format readable, but never let that
                # legacy timestamp extend a known original event beyond 72h.
                event_expired = False
                for name in names:
                    meta = payload[name].get("metadata") or {}
                    dates = [meta[field] for field in ("date", "edit_date") if meta.get(field) is not None]
                    if dates:
                        original = max(_time(value) for value in dates)
                        event_expired = event_expired or not 0 <= time.time()-original < 72*3600
                if event_expired:
                    break
                return self._counted(fd, index, StoreResult(True, "history_page_hit", data={
                    "metadata": {name: value for name, value in metadata.items()
                                 if name not in {"history_page", "record_keys", "history_page_ttl"}},
                    "records": [payload[name] for name in names], "coverage": "unknown"}), "cache_hits")
            return self._counted(fd, index, StoreResult(False, "history_page_miss"), "cache_misses")
        return self._run(operation)

    async def admit_observation(self, **kwargs: Any) -> StoreResult:
        return await asyncio.to_thread(self.admit_observation_sync, **kwargs)

    def admit_observation_sync(self, *, account_id: int, group_id: int | str,
                               message_id: int, version: str, joined_at: datetime | float | str,
                               date: datetime | float | str,
                               edit_date: datetime | float | str | None = None,
                               records: Sequence[Mapping[str, Any]],
                               metadata: Mapping[str, Any] | None = None,
                               now: datetime | float | str | None = None) -> StoreResult:
        def operation(fd: int, index: dict[str, Any]) -> StoreResult:
            instant, original = _time(now), _time(date)
            edited = _time(edit_date) if edit_date is not None else original
            joined = _time(joined_at)
            if account_id <= 0 or message_id <= 0 or max(original, edited, joined) > instant:
                raise ValueError("invalid_observation_identity_or_time")
            group, revision = _label(group_id), _label(version)
            slots = index["protected"]
            account_slots = [entry for entry in slots if entry and entry["account_id"] == account_id]
            group_slots = [entry for entry in account_slots if entry["group_id"] == group]
            for entry in group_slots:
                if entry["message_id"] == message_id and entry["joined_at"] == joined:
                    if entry["version"] == revision:
                        return self._counted(fd, index, StoreResult(
                            True, "observation_replay", handle=entry["handle"]), "observation_replays")
                    if entry["state"] == "observing":
                        return StoreResult(False, "previous_version_requires_invalidation")
            if len(account_slots) >= self.config.slots_per_account:
                return StoreResult(False, "protected_account_capacity")
            if len(group_slots) >= self.config.slots_per_account_group:
                return StoreResult(False, "protected_account_group_capacity")
            slot = next((i for i, entry in enumerate(slots) if entry is None), None)
            if slot is None:
                return StoreResult(False, "protected_capacity")
            if not records or len(records) + 1 > self.config.protected_records_per_slot:
                return StoreResult(False, "protected_record_capacity")
            payload = {"records": [self._record(
                str(record["key"]), record.get("body", ""), record.get("metadata"),
                record.get("event_at", original), 72 * 3600) for record in records]}
            if len(_json(payload)) > self.config.protected_slot_bytes - self.config.protected_control_bytes:
                return StoreResult(False, "protected_payload_capacity")
            handle = uuid4().hex
            ready = max(original, edited) + DAY
            control = {"handle": handle, "account_id": account_id, "group_id": group,
                       "message_id": message_id, "version": revision, "joined_at": joined,
                       "date": original, "edit_date": edited, "admitted_at": instant,
                       "ready_at": ready, "deadline_at": max(instant, ready) + self.config.maturity_grace_seconds,
                       "state": "observing", "facts": {}, "metadata": dict(metadata or {})}
            if len(_json(control["metadata"])) > self.config.protected_control_bytes // 4:
                return StoreResult(False, "observation_metadata_exceeds_control_allowance")
            entry = {key: control[key] for key in (
                "handle", "account_id", "group_id", "message_id", "version", "joined_at", "state")}
            self._count(index, "observations_admitted")
            self._commit(fd, index, [(True, slot, entry, control, payload)])
            return StoreResult(True, "observation_admitted", handle=handle, data=control)
        return self._run(operation)

    def _observation(self, fd: int, index: dict[str, Any], handle: str):
        for slot, entry in enumerate(index["protected"]):
            if entry and entry["handle"] == handle:
                control, data = self._load_page(fd, True, slot, entry)
                return slot, entry, control, data
        return None

    async def list_observations(self, **kwargs: Any) -> StoreResult:
        return await asyncio.to_thread(self.list_observations_sync, **kwargs)

    def list_observations_sync(self, *, account_id: int | None = None,
                               group_id: int | str | None = None,
                               joined_at: datetime | float | str | None = None) -> StoreResult:
        def operation(fd: int, index: dict[str, Any]) -> StoreResult:
            rows = []
            group = _label(group_id) if group_id is not None else None
            joined = _time(joined_at) if joined_at is not None else None
            for slot, entry in enumerate(index["protected"]):
                if (not entry or (account_id is not None and entry["account_id"] != account_id)
                        or (group is not None and entry["group_id"] != group)
                        or (joined is not None and entry["joined_at"] != joined)):
                    continue
                control, _data = self._load_page(fd, True, slot, entry)
                rows.append({"handle": entry["handle"], "control": control})
            return StoreResult(True, "observations_listed", data={"observations": rows})
        return self._run(operation)

    def lookup_observation_sync(self, *, account_id: int, group_id: int | str,
                                message_id: int, joined_at: datetime | float | str,
                                version: str | None = None) -> StoreResult:
        result = self.list_observations_sync(account_id=account_id, group_id=group_id,
                                              joined_at=joined_at)
        if not result.ok:
            return result
        for row in (result.data or {}).get("observations", []):
            control = row["control"]
            if control["message_id"] == message_id and (version is None or control["version"] == version):
                return StoreResult(True, "observation_found", handle=row["handle"], data=control)
        return StoreResult(False, "observation_missing")

    async def lookup_observation(self, **kwargs: Any) -> StoreResult:
        return await asyncio.to_thread(self.lookup_observation_sync, **kwargs)

    async def get_observation(self, handle: str) -> StoreResult:
        return await asyncio.to_thread(self.get_observation_sync, handle)

    async def claim_maintenance(self, **kwargs: Any) -> StoreResult:
        return await asyncio.to_thread(self.claim_maintenance_sync, **kwargs)

    def claim_maintenance_sync(self, *, account_ids: Sequence[int], force: bool = False,
                               now: datetime | float | str | None = None,
                               interval_seconds: int = 300) -> StoreResult:
        """Claim due slots across processes without rewriting their protected pages.

        The index carries at most one small lease per occupied slot. A crashed
        owner delays maintenance for at most the normal five-minute interval;
        it cannot release evidence or change its maturity/deadline. New control
        facts are due immediately once the preceding owner has finished.
        """
        def operation(fd: int, index: dict[str, Any]) -> StoreResult:
            if not 1 <= interval_seconds <= 300:
                raise ValueError("invalid_maintenance_interval")
            instant = _time(now)
            accounts = set(account_ids)
            token = uuid4().hex
            rows, waiting = [], 0
            for slot, entry in enumerate(index["protected"]):
                if not entry or entry["account_id"] not in accounts:
                    continue
                control, _data = self._load_page(fd, True, slot, entry)
                state = hashlib.sha256(_json({name: control.get(name) for name in (
                    "state", "facts", "handoff", "maturity_scheduled_at",
                )})).hexdigest()
                prior = entry.get("maintenance", {})
                if (prior.get("token") and prior.get("lease_until", 0) > instant) or (
                    not force and prior.get("state") == state and prior.get("next_at", 0) > instant
                ):
                    waiting += 1
                    continue
                # Never let the cadence postpone a maturity/deadline boundary.
                due = [instant + interval_seconds, *[
                    control[name] for name in ("ready_at", "deadline_at") if control[name] > instant
                ]]
                index["protected"][slot] = {**entry, "maintenance": {
                    "token": token, "lease_until": instant + interval_seconds,
                    "next_at": min(due), "state": state,
                }}
                rows.append({"handle": entry["handle"], "control": control})
            if rows:
                self._commit(fd, index, [])
            return StoreResult(True, "maintenance_claimed", data={
                "token": token if rows else None, "observations": rows, "waiting": waiting,
            })
        return self._run(operation)

    async def finish_maintenance(self, token: str) -> StoreResult:
        return await asyncio.to_thread(self.finish_maintenance_sync, token)

    def finish_maintenance_sync(self, token: str) -> StoreResult:
        """End only this owner's lease; retain the shared next-at/state marker."""
        def operation(fd: int, index: dict[str, Any]) -> StoreResult:
            changed = False
            for slot, entry in enumerate(index["protected"]):
                if not entry or (entry.get("maintenance") or {}).get("token") != token:
                    continue
                kept = {key: value for key, value in entry["maintenance"].items()
                        if key not in {"token", "lease_until"}}
                index["protected"][slot] = {**entry, "maintenance": kept}
                changed = True
            if changed:
                self._commit(fd, index, [])
            return StoreResult(True, "maintenance_finished")
        return self._run(operation)

    def get_observation_sync(self, handle: str) -> StoreResult:
        def operation(fd: int, index: dict[str, Any]) -> StoreResult:
            found = self._observation(fd, index, handle)
            if not found:
                return StoreResult(False, "observation_missing")
            _slot, _entry, control, data = found
            return StoreResult(True, "observation_found", handle=handle,
                               data={"control": control, **data})
        return self._run(operation)

    async def append_dependencies(self, handle: str, records: Sequence[Mapping[str, Any]]) -> StoreResult:
        return await asyncio.to_thread(self.append_dependencies_sync, handle, records)

    def append_dependencies_sync(self, handle: str, records: Sequence[Mapping[str, Any]]) -> StoreResult:
        """Extend an existing reservation; overflow records a gap and keeps every old body."""
        def operation(fd: int, index: dict[str, Any]) -> StoreResult:
            found = self._observation(fd, index, handle)
            if not found:
                return StoreResult(False, "observation_missing")
            slot, entry, control, payload = found
            existing = {record["key"]: record for record in payload.get("records", [])}
            combined = dict(existing)
            reason = None
            try:
                for raw in records:
                    record = self._record(str(raw["key"]), raw.get("body", ""), raw.get("metadata"),
                                          raw.get("event_at", control["date"]), 72 * 3600)
                    if record["key"] in existing and existing[record["key"]] != record:
                        reason = "dependency_version_collision"
                        break
                    combined[record["key"]] = record
                next_payload = {"records": list(combined.values())}
                if (len(combined) + 1 > self.config.protected_records_per_slot
                        or len(_json(next_payload)) > self.config.protected_slot_bytes - self.config.protected_control_bytes):
                    reason = "dependency_capacity"
            except (ValueError, TypeError, KeyError):
                reason = "dependency_incomplete"
            if reason:
                self._control_fact(control, "gap", reason=reason)
                self._count(index, "dependency_overflows")
                self._commit(fd, index, [(True, slot, {**entry, "state": control["state"]}, control, payload)])
                return StoreResult(False, reason)
            added = len(combined) - len(existing)
            if not added:
                return StoreResult(True, "dependencies_replay", handle=handle)
            self._count(index, "dependencies_added", added)
            self._commit(fd, index, [(True, slot, entry, control, next_payload)])
            return StoreResult(True, "dependencies_protected", handle=handle, data={"added": added})
        return self._run(operation)

    async def mark_maturity_scheduled(self, handle: str, *, now: Any = None) -> StoreResult:
        return await asyncio.to_thread(self.mark_maturity_scheduled_sync, handle, now=now)

    def mark_maturity_scheduled_sync(self, handle: str, *, now: Any = None) -> StoreResult:
        """Called only after the original scheduler cue has committed."""
        def operation(fd: int, index: dict[str, Any]) -> StoreResult:
            found = self._observation(fd, index, handle)
            if not found:
                return StoreResult(False, "observation_missing")
            slot, entry, control, payload = found
            if control.get("maturity_scheduled_at") is not None:
                return StoreResult(True, "maturity_schedule_replay")
            instant = _time(now)
            if instant < control["ready_at"]:
                return StoreResult(False, "observation_not_mature")
            control["maturity_scheduled_at"] = instant
            delay = min(COUNTER_LIMIT, max(0, int(instant - control["ready_at"])))
            self._count(index, "maturity_scheduled")
            self._count(index, "maturity_delay_seconds_total", delay)
            counters = index.setdefault("lifecycle_counters", {})
            counters["maturity_delay_seconds_max"] = max(counters.get("maturity_delay_seconds_max", 0), delay)
            self._commit(fd, index, [(True, slot, entry, control, payload)])
            return StoreResult(True, "maturity_scheduled")
        return self._run(operation)

    async def record_control(self, handle: str, kind: str, **kwargs: Any) -> StoreResult:
        return await asyncio.to_thread(self.record_control_sync, handle, kind, **kwargs)

    def record_ingest_gap_sync(self, source: str, token: str, *,
                               epoch: tuple[str, int] | None = None) -> StoreResult:
        """Commit an ingest failure generation once per occupied reservation.

        Tokens are not Telegram event identities. Each real ingest failure gets
        a new token; retries of its coverage fence keep the same token. A bounded
        per-role map prevents independent processes from undoing one another's
        replay marker. Existing real control events retain their original path.
        """
        def operation(fd: int, index: dict[str, Any]) -> StoreResult:
            if source not in {"backend", "growth", "celery", "other"}:
                raise ValueError("invalid_ingest_gap_source")
            revision = _label(token, maximum=64)
            if epoch is not None and (len(epoch) != 2 or type(epoch[1]) is not int
                                      or not 0 <= epoch[1] <= COUNTER_LIMIT):
                raise ValueError("invalid_ingest_gap_epoch")
            current = ({"boot": _label(epoch[0], maximum=64), "order": epoch[1], "token": revision}
                       if epoch is not None else revision)
            changes, rows = [], []
            for slot, entry in enumerate(index["protected"]):
                if not entry:
                    continue
                control, data = self._load_page(fd, True, slot, entry)
                epochs = dict(control.get("ingest_gap_epochs", {}))
                previous = epochs.get(source)
                replay = previous == current
                if isinstance(previous, dict) and isinstance(current, dict):
                    replay = (previous.get("boot") == current["boot"]
                              and previous.get("order", -1) >= current["order"])
                if not replay:
                    self._control_fact(control, "gap", reason="evidence_ingest_gap")
                    control["ingest_gap_epochs"] = {
                        **{key: value for key, value in epochs.items()
                           if key in {"backend", "growth", "celery", "other"}}, source: current}
                    self._count(index, "control_updates")
                    changes.append((True, slot, {**entry, "state": control["state"]}, control, data))
                rows.append({"handle": entry["handle"], "control": control})
            # Payloads captured during the unresolved interval cannot reappear
            # when its durable business cue has already been consumed. Empty
            # ordinary space requires no commit on a replay.
            ordinary_invalidated = any(index["ordinary"])
            if ordinary_invalidated:
                index["ordinary"] = [None] * self.config.ordinary_pages
            if changes or ordinary_invalidated:
                self._commit(fd, index, changes)
            return StoreResult(True, "ingest_gap_recorded" if changes else "ingest_gap_replay",
                data={"observations": rows, "control_updates": len(changes),
                      "ordinary_invalidated": ordinary_invalidated})
        return self._run(operation)

    @staticmethod
    def _control_fact(control: dict, kind: str, *, source_id: Any = None,
                      at: Any = None, reason: str | None = None) -> None:
        fact = {"source_id": _label(source_id) if source_id is not None else None, "at": _time(at)}
        if reason:
            fact["reason"] = reason
        control["facts"][kind] = fact
        control["state"] = "invalidated" if kind in {"deleted", "edited"} else "needs_evidence"
        if control.get("handoff"):
            control.update(state="handoff_pending", handoff={
                "token": uuid4().hex, "outcome": "unknown", "prepared_at": _time(at),
                "reason": "control_changed_after_handoff"})

    def record_control_sync(self, handle: str, kind: str, *, source_id: int | str | None = None,
                            at: datetime | float | str | None = None) -> StoreResult:
        """Always use the reserved control region, even when payload capacity is full."""
        def operation(fd: int, index: dict[str, Any]) -> StoreResult:
            if kind not in {"warning", "deleted", "edited", "gap", "unknown", "not_applicable"}:
                raise ValueError("invalid_control_fact")
            found = self._observation(fd, index, handle)
            if not found:
                return StoreResult(False, "observation_missing")
            slot, entry, control, data = found
            self._control_fact(control, kind, source_id=source_id, at=at)
            entry = {**entry, "state": control["state"]}
            self._count(index, "control_updates")
            self._commit(fd, index, [(True, slot, entry, control, data)])
            return StoreResult(True, "control_recorded", handle=handle, data=control)
        return self._run(operation)

    async def prepare_handoff(self, handle: str, outcome: str, **kwargs: Any) -> StoreResult:
        return await asyncio.to_thread(self.prepare_handoff_sync, handle, outcome, **kwargs)

    def prepare_handoff_sync(self, handle: str, outcome: str, *,
                              now: datetime | float | str | None = None) -> StoreResult:
        def operation(fd: int, index: dict[str, Any]) -> StoreResult:
            if outcome not in {"verified", "unknown", "edited", "deleted", "not_applicable"}:
                raise ValueError("invalid_handoff_outcome")
            found = self._observation(fd, index, handle)
            if not found:
                return StoreResult(False, "observation_missing")
            slot, entry, control, data = found
            instant = _time(now)
            if outcome in {"verified", "unknown"} and instant < control["ready_at"]:
                return self._counted(fd, index, StoreResult(False, "observation_not_mature"),
                                     "rejected_early_handoffs")
            if outcome == "edited" and "edited" not in control["facts"]:
                return StoreResult(False, "edit_fact_required")
            if outcome == "deleted" and "deleted" not in control["facts"]:
                return StoreResult(False, "deletion_fact_required")
            if outcome == "not_applicable" and "not_applicable" not in control["facts"]:
                return StoreResult(False, "not_applicable_fact_required")
            if outcome == "verified" and control["facts"]:
                return StoreResult(False, "control_facts_require_reconciliation")
            if control.get("handoff") and outcome != "not_applicable":
                return StoreResult(True, "handoff_pending", handle=handle, data=control["handoff"])
            handoff = {"token": uuid4().hex, "outcome": outcome, "prepared_at": instant}
            control.update(state="handoff_pending", handoff=handoff)
            self._commit(fd, index, [(True, slot, {**entry, "state": control["state"]}, control, data)])
            return StoreResult(True, "handoff_prepared", handle=handle, data=handoff)
        return self._run(operation)

    async def ack_handoff(self, handle: str, token: str) -> StoreResult:
        return await asyncio.to_thread(self.ack_handoff_sync, handle, token)

    def ack_handoff_sync(self, handle: str, token: str) -> StoreResult:
        """Call only after the original business workflow durably owns the handoff."""
        def operation(fd: int, index: dict[str, Any]) -> StoreResult:
            found = self._observation(fd, index, handle)
            if not found:
                return StoreResult(False, "observation_missing")
            slot, _entry, control, _data = found
            if not token or (control.get("handoff") or {}).get("token") != token:
                return StoreResult(False, "handoff_token_mismatch")
            pending = control["handoff"]
            outcome = pending["outcome"]
            early = pending["prepared_at"] < control["ready_at"]
            if early and outcome not in {"edited", "deleted", "not_applicable"}:
                # A control fact can replace a previously prepared token. Do not
                # let that path release an otherwise-valid observation early.
                return self._counted(fd, index, StoreResult(False, "observation_not_mature"),
                                     "rejected_early_handoffs")
            self._count(index, f"released_{outcome}")
            if early:
                self._count(index, "legal_early_releases")
            index["protected"][slot] = None
            self._commit(fd, index, [])
            return StoreResult(True, "observation_released", handle=handle)
        return self._run(operation)

    async def sweep(self, *, now: datetime | float | str | None = None) -> StoreResult:
        return await asyncio.to_thread(self.sweep_sync, now=now)

    def sweep_sync(self, *, now: datetime | float | str | None = None) -> StoreResult:
        """Local cleanup only. Protected expiry never deletes payload before handoff."""
        def operation(fd: int, index: dict[str, Any]) -> StoreResult:
            instant = _time(now)
            changes = []
            pending = []
            for slot, entry in enumerate(index["protected"]):
                if not entry:
                    continue
                control, data = self._load_page(fd, True, slot, entry)
                if instant >= control["deadline_at"] and not control.get("handoff"):
                    self._count(index, "deadline_unknown")
                    control.update(state="handoff_pending", handoff={
                        "token": uuid4().hex, "outcome": "unknown", "prepared_at": instant,
                        "reason": "maturity_handoff_deadline"})
                    changes.append((True, slot, {**entry, "state": control["state"]}, control, data))
                if control.get("handoff"):
                    pending.append({"handle": entry["handle"], "account_id": entry["account_id"],
                                    "group_id": entry["group_id"], **control["handoff"]})
            expired = 0
            for slot, entry in enumerate(index["ordinary"]):
                if not entry:
                    continue
                control, data = self._load_page(fd, False, slot, entry)
                kept = {key: value for key, value in data.items() if value["expires_at"] > instant}
                if len(kept) == len(data):
                    continue
                expired += len(data) - len(kept)
                if not kept:
                    index["ordinary"][slot] = None
                else:
                    entry = {**entry, "records": len(kept) + 1}
                    index["ordinary"][slot] = entry
                    changes.append((False, slot, entry, control, kept))
            ordinary = [(slot, entry) for slot, entry in enumerate(index["ordinary"]) if entry]
            records_used = sum(entry["records"] for _slot, entry in ordinary)
            pressure = (len(ordinary) >= self.config.ordinary_pages * 0.9
                        or records_used >= self.config.ordinary_record_limit * 0.9)
            evicted_groups = []
            if pressure:
                for slot, entry in sorted(ordinary, key=lambda pair: pair[1]["last_event_at"]):
                    if (len(ordinary) <= self.config.ordinary_pages * 0.8
                            and records_used <= self.config.ordinary_record_limit * 0.8):
                        break
                    index["ordinary"][slot] = None
                    ordinary = [pair for pair in ordinary if pair[0] != slot]
                    records_used -= entry["records"]
                    evicted_groups.append({"account_id": entry["account_id"], "group_id": entry["group_id"]})
                    changes = [change for change in changes if change[0] or change[1] != slot]
            if changes or expired or evicted_groups:
                self._commit(fd, index, changes)
            return StoreResult(True, "swept", data={"expired_ordinary_records": expired,
                                                    "handoffs": pending,
                                                    "evicted_groups": evicted_groups})
        return self._run(operation)

    async def stats(self) -> StoreResult:
        return await asyncio.to_thread(self.stats_sync)

    def stats_sync(self) -> StoreResult:
        def operation(_fd: int, index: dict[str, Any]) -> StoreResult:
            protected = [entry for entry in index["protected"] if entry]
            ordinary = [entry for entry in index["ordinary"] if entry]
            return StoreResult(True, "store_stats", data={
                "enabled": True, "layout": "fixed_dual_bank_v1",
                "physical_file_bytes": self.config.physical_size_bytes,
                "physical_file_size": self.config.physical_size_bytes,
                "physical_hard_limit_bytes": self.config.physical_limit_bytes,
                "disabled_reason": None,
                "protected_slots_used": len(protected),
                "protected_slots_limit": self.config.protected_slots,
                "protected_reserved_bytes": len(protected) * self.config.protected_slot_bytes,
                "protected_reserved_records": len(protected) * self.config.protected_records_per_slot,
                "ordinary_reserved_bytes": len(ordinary) * self.config.ordinary_page_bytes,
                "ordinary_records": sum(entry["records"] for entry in ordinary),
                "ordinary_pages_used": len(ordinary), "generation": index["generation"],
                "ordinary_bytes_limit": self.config.ordinary_limit_bytes,
                "ordinary_page_limit": self.config.ordinary_pages,
                "ordinary_record_limit": self.config.ordinary_record_limit,
                "ordinary_watermark": len(ordinary) / self.config.ordinary_pages,
                "pending_handoffs": sum(entry["state"] == "handoff_pending" for entry in protected),
                "coverage_default": "unknown",
                "lifecycle_counters": {name: int(index.get("lifecycle_counters", {}).get(name, 0))
                                       for name in sorted(COUNTERS)},
            })
        result = self._run(operation)
        if result.ok:
            return result
        return StoreResult(False, result.reason, data={"enabled": False,
            "disabled_reason": result.reason, "physical_file_size": None})
