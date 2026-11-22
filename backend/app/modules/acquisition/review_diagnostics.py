"""Finite numeric diagnostics. No chat identifiers, bodies, append log or WAL."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import stat
import struct
import threading
import time
from collections.abc import Mapping
from pathlib import Path

FILE_NAME = "review-diagnostics-v1.bin"
FILE_BYTES = 2 * 1024 * 1024
SLOT_BYTES = 4096
HEADER_BYTES = 4096
SLOTS = (FILE_BYTES - HEADER_BYTES) // SLOT_BYTES
# A five-minute maintenance interval has ten minutes of headroom before 24h.
RETENTION_SECONDS = 23 * 3600 + 50 * 60
MAGIC = b"VGDIAG01"
HEADER = struct.Struct("<8sQ")
FRAME = struct.Struct("<8sQdI32s")
# Reserved header bytes; appends still update only HEADER's first 16 bytes.
MAINTENANCE_OFFSETS = {"backend": 32, "growth": 40, "celery": 48}
MAINTENANCE_STAMP = struct.Struct("<d")
MAINTENANCE_INTERVAL_SECONDS = 240
MAINTENANCE_RETRY_SECONDS = 5
EVENTS = frozenset({"review_history_cache_gates", "review_evidence_handoff_failed",
    "review_evidence_maintenance_failed", "review_collection_error", "review_metrics_manager_health_failed"})
REASONS = frozenset({"unknown", "other", "RuntimeError", "TypeError", "ValueError", "KeyError",
    "AttributeError", "TimeoutError", "CancelledError", "OSError", "IOError", "PermissionError",
    "FileNotFoundError", "OperationalError", "DBAPIError", "IntegrityError", "InvalidRequestError",
    "StaleDataError", "ReadTimeout", "ConnectTimeout", "ConnectionError", "AssertionError",
    "IndexError", "ZeroDivisionError", "OverflowError", "NameError", "UnboundLocalError"})
COUNTS = frozenset({"phase_code", "role_code", "sqlstate_code", "stage_code", "module_code", "function_code", "line", "count",
    "scope_invalid", "ingest_unavailable", "ingest_stopped", "ingest_controls_pending",
    "ingest_account_pending", "ingest_dirty", "sdk_state_unavailable", "sdk_watermark_unavailable",
    "sdk_gap", "dependency_unproven", "fingerprint_invalid", "facts_overflow", "fact_gap",
    "fact_scope_changed", "page_reference_unavailable", "page_store_unavailable",
    "page_changed_during_load", "page_metadata_invalid", "page_records_invalid",
    "page_dependency_changed", "page_load_invalid", "save_scope_changed", "save_segment_incomplete",
    "save_store_rejected", "save_invalid", "page_loaded", "fingerprint_ready", "page_saved",
    *(f"frame_{i}_{field}" for i in range(8) for field in ("module_code", "function_code", "line"))})
_lock = threading.RLock()
_store: DiagnosticStore | None = None
_task: asyncio.Task | None = None


class DiagnosticStore:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        if not self.directory.is_absolute():
            raise ValueError("diagnostic_path_must_be_absolute")
        self.path = self.directory / FILE_NAME

    def _open(self) -> int:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.is_symlink() or any(p.name != FILE_NAME or p.is_symlink()
                for p in self.directory.iterdir()):
            raise OSError("diagnostic_directory_unverified")
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_BINARY", 0), 0o600)
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise OSError("diagnostic_file_unverified")
            if info.st_size in {0, 1}:
                os.ftruncate(fd, FILE_BYTES)
                if hasattr(os, "posix_fallocate"):
                    os.posix_fallocate(fd, 0, FILE_BYTES)
                self._write(fd, 0, HEADER.pack(MAGIC, 0))
                os.fsync(fd)
            if os.fstat(fd).st_size != FILE_BYTES:
                raise OSError("diagnostic_file_size_mismatch")
            return fd
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def _write(fd: int, offset: int, value: bytes) -> None:
        os.lseek(fd, offset, os.SEEK_SET)
        view = memoryview(value)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("diagnostic_short_write")
            view = view[written:]

    @staticmethod
    def _read(fd: int, offset: int, size: int) -> bytes:
        os.lseek(fd, offset, os.SEEK_SET)
        return os.read(fd, size)

    def _rows(self, fd: int, now: float, *, clear: bool) -> list[dict]:
        rows = []
        for slot in range(SLOTS):
            offset = HEADER_BYTES + slot * SLOT_BYTES
            frame = self._read(fd, offset, FRAME.size)
            magic, sequence, at, length, digest = FRAME.unpack(frame)
            valid = (magic == MAGIC and 0 <= now - at < RETENTION_SECONDS
                     and length <= SLOT_BYTES - FRAME.size)
            payload = self._read(fd, offset + FRAME.size, length) if valid else b""
            valid = valid and hashlib.sha256(payload).digest() == digest
            if valid:
                try:
                    row = json.loads(payload)
                    valid = row.get("event") in EVENTS and row.get("reason") in REASONS
                except (ValueError, TypeError, AttributeError):
                    valid = False
                if valid:
                    rows.append({"sequence": sequence, "at": at, **row})
            if clear and not valid:
                # An interrupted previous erase can leave a zero frame above
                # nonzero stale payload. Expiry must remove those bytes too.
                if any(self._read(fd, offset, SLOT_BYTES)):
                    self._write(fd, offset, bytes(SLOT_BYTES))
        return rows

    def append(self, event: str, *, counts: Mapping[str, int] | None = None,
               reason: str = "unknown", now: float | None = None) -> bool:
        if not isinstance(event, str) or event not in EVENTS:
            return False
        safe = {key: min(value, 2**63 - 1) for key, value in (counts if isinstance(counts, Mapping) else {}).items()
                if key in COUNTS and type(value) is int and value >= 0}
        data = json.dumps({"event": event, "reason": reason if isinstance(reason, str) and reason in REASONS else "other",
                           "counts": safe}, sort_keys=True, separators=(",", ":")).encode()
        if len(data) > SLOT_BYTES - FRAME.size:
            return False
        try:
            with _lock:
                fd = self._open()
                try:
                    instant = time.time() if now is None else now
                    magic, last = HEADER.unpack(self._read(fd, 0, HEADER.size))
                    if magic != MAGIC:
                        return False
                    rows = self._rows(fd, instant, clear=True)
                    sequence = max(last, max((r["sequence"] for r in rows), default=0)) + 1
                    if sequence >= 2**64:
                        return False
                    frame = FRAME.pack(MAGIC, sequence, instant, len(data), hashlib.sha256(data).digest())
                    self._write(fd, HEADER_BYTES + ((sequence - 1) % SLOTS) * SLOT_BYTES,
                                frame + data + bytes(SLOT_BYTES - len(frame) - len(data)))
                    self._write(fd, 0, HEADER.pack(MAGIC, sequence))
                    os.fsync(fd)
                    return True
                finally:
                    os.close(fd)
        except (OSError, ValueError, TypeError, struct.error):
            return False

    def snapshot(self, *, now: float | None = None) -> list[dict]:
        with _lock:
            fd = self._open()
            try:
                rows = self._rows(fd, time.time() if now is None else now, clear=True)
                os.fsync(fd)
                return sorted(rows, key=lambda row: row["sequence"])
            finally:
                os.close(fd)

    def sweep(self) -> bool:
        try:
            self.snapshot()
            return True
        except (OSError, ValueError, TypeError, struct.error):
            return False

    def _maintenance_sweep(self, role: str) -> bool:
        """Only the running maintenance task may attest a successful sweep."""
        if role not in MAINTENANCE_OFFSETS:
            return self.sweep()
        try:
            with _lock:
                fd = self._open()
                try:
                    magic, _sequence = HEADER.unpack(self._read(fd, 0, HEADER.size))
                    if magic != MAGIC:
                        return False
                    self._rows(fd, time.time(), clear=True)
                    os.fsync(fd)
                    offset = MAINTENANCE_OFFSETS[role]
                    previous = self._read(fd, offset, MAINTENANCE_STAMP.size)
                    instant = time.time()
                    if not math.isfinite(instant) or not 0 < instant <= 253402300799:
                        return False
                    try:
                        self._write(fd, offset, MAINTENANCE_STAMP.pack(instant))
                        os.fsync(fd)
                    except (OSError, ValueError, TypeError, struct.error):
                        # Keep the prior attestation if the stamp cannot be persisted.
                        self._write(fd, offset, previous)
                        os.fsync(fd)
                        raise
                    return True
                finally:
                    os.close(fd)
        except (OSError, ValueError, TypeError, struct.error):
            return False

    def maintenance_status(self) -> dict:
        """Read existing attestations without creating, sweeping or repairing."""
        result = {"available": False, "reason": "diagnostic_unavailable",
                  "roles": dict.fromkeys(MAINTENANCE_OFFSETS, 0.0)}
        fd = None
        try:
            with _lock:
                if self.directory.is_symlink() or self.path.is_symlink():
                    raise OSError("diagnostic_file_unverified")
                fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_BINARY", 0))
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fd, msvcrt.LK_NBRLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or info.st_size != FILE_BYTES):
                    raise OSError("diagnostic_file_unverified")
                magic, _sequence = HEADER.unpack(self._read(fd, 0, HEADER.size))
                if magic != MAGIC:
                    raise ValueError("diagnostic_header_invalid")
                roles = {role: MAINTENANCE_STAMP.unpack(self._read(fd, offset, MAINTENANCE_STAMP.size))[0]
                         for role, offset in MAINTENANCE_OFFSETS.items()}
                if any(not math.isfinite(value) or not 0 <= value <= 253402300799
                       for value in roles.values()):
                    raise ValueError("diagnostic_heartbeat_invalid")
                return {"available": True, "reason": "ok", "roles": roles,
                        "file_bytes": info.st_size, "mtime_ns": info.st_mtime_ns}
        except FileNotFoundError:
            result["reason"] = "diagnostic_file_missing"
        except (OSError, ValueError, TypeError, struct.error):
            pass
        finally:
            if fd is not None:
                os.close(fd)
        return result


def configure(directory: str | Path) -> DiagnosticStore:
    global _store
    _store = DiagnosticStore(directory)
    _store.sweep()  # Clear stale slots before any restart-time consumer reads them.
    return _store


def append(event: str, *, counts: Mapping[str, int] | None = None, reason: str = "unknown") -> bool:
    if _store is None:
        _configure_settings()
    return _store.append(event, counts=counts, reason=reason) if _store is not None else False


def sweep() -> bool:
    return _store.sweep() if _store is not None else False


def read_maintenance_status(directory: str | Path) -> dict:
    """Independent read-only doctor entry point; never consult app settings."""
    return DiagnosticStore(directory).maintenance_status()


def _maintenance_sweep() -> bool:
    if _store is None:
        return False
    try:
        from app.core.config import settings
        role = settings.REVIEW_READ_METRICS_ROLE
    except (ImportError, AttributeError):
        role = ""
    return _store._maintenance_sweep(role)


class DiagnosticLogFilter(logging.Filter):
    """Route only V5 diagnostics to their finite ring; never retain a raw copy."""
    def __init__(self, directory: str | Path | None = None) -> None:
        super().__init__()
        self.store = configure(directory) if directory else _configure_settings()

    def filter(self, record: logging.LogRecord) -> bool:
        value = record.msg if isinstance(record.msg, dict) else None
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            return not any(event in str(record.msg) for event in EVENTS)
        if value is None:
            try:
                value = json.loads(message)
            except (ValueError, TypeError):
                return not any(event in message for event in EVENTS)
        if (not isinstance(value, dict) or not isinstance(value.get("event"), str)
                or value["event"] not in EVENTS):
            return True
        if not getattr(record, "review_diagnostic_routed", False):
            record.review_diagnostic_routed = True
            if self.store is not None:
                self.store.append(value["event"], counts=value.get("evaluations") or {},
                                  reason=value.get("error", "unknown"))
        return False


def _configure_settings() -> DiagnosticStore | None:
    global _store
    if _store is not None:
        return _store
    try:
        from app.core.config import settings
        path = settings.REVIEW_DIAGNOSTICS_PATH
        if path and Path(path).is_absolute():
            return configure(path)
    except (ImportError, AttributeError, OSError, ValueError):
        pass
    return None


def route_event(_logger, _method_name, event_dict):
    """Runs before either JSON or console rendering, including PrintLogger."""
    event = event_dict.get("event")
    if isinstance(event, str) and event in EVENTS:
        append(event, counts=event_dict.get("counts") or event_dict.get("evaluations"),
               reason=event_dict.get("reason") or event_dict.get("error", "unknown"))
        import structlog
        raise structlog.DropEvent
    return event_dict


def install_logging_filters() -> None:
    """Add only diagnostic routing, preserving application/Celery log setup."""
    _configure_settings()
    filt = DiagnosticLogFilter()
    loggers = [logging.getLogger(), *[value for value in logging.Logger.manager.loggerDict.values()
                                   if isinstance(value, logging.Logger)]]
    for logger in loggers:
        for handler in logger.handlers:
            if not any(isinstance(item, DiagnosticLogFilter) for item in handler.filters):
                handler.addFilter(filt)
    import structlog
    processors = structlog.get_config()["processors"]
    if route_event not in processors:
        if isinstance(processors, list):
            # Cached bound loggers share this list; configuring future loggers
            # alone would leave their old console path able to retain a copy.
            processors.insert(0, route_event)
        else:
            structlog.configure(processors=[route_event, *processors])


async def start_maintenance() -> None:
    global _task
    install_logging_filters()
    if _task is not None and not _task.done():
        return

    initial_done = asyncio.Event()
    last_succeeded = False

    async def run() -> None:
        nonlocal last_succeeded
        await initial_done.wait()
        while True:
            delay = MAINTENANCE_INTERVAL_SECONDS if last_succeeded else MAINTENANCE_RETRY_SECONDS
            await asyncio.sleep(delay)
            last_succeeded = await asyncio.to_thread(_maintenance_sweep)

    if _store is not None:
        _task = asyncio.create_task(run(), name="review-diagnostic-retention")
        try:
            last_succeeded = await asyncio.to_thread(_maintenance_sweep)
        finally:
            initial_done.set()


async def stop_maintenance() -> None:
    global _task
    task, _task = _task, None
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
