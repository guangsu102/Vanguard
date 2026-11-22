"""Bounded host-side followup; prepare locally, run on the server only on request.

No Telegram calls, scheduler actions, budget writes, file repair or cache sweep.
All raw logs, DB evidence and request/lease identifiers stay in process memory.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import math
import os
import stat
import struct
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

sys.dont_write_bytecode = True
ACCOUNTS = (2, 33, 34, 35)
ROLES = ("backend", "growth", "celery")
SERVICES = dict(zip(ROLES, ("vanguard-backend", "vanguard-telegram-growth-worker", "vanguard-celery-worker"), strict=True))
STAGES = ("ads", "join", "review", "survival", "reconcile", "verify", "exit", "p3_read", "p2_outbox", "reconciliation", "group_metadata", "spam_check", "other")
WAIT_PHASES = ("queue", "execution", "account_session", "executor_capacity", "protected_slot_reservation", "exclusive_stage", "queue_full", "account_stage", "cancellation_cleanup", "owner_shutdown", "other")
OWNER_EVENTS = ("session_exec_stage_started", "session_exec_stage_completed", "session_exec_stage_timed_out", "session_exec_stage_failed", "session_exec_request_queued", "session_exec_cancellation_requested", "session_exec_caller_cleanup_failed", "session_exec_reply_failed")
ERRORS = ("ModuleNotFoundError", "ImportError", "AttributeError", "TypeError", "ValueError", "RuntimeError", "TimeoutError", "ConnectionError", "OperationalError", "DBAPIError", "IntegrityError", "PermissionError", "OSError", "FloodWaitError", "UserDeactivatedBanError", "AuthKeyUnregisteredError", "other")
DIAG_EVENTS = ("review_history_cache_gates", "review_evidence_handoff_failed", "review_evidence_maintenance_failed", "review_collection_error", "review_metrics_manager_health_failed")
DIAG_REASONS = (*ERRORS, "unknown", "KeyError", "CancelledError", "IOError", "FileNotFoundError", "InvalidRequestError", "StaleDataError", "ReadTimeout", "ConnectTimeout", "AssertionError", "IndexError", "ZeroDivisionError", "OverflowError", "NameError", "UnboundLocalError")
GATES = ("scope_invalid", "ingest_unavailable", "ingest_stopped", "ingest_controls_pending", "ingest_account_pending", "ingest_dirty", "sdk_state_unavailable", "sdk_watermark_unavailable", "sdk_gap", "dependency_unproven", "fingerprint_invalid", "facts_overflow", "fact_gap", "fact_scope_changed", "page_reference_unavailable", "page_store_unavailable", "page_changed_during_load", "page_metadata_invalid", "page_records_invalid", "page_dependency_changed", "page_load_invalid", "save_scope_changed", "save_segment_incomplete", "save_store_rejected", "save_invalid", "page_loaded", "fingerprint_ready", "page_saved")
LOCATION_KEYS = ("stage_code", "module_code", "function_code", "line", "count", *(f"frame_{index}_{field}" for index in range(8) for field in ("module_code", "function_code", "line")))
DIAG_BYTES, DIAG_HEADER, DIAG_SLOT = 2 * 1024 * 1024, 4096, 4096
DIAG_FRAME = struct.Struct("<8sQdI32s")
DIAG_RETENTION = 23 * 3600 + 50 * 60
LOG_BYTES, LOG_LINES, REQUEST_LIMIT, EVIDENCE_LIMIT = 1024 * 1024, 3000, 256, 100
STATES = ("queued", "running", "waiting_ai", "reviewing_ai", "completed", "cancelled", "manual_required", "waiting_membership", "other")
DECISIONS = ("allowed", "trial", "observe", "wait", "reject", "protected", "unknown", "technical_wait", "other")
DB_ERROR_CLASSES = ("InvalidRequestError", "ProgrammingError", "DataError", "StatementError", "CompileError",
                    "OperationalError", "DBAPIError", "InterfaceError", "IntegrityError", "TimeoutError",
                    "ImportError", "ModuleNotFoundError", "AttributeError", "TypeError", "ValueError", "other")
HISTORICAL_REVIEW_SQL = """SELECT account_id,state,decision,count(*) AS count,
    count(*) FILTER(WHERE reason='telegram_read_budget') AS budget_wait,
    count(*) FILTER(WHERE next_retry_at<=:review_now_utc) AS retry_due,
    count(*) FILTER(WHERE state='completed' AND checked_at>=:since) AS committed_rows_checked_since_stage,
    max(checked_at) AS latest_checked,min(next_retry_at) AS earliest_retry
    FROM group_qualification_audit WHERE account_id IN (2,33,34,35)
    GROUP BY account_id,state,decision ORDER BY account_id,state,decision LIMIT 401"""


def finite(value, default=None):
    if type(value) not in (int, float):
        return default
    try:
        result = float(value)
    except OverflowError:
        return default
    return result if math.isfinite(result) else default


def count(value):
    if type(value) is int:
        return min(max(0, value), 2**63 - 1)
    return min(max(0, int(value)), 2**63 - 1) if type(value) is float and math.isfinite(value) else 0


def label(value, allowed):
    return value if isinstance(value, str) and value in allowed else "other"


def stage(value):
    return label(str(value).split(":", 1)[0], STAGES)


def timestamp(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or UTC).timestamp()
    if isinstance(value, str) and len(value) <= 40:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=parsed.tzinfo or UTC).timestamp()
        except (ValueError, OverflowError):
            return None
    result = finite(value)
    return result if result is not None and 0 <= result <= 253402300799 else None


def read_diagnostics(path: Path, since: float, now: float | None = None) -> dict:
    """Read exactly the existing 2 MiB file; never instantiate its writer."""
    fd = None
    try:
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError("unverified")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBRLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != DIAG_BYTES:
            raise ValueError("unverified")
        parts, remaining = [], DIAG_BYTES
        while remaining:
            part = os.read(fd, min(65536, remaining))
            if not part:
                raise ValueError("short")
            parts.append(part)
            remaining -= len(part)
        data = b"".join(parts)
        now = time.time() if now is None else now
        if data[:8] != b"VGDIAG01":
            raise ValueError("format")
        beats = {role: struct.unpack_from("<d", data, 32 + index * 8)[0] for index, role in enumerate(ROLES)}
        if any(finite(at) is None or not 0 <= at <= 253402300799 for at in beats.values()):
            raise ValueError("heartbeat")
        events, reasons, gates, locations = Counter(), Counter(), Counter(), Counter()
        invalid = expired = before_stage = 0
        latest = None
        for offset in range(DIAG_HEADER, DIAG_BYTES, DIAG_SLOT):
            frame = data[offset:offset + DIAG_FRAME.size]
            if not any(frame):
                continue
            magic, _sequence, at, length, digest = DIAG_FRAME.unpack(frame)
            if magic != b"VGDIAG01" or finite(at) is None or length > DIAG_SLOT - DIAG_FRAME.size:
                invalid += 1
                continue
            payload = data[offset + DIAG_FRAME.size:offset + DIAG_FRAME.size + length]
            if hashlib.sha256(payload).digest() != digest:
                invalid += 1
                continue
            if not 0 <= now - at < DIAG_RETENTION:
                expired += 1
                continue
            if at < since:
                before_stage += 1
                continue
            try:
                row = json.loads(payload)
                if not isinstance(row, dict) or row.get("event") not in DIAG_EVENTS:
                    raise ValueError("event")
                event = row["event"]
                reason = label(row.get("reason"), DIAG_REASONS)
                raw = row.get("counts") if isinstance(row.get("counts"), dict) else {}
                safe = {key: count(raw[key]) for key in (*LOCATION_KEYS, *GATES) if type(raw.get(key)) is int and raw[key] >= 0}
            except (ValueError, TypeError, KeyError):
                invalid += 1
                continue
            events[event] += 1
            reasons[reason] += 1
            gates.update({key: safe[key] for key in GATES if key in safe})
            if event != "review_history_cache_gates":
                locations[(event, reason, tuple((key, safe[key]) for key in LOCATION_KEYS if key in safe))] += 1
            latest = max(latest or at, at)
        return {"available": True, "file_bytes": info.st_size, "mtime_ns": info.st_mtime_ns,
                "heartbeat": {role: {"last_success_at": at, "age_seconds": now - at, "fresh": 0 <= now - at <= 300} for role, at in beats.items()},
                "event_counts": dict(events), "reason_counts": dict(reasons), "gate_counts": dict(gates),
                "locations": [{"event": event, "reason": reason, "counts": dict(values), "occurrences": occurrences}
                              for (event, reason, values), occurrences in locations.most_common(32)],
                "locations_truncated": len(locations) > 32, "invalid_slots": invalid, "expired_slots_not_cleaned": expired,
                "before_stage_slots": before_stage, "latest_event_at": latest}
    except (OSError, ValueError, TypeError, struct.error):
        return {"available": False, "reason": "source_unavailable"}
    finally:
        if fd is not None:
            os.close(fd)


def classify_logs(raw: bytes, now: float) -> tuple[dict, list[dict]]:
    """Correlate finite owner events in memory; never return request identifiers."""
    events, errors, levels, waits, requests = Counter(), Counter(), Counter(), Counter(), {}
    lines = raw[:LOG_BYTES].decode("utf-8", errors="replace").splitlines()
    for line in lines[:LOG_LINES]:
        if not line.startswith("{") and " " in line:
            line = line.split(" ", 1)[1]
        try:
            row = json.loads(line)
        except ValueError:
            row = {}
        if not isinstance(row, dict):
            row = {}
        level = row.get("level")
        if level in ("warning", "error", "critical"):
            levels[level] += 1
        for error in ERRORS[:-1]:
            if error in line:
                errors[error] += 1
        event = row.get("event")
        if event not in OWNER_EVENTS:
            continue
        events[event] += 1
        if row.get("wait_phase") is not None:
            waits[label(row["wait_phase"], WAIT_PHASES)] += 1
        identifier = row.get("request_id")
        if not isinstance(identifier, str) or len(identifier) != 32 or any(char not in "0123456789abcdef" for char in identifier):
            continue
        if event == "session_exec_stage_started" and row.get("account_id") in ACCOUNTS:
            requests[identifier] = {"request_id": identifier, "account_id": row["account_id"], "stage": stage(row.get("stage")),
                                    "started_at": timestamp(row.get("started_at")), "deadline_at": timestamp(row.get("deadline_at")), "cancellation_requested": False}
        elif identifier in requests:
            if event == "session_exec_cancellation_requested":
                requests[identifier]["cancellation_requested"] = True
            elif event in ("session_exec_stage_completed", "session_exec_stage_timed_out", "session_exec_stage_failed"):
                requests.pop(identifier)
    candidates = list(requests.values())[-REQUEST_LIMIT:]
    public = {"lines_examined": min(len(lines), LOG_LINES), "tail_truncated_or_full": len(raw) > LOG_BYTES or len(lines) >= LOG_LINES,
              "levels": dict(levels), "exception_mentions": dict(errors), "owner_event_counts": dict(events), "wait_phase_counts": dict(waits),
              "inflight_candidate_count": len(candidates), "inflight_candidates": [{key: value for key, value in row.items() if key != "request_id"} for row in candidates],
              "current_stage_complete": False, "sample_at": now}
    return public, candidates


def bounded_command(args: list[str], *, limit: int = LOG_BYTES, timeout: int = 20) -> tuple[bytes, bool, bool]:
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    timer = threading.Timer(timeout, process.kill)
    timer.daemon = True
    timer.start()
    try:
        data = process.stdout.read(limit + 1)
        truncated = len(data) > limit
        if truncated:
            process.kill()
        process.wait(timeout=3)
        return data[:limit], truncated, process.returncode == 0
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
        process.stdout.close()


async def remote_snapshot(since: float, candidates: list[dict]) -> dict:
    """Runs inside backend only: one read-only SQL connection and Redis reads."""
    import redis.asyncio as redis_asyncio
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.core.config import settings

    result = {"db_available": False, "db_error_class": None, "redis_available": False, "accounts": [], "reviews": [], "budgets": [], "ads": [], "joins": [],
              "reviews_snapshot_is_all_historical_rows": True, "retry_due_is_queue_length": False,
              "review_time_basis": "utc_naive_bound", "current_membership_reviews": [],
              "current_membership_reviews_available": False,
              "current_membership_review_scope": "not_collected",
              "current_membership_due_scope": "not_collected",
              "due_by_policy_is_executable_queue_length": False,
              "renewal_snapshot_scan": [], "worker_heartbeat_fresh": False, "owner": [], "owner_queue": {}, "candidate_liveness": []}
    engine = create_async_engine(settings.DATABASE_URL, echo=False, pool_size=1, max_overflow=0, pool_pre_ping=True)
    started = datetime.fromtimestamp(since, UTC).replace(tzinfo=None)
    review_now = datetime.now(UTC).replace(tzinfo=None)
    args = {"since": started, "review_now_utc": review_now}
    result["review_snapshot_at"] = timestamp(review_now)
    ids = "(2,33,34,35)"
    try:
        async with asyncio.timeout(30):
            async with engine.connect() as db:
                await db.execute(text("SET TRANSACTION READ ONLY"))
                await db.execute(text("SET LOCAL statement_timeout='5000ms'"))
                await db.execute(text("SET LOCAL lock_timeout='1000ms'"))
                try:
                    rows = (await db.execute(text(f"""SELECT a.id AS account_id,a.status::text AS status,a.is_active,
                        (a.proxy_mode::text='static') AS static_mode,(a.static_proxy_id IS NOT NULL) AS proxy_bound,
                        coalesce(p.is_active,false) AS proxy_active,(lower(p.proxy_type::text)='residential') AS residential,
                        (SELECT count(*) FROM telegram_account b WHERE b.is_active AND b.static_proxy_id=a.static_proxy_id) AS active_binding_count,
                        NOT EXISTS(SELECT 1 FROM telegram_account b JOIN proxy q ON q.id=b.static_proxy_id
                          WHERE b.is_active AND b.id<>a.id AND q.host=p.host AND q.port=p.port
                          AND q.username IS NOT DISTINCT FROM p.username AND q.password IS NOT DISTINCT FROM p.password) AS unique_configuration
                        FROM telegram_account a LEFT JOIN proxy p ON p.id=a.static_proxy_id WHERE a.id IN {ids} ORDER BY a.id"""))).mappings().all()
                    for row in rows:
                        result["accounts"].append({"account_id": row["account_id"], "status": label(row["status"].lower(), ("online", "offline", "working", "idle", "restricted", "banned", "error", "other")),
                            **{key: bool(row[key]) for key in ("is_active", "static_mode", "proxy_bound", "proxy_active", "residential", "unique_configuration")},
                            "active_binding_count": count(row["active_binding_count"]), "exit_ip_verified": False, "listener_connected": None})
                    rows = (await db.execute(text(HISTORICAL_REVIEW_SQL), args)).mappings().all()
                    result["review_groups_truncated"] = len(rows) > 400
                    for row in rows[:400]:
                        result["reviews"].append({"account_id": row["account_id"], "state": label(row["state"], STATES), "decision": label(row["decision"], DECISIONS),
                            **{key: count(row[key]) for key in ("count", "budget_wait", "retry_due", "committed_rows_checked_since_stage")},
                            "latest_checked": timestamp(row["latest_checked"]), "earliest_retry": timestamp(row["earliest_retry"])})
                    rows = (await db.execute(text(f"""SELECT account_id,lane,state,count(*) AS count,sum(consumed_reads) AS charged_reads,
                        count(*) FILTER(WHERE created_at>=:since) AS created_since_stage,
                        count(*) FILTER(WHERE settled_at>=:since) AS settled_since_stage
                        FROM account_budget_reservation WHERE account_id IN {ids}
                        AND (created_at>=:since OR settled_at>=:since OR state='reserved')
                        GROUP BY account_id,lane,state ORDER BY account_id,lane,state LIMIT 120"""), args)).mappings().all()
                    for row in rows:
                        result["budgets"].append({"account_id": row["account_id"], "lane": label(row["lane"], ("ad", "survival", "critical", "sync", "routine", "listener", "other")),
                            "state": label(row["state"], ("reserved", "settled", "released", "expired", "other")),
                            **{key: count(row[key]) for key in ("count", "charged_reads", "created_since_stage", "settled_since_stage")}})
                    rows = (await db.execute(text(f"""SELECT account_id,
                        count(*) FILTER(WHERE attempted_at>=:since) AS attempted_since_stage,
                        count(*) FILTER(WHERE state='succeeded' AND message_id IS NOT NULL AND completed_at>=:since) AS succeeded_since_stage,
                        count(*) FILTER(WHERE state='unknown') AS unknown_current,
                        count(*) FILTER(WHERE state='failed' AND completed_at>=:since) AS failed_since_stage,
                        max(completed_at) FILTER(WHERE state='succeeded' AND message_id IS NOT NULL) AS latest_success
                        FROM account_outbound_attempt WHERE account_id IN {ids} AND category='ad'
                        AND (attempted_at>=:since OR completed_at>=:since OR state='unknown') GROUP BY account_id"""), args)).mappings().all()
                    result["ads"] = [{"account_id": row["account_id"], **{key: count(row[key]) for key in ("attempted_since_stage", "succeeded_since_stage", "unknown_current", "failed_since_stage")},
                                      "latest_success": timestamp(row["latest_success"])} for row in rows]
                    rows = (await db.execute(text(f"""SELECT account_id,
                        count(*) FILTER(WHERE telegram_action_attempted AND request_sent_at>=:since) AS sent_since_stage,
                        count(*) FILTER(WHERE status='success' AND joined_at>=:since) AS succeeded_since_stage,
                        count(*) FILTER(WHERE request_state='outcome_unknown') AS unknown_current,
                        count(*) FILTER(WHERE request_state='sent' AND status='pending') AS sent_pending_current,
                        max(joined_at) FILTER(WHERE status='success') AS latest_success
                        FROM acquisition_auto_join_attempt WHERE account_id IN {ids}
                        AND (attempted_at>=:since OR joined_at>=:since OR request_state IN ('sent','outcome_unknown')) GROUP BY account_id"""), args)).mappings().all()
                    result["joins"] = [{"account_id": row["account_id"], **{key: count(row[key]) for key in ("sent_since_stage", "succeeded_since_stage", "unknown_current", "sent_pending_current")},
                                        "latest_success": timestamp(row["latest_success"])} for row in rows]
                    for account in ACCOUNTS:
                        rows = (await db.execute(text("""SELECT state,decision,left(evidence_json,65537) AS evidence
                            FROM group_qualification_audit WHERE account_id=:account AND state!='cancelled'
                            ORDER BY id DESC LIMIT 101"""), {"account": account})).mappings().all()
                        counts = Counter()
                        for row in rows[:EVIDENCE_LIMIT]:
                            raw = row["evidence"] or "{}"
                            if len(raw) > 65536:
                                counts["oversized_not_parsed"] += 1
                                continue
                            try:
                                saved = json.loads(raw)
                                if not isinstance(saved, dict):
                                    raise ValueError("snapshot")
                            except (TypeError, ValueError):
                                counts["invalid_json"] += 1
                                continue
                            counts["scanned"] += 1
                            counts["renewal_budget_wait"] += int(saved.get("renewal_wait_reason") == "telegram_read_budget")
                            reconciled = timestamp(saved.get("event_reconciled_at"))
                            counts["event_renewal_committed_rows_since_stage"] += int(reconciled is not None and reconciled >= since
                                and row["state"] == "completed" and row["decision"] in ("allowed", "trial") and saved.get("authorization_mode") == "events")
                        result["renewal_snapshot_scan"].append({"account_id": account, **dict(counts), "row_limit": EVIDENCE_LIMIT,
                            "truncated": len(rows) > EVIDENCE_LIMIT, "local_renewal_since_stage_verified": False, "transition_count_complete": False})
                    workers = (await db.execute(text("""SELECT last_heartbeat_at,left(metadata_json,262145) AS metadata
                        FROM telegram_worker_status WHERE role='growth_user_worker' ORDER BY last_heartbeat_at DESC NULLS LAST LIMIT 1"""))).mappings().all()
                    if workers:
                        at = timestamp(workers[0]["last_heartbeat_at"])
                        fresh = at is not None and 0 <= time.time() - at <= 180
                        result["worker_heartbeat_fresh"] = fresh
                        result["worker_heartbeat_at"] = at
                        raw = workers[0]["metadata"] or "{}"
                        if fresh and len(raw) <= 262144:
                            try:
                                metadata = json.loads(raw)
                                listeners = (metadata.get("runtime") or {}).get("listeners") or []
                                connected = {row.get("account_id"): row.get("connected") is True for row in listeners[:32] if isinstance(row, dict) and row.get("account_id") in ACCOUNTS}
                                for account in result["accounts"]:
                                    account["listener_connected"] = connected.get(account["account_id"])
                            except (ValueError, TypeError, AttributeError):
                                pass
                    result["db_available"] = True
                finally:
                    await db.rollback()
    except Exception as error:
        result["db_available"] = False
        result["db_error_class"] = label(type(error).__name__, DB_ERROR_CLASSES)
    finally:
        await engine.dispose()
    client = redis_asyncio.from_url(settings.REDIS_URL, password=settings.effective_redis_password,
        encoding="utf-8", decode_responses=True, socket_timeout=3, socket_connect_timeout=3)
    try:
        async with asyncio.timeout(8):
            for account in ACCOUNTS:
                owner = await client.get(f"vanguard:telegram:owner:{account}")
                lease_ttl = await client.ttl(f"vanguard:telegram:lease:{account}")
                operation = await client.get(f"vanguard:account-operation:{account}")
                run = await client.get(f"vanguard:growth_dispatch:v1:account:{account}:run")
                operation_purpose = operation.split(":")[1] if isinstance(operation, str) and operation.startswith("account-pool:") else "other"
                result["owner"].append({"account_id": account, "owner_present": bool(owner), "session_lease_ttl_seconds": int(lease_ttl),
                    "operation_present": bool(operation), "operation_purpose": label(operation_purpose, ("group_qualification", "ad_qualification_refresh", "auto_join", "auto_ads", "ad_survival_check", "qualification_verification", "other")),
                    "scheduler_run_present": bool(run), "scheduler_stage": stage(run) if run else "other"})
            raw_queue = await client.lrange("vanguard:session-exec:requests", 0, REQUEST_LIMIT - 1)
            total = await client.llen("vanguard:session-exec:requests")
            parsed = []
            for raw in raw_queue:
                try:
                    row = json.loads(raw) if len(raw) <= 4096 else None
                    identifier = row.get("id") if isinstance(row, dict) else None
                    if not isinstance(identifier, str) or len(identifier) != 32 or any(char not in "0123456789abcdef" for char in identifier) or row.get("account_id") not in ACCOUNTS:
                        continue
                    parsed.append(row)
                except (TypeError, ValueError):
                    continue
            alive = await client.mget(["vanguard:session-exec:active:" + row["id"] for row in parsed]) if parsed else []
            result["owner_queue"] = {"total": count(total), "inspected": len(raw_queue), "truncated": total > REQUEST_LIMIT,
                "safe_account_requests": len(parsed), "caller_gone_candidates": sum(not value for value in alive),
                "deadline_expired": sum((timestamp(row.get("expires_at")) or 0) < time.time() for row in parsed),
                "by_stage": dict(Counter(stage(row.get("stage")) for row in parsed))}
            filtered = [row for row in candidates[:REQUEST_LIMIT] if row.get("account_id") in ACCOUNTS and isinstance(row.get("request_id"), str)
                        and len(row["request_id"]) == 32 and all(char in "0123456789abcdef" for char in row["request_id"])]
            live = await client.mget(["vanguard:session-exec:active:" + row["request_id"] for row in filtered]) if filtered else []
            result["candidate_liveness"] = [{"index": index, "caller_alive": bool(value)} for index, value in enumerate(live)]
            result["redis_available"] = True
    except Exception:
        result["redis_available"] = False
    finally:
        await client.aclose()
    return result


def protected_summary(directory: Path) -> dict:
    try:
        spec = importlib.util.spec_from_file_location("six_followup_protection", directory / "protection_readonly.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        proof = module.fixed_evidence()
        names = ("baseline_count", "current_protected_count", "exactly_preserved", "missing_count", "changed_count", "ready_count", "deadline_due_count", "maturity_scheduled_count", "active_maintenance_leases", "file_bytes")
        result = {"available": proof.get("complete") is True, **{key: count(proof.get(key)) for key in names}}
        required = ("baseline_count", "current_protected_count", "exactly_preserved", "missing_count", "changed_count")
        valid = all(type(proof.get(key)) is int and proof[key] >= 0 for key in required)
        common = bool(valid and proof["baseline_count"] == 32 and proof["current_protected_count"] >= 32
                      and proof["exactly_preserved"] + proof["missing_count"] + proof["changed_count"] == 32)
        result["proof_version"] = "unsupported"
        result["original_32_preserved"] = False
        if proof.get("guard_version") == "original_prefix_v2":
            extra = ("original_records_preserved", "stable_fields_changed", "original_prefix_missing", "appended_count",
                     "append_scope_mismatch", "append_kind_invalid", "append_records_invalid", "index_scope_mismatch")
            result.update({key: count(proof.get(key)) for key in extra})
            valid = valid and all(type(proof.get(key)) is int and proof[key] >= 0 for key in extra)
            result["proof_version"] = "original_prefix_v2"
            result["original_32_preserved"] = bool(result["available"] and common and valid
                and proof["original_records_preserved"] == 32 and proof["missing_count"] == 0
                and all(proof[key] == 0 for key in extra if key not in {"original_records_preserved", "appended_count"}))
        elif "guard_version" not in proof:
            result["proof_version"] = "exact_hash_v1"
            result["original_32_preserved"] = bool(result["available"] and common
                and proof["exactly_preserved"] == 32 and proof["missing_count"] == proof["changed_count"] == 0)
        else:
            valid = False
        result["proof_fields_valid"] = valid
        result["earliest_ready_at"] = timestamp(proof.get("earliest_ready_at_utc"))
        result["earliest_deadline_at"] = timestamp(proof.get("earliest_deadline_at_utc"))
        states = Counter()
        for key, value in (proof.get("current_state_counts") or {}).items():
            states[label(key, ("needs_evidence", "ready", "mature", "handed_off", "unknown", "other"))] += count(value)
        result["state_counts"] = dict(states)
        return result
    except Exception:
        return {"available": False, "original_32_preserved": False, "proof_version": "unavailable", "reason": "source_unavailable"}


def heartbeat_comparison(current: dict, previous: dict | None, now: float) -> dict:
    result = {"prior_available": False, "interval_sufficient": False, "all_roles_advanced": False}
    try:
        at = finite(previous["observed_at"])
        old = previous["diagnostics"]["heartbeat"]
        new = current["heartbeat"]
        if (at is None or not current.get("available") or previous["diagnostics"].get("available") is not True
                or set(old) != set(ROLES) or set(new) != set(ROLES)):
            return result
        if any(finite(old[role]["last_success_at"]) is None or not 0 < old[role]["last_success_at"] <= at
               or at - old[role]["last_success_at"] > 300 or not new[role].get("fresh") for role in ROLES):
            return result
        interval = now - at
        advances = {role: finite(new[role]["last_success_at"]) is not None and finite(old[role]["last_success_at"]) is not None
                    and new[role]["last_success_at"] > old[role]["last_success_at"] for role in ROLES}
        result.update(prior_available=True, interval_seconds=interval, interval_sufficient=interval >= 240,
                      roles_advanced=advances, all_roles_advanced=interval >= 240 and all(advances.values()))
    except (KeyError, TypeError, ValueError):
        pass
    return result


def host_snapshot(since: float, directory: Path, prior: dict | None) -> dict:
    now = time.time()
    result = {"observed_at": now, "stage_start": since, "business_writes": 0, "telegram_calls": 0,
              "manual_reorders": 0, "budget_writes": 0, "source_scope": "natural_business_only", "logs": {}, "applications": {}}
    candidates = []
    for role, name in SERVICES.items():
        raw, clipped, ok = bounded_command(["docker", "inspect", "--format", "{{json .State}}", name], limit=65536, timeout=10)
        try:
            info = json.loads(raw) if ok and not clipped else {}
            result["applications"][role] = {"available": bool(info), "state": label(info.get("Status"), ("running", "exited", "restarting", "paused", "dead", "created", "other")),
                "health": label((info.get("Health") or {}).get("Status"), ("healthy", "unhealthy", "starting", "other")), "started_at": timestamp(info.get("StartedAt"))}
        except (TypeError, ValueError, AttributeError):
            result["applications"][role] = {"available": False}
        raw, clipped, ok = bounded_command(["docker", "logs", "--timestamps", "--since", datetime.fromtimestamp(since, UTC).isoformat(), "--tail", str(LOG_LINES), name], timeout=15)
        public, active = classify_logs(raw, now)
        public.update(command_succeeded=ok, byte_limit_reached=clipped)
        result["logs"][role] = public
        candidates.extend(active)
    candidates = list({item["request_id"]: item for item in candidates}.values())[-REQUEST_LIMIT:]
    code = Path(__file__).read_text(encoding="utf-8")
    raw, clipped, ok = bounded_command(["docker", "exec", "vanguard-backend", "python", "-B", "-c", code,
        "--container-snapshot", "--stage-start", datetime.fromtimestamp(since, UTC).isoformat(),
        "--request-ids", json.dumps(candidates, separators=(",", ":"))], limit=512 * 1024, timeout=45)
    remote = None
    if ok and not clipped:
        for line in raw.decode("utf-8", errors="replace").splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict) and set(value) == {"six_followup_remote"}:
                remote = value["six_followup_remote"]
    result["natural_work"] = remote if isinstance(remote, dict) else {"db_available": False, "redis_available": False, "reason": "source_unavailable"}
    if isinstance(remote, dict):
        liveness = {item["index"]: item["caller_alive"] for item in remote.pop("candidate_liveness", []) if isinstance(item, dict)}
        result["owner_inflight_candidates"] = [{**{key: value for key, value in row.items() if key != "request_id"},
            "caller_alive": liveness.get(index), "caller_gone_cleanup_candidate": liveness.get(index) is False}
            for index, row in enumerate(candidates)]
        accounts = {row["account_id"]: row for row in remote.get("accounts", [])}
        owners = {row["account_id"]: row for row in remote.get("owner", [])}
        result["all_four_online_verified"] = bool(remote.get("db_available") and remote.get("redis_available") and remote.get("worker_heartbeat_fresh") and all(
            accounts.get(account, {}).get("listener_connected") is True and accounts[account]["is_active"] and owners.get(account, {}).get("owner_present")
            and owners[account]["session_lease_ttl_seconds"] > 0 for account in ACCOUNTS))
        result["all_four_configured_exclusive_residential"] = bool(remote.get("db_available") and all(
            all(accounts.get(account, {}).get(key) is True for key in ("static_mode", "proxy_bound", "proxy_active", "residential", "unique_configuration"))
            and accounts[account]["active_binding_count"] == 1 for account in ACCOUNTS))
        result["proxy_exit_ips_tested"] = False
    result["diagnostics"] = read_diagnostics(Path("/opt/vanguard/data/review-diagnostics-v5/review-diagnostics-v1.bin"), since)
    result["observed_at"] = time.time()
    result["heartbeat_comparison"] = heartbeat_comparison(result["diagnostics"], prior, result["observed_at"])
    result["protection"] = protected_summary(directory)
    result["complete_business_transition_count_available"] = False
    return result


def parse_start(value: str) -> float:
    try:
        result = timestamp(float(value)) if value.replace(".", "", 1).isdigit() else timestamp(value)
    except (ValueError, OverflowError):
        result = None
    if result is None or result <= 0:
        raise argparse.ArgumentTypeError("stage_start_invalid")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Natural business followup without Telegram probes or writes")
    parser.add_argument("--stage-start", required=True, type=parse_start)
    parser.add_argument("--directory", type=Path, default=Path("/opt/vanguard/backups/review-v5-six-fix-20261104"))
    parser.add_argument("--prior-sample", type=Path)
    parser.add_argument("--container-snapshot", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--request-ids", default="[]", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 0 <= time.time() - args.stage_start <= 7 * 86400:
        raise ValueError("stage_window_invalid")
    if args.container_snapshot:
        if len(args.request_ids) > 128 * 1024:
            raise ValueError("request_limit")
        candidates = json.loads(args.request_ids)
        if not isinstance(candidates, list) or len(candidates) > REQUEST_LIMIT:
            raise ValueError("request_limit")
        print(json.dumps({"six_followup_remote": asyncio.run(remote_snapshot(args.stage_start, candidates))}, allow_nan=False))
    else:
        prior = None
        if args.prior_sample is not None:
            if args.prior_sample.stat().st_size > 512 * 1024:
                raise ValueError("prior_limit")
            value = json.loads(args.prior_sample.read_text(encoding="utf-8"))
            prior = value.get("safe_six_followup") if isinstance(value, dict) else None
        print(json.dumps({"safe_six_followup": host_snapshot(args.stage_start, args.directory, prior)}, allow_nan=False))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(json.dumps({"safe_six_followup_failure": "source_unavailable"}))
        raise SystemExit(1) from None
