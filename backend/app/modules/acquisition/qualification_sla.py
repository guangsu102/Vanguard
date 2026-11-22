"""Bounded READY-episode observations; never execution or budget authority.

Writers are called only by the independent measurement runtime. They neither
commit nor roll back the supplied session. The business actor must not await
these functions. Missing proof, source coverage and empty samples are unknown.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any

from app.modules.acquisition import qualification_ready_origin as ready_origin

PREFIX = "qualification.ready.sla.v1."
INDEX_PREFIX = "qualification.ready.sla.index.v1."
CURSOR_PREFIX = "qualification.ready.sla.cursor.v1."
ALIAS_PREFIX = "qualification.ready.sla.alias.v1."
UNRESOLVED_PREFIX = "qualification.ready.sla.unresolved.v1."
COVERAGE_PREFIX = "qualification.ready.sla.coverage.v1."
VERSION = "ready_origin_v1"
SLA_SECONDS = 120
SOURCE_LEASE_SECONDS = 120
SOURCE_HEARTBEAT_LEASE_SECONDS = 60
ORIGIN_PROOF_SKEW_SECONDS = 1
MAX_ROWS = 4096
CAPACITY_PRESSURE = "qualification.telemetry.capacity_pressure"
MAX_GAPS = 1024
MAX_ALIASES = 8192
MAX_CURSORS = 4096
MAX_UNRESOLVED = 1024
RETENTION_DAYS = 7
LOCK_NAMESPACE = 572910


def _date(value: Any) -> datetime | None:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed
    except (ValueError, TypeError, AttributeError):
        return None


def _iso(value: Any) -> str:
    parsed = _date(value)
    if parsed is None:
        raise ValueError("sla_timestamp_required")
    return parsed.isoformat(timespec="microseconds")


def _data(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    try:
        result = json.loads(value or "{}")
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def _identity(event: dict) -> tuple[int, int, int, int]:
    values = tuple(event.get(name) for name in ("account_id", "work_id", "revision", "ready_sequence"))
    if any(type(value) is not int for value in values) or any(value <= 0 for value in values[:3]) or values[3] < 0:
        raise ValueError("sla_identity_invalid")
    return values


def episode_key(account_id: int, work_id: int, revision: int, ready_sequence: int, ready_at: Any) -> str:
    identity = _identity(dict(account_id=account_id, work_id=work_id, revision=revision, ready_sequence=ready_sequence))
    digest = hashlib.sha256(json.dumps([*identity, _iso(ready_at)], separators=(",", ":")).encode()).hexdigest()
    return PREFIX + str(account_id) + "." + digest


def _key(event: dict) -> str:
    return episode_key(*_identity(event), event.get("ready_at"))


def _index_key(event: dict) -> str:
    identity = _identity(event)
    return INDEX_PREFIX + str(identity[0]) + "." + hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def _proof(event: dict, ready_at: datetime, now: datetime) -> dict:
    supplied = _data(event.get("eligibility"))
    health = supplied.get("health") if type(supplied.get("health")) is bool else None
    payable = supplied.get("payable") if type(supplied.get("payable")) is bool else None
    required = supplied.get("required_reads")
    available = supplied.get("available_reads")
    plan = supplied.get("plan_fingerprint")
    observed = _date(supplied.get("observed_at"))
    reasons = []
    if event.get("origin_unknown"):
        reasons.append("origin_unknown")
    if _date(supplied.get("anchor_ready_at")) != ready_at:
        reasons.append("origin_anchor_missing_or_mismatch")
    if observed is None or abs((observed-ready_at).total_seconds()) > ORIGIN_PROOF_SKEW_SECONDS or observed > now:
        reasons.append("origin_snapshot_missing_or_stale")
    if health is None:
        reasons.append("health_unknown")
    if payable is None:
        reasons.append("budget_unknown")
    if not isinstance(plan, str) or not plan or len(plan) > 128:
        reasons.append("next_step_plan_unknown")
        plan = None
    if type(required) is not int or required < 0:
        reasons.append("required_reads_unknown")
        required = None
    elif required == 0:
        if supplied.get("explicit_local_zero_plan") is not True:
            reasons.append("zero_is_not_a_plan")
        if payable is not True:
            reasons.append("zero_plan_payability_conflict")
    elif type(available) is not int or available < 0:
        reasons.append("available_reads_unknown")
        available = None
    elif payable is not None and payable != (available >= required):
        reasons.append("payability_proof_conflict")
    return {"state": "unknown" if reasons else "eligible" if health and payable else "blocked",
        "health": health, "payable": payable, "required_reads": required,
        "available_reads": available if type(available) is int and available >= 0 else None,
        "plan_fingerprint": plan, "observed_at": observed.isoformat() if observed else None,
        "anchor_ready_at": ready_at.isoformat(), "version": VERSION,
        "explicit_local_zero_plan": supplied.get("explicit_local_zero_plan") is True,
        "unknown_reasons": sorted(set(reasons))}


def _new(event: dict, now: datetime, *, origin_received: bool = True) -> dict:
    identity = _identity(event)
    ready = _date(event.get("ready_at"))
    if ready is None:
        raise ValueError("sla_timestamp_required")
    family = event.get("family", "qualification")
    scope = event.get("scope_hash")
    return {"version": VERSION, "episode_key": _key(event), "account_id": identity[0],
        "work_id": identity[1], "revision": identity[2], "ready_sequence": identity[3],
        "family": family if family in {"qualification", "business_maintenance"} else "unknown",
        "scope_hash": scope if isinstance(scope, str) and 0 < len(scope) <= 128 else None,
        "ready_at": ready.isoformat(), "observed_at": now.isoformat(),
        "origin_state": event.get("origin_state") if event.get("origin_state") in {"READY", "WAIT_BUDGET", "WAIT_PROVIDER", "WAIT_MATURITY"} else "READY",
        "origin_unknown": event.get("origin_unknown") is True,
        "origin_time_unknown": event.get("origin_time_unknown") is True,
        "origin_event_received": origin_received, "eligibility": _proof(event, ready, now),
        "first_actual_step_at": None, "start_proof": None,
        "closed_at": None, "outcome": None, "reason": None, "invalid_reasons": []}


def _merge_origin(row: dict, event: dict, now: datetime) -> None:
    """Only authentic origin proof may arrive late; a later snapshot cannot."""
    if event.get("preserve_origin"):
        return
    row["origin_event_received"] = True
    if event.get("origin_time_unknown"):
        row["origin_time_unknown"] = True
    if row.get("origin_unknown") or event.get("origin_unknown"):
        row["origin_unknown"] = True
        row["eligibility"]["state"] = "unknown"
        row["eligibility"]["unknown_reasons"] = sorted(set(row["eligibility"].get("unknown_reasons", []) + ["origin_unknown"]))
        return
    supplied = _proof(event, _date(row["ready_at"]), now)
    current = row.get("eligibility") or {}
    if supplied["state"] == "unknown":
        return  # A late fresh snapshot never changes historical qualification.
    if current.get("state") == "unknown":
        row["eligibility"] = supplied
    elif any(current.get(name) != supplied.get(name) for name in ("health", "payable", "required_reads", "available_reads", "plan_fingerprint")):
        row["invalid_reasons"] = sorted(set(row.get("invalid_reasons", []) + ["conflicting_origin_proof"]))


def _merge_start(row: dict, event: dict, now: datetime) -> tuple[bool, str | None]:
    # Claims and owner-queue admission are expected diagnostics, not bad data.
    if event.get("actor_verified") is not True or event.get("step_entered") is not True:
        return False, "no_actual_step"
    if event.get("budget_granted") is not True and event.get("explicit_local_zero_plan") is not True:
        return False, "step_not_funded_or_local"
    started = _date(event.get("started_at"))
    ready = _date(row.get("ready_at"))
    fingerprint = event.get("plan_fingerprint")
    claim = event.get("claim_token")
    supplied_hash = event.get("claim_hash")
    hashed_claim = supplied_hash if isinstance(supplied_hash, str) and len(supplied_hash) == 64 and all(char in "0123456789abcdef" for char in supplied_hash) else None
    invalid = None
    if event.get("actor_account") != row["account_id"]:
        invalid = "actor_account_mismatch"
    elif not row.get("scope_hash") or event.get("scope_hash") != row.get("scope_hash"):
        invalid = "scope_mismatch"
    elif hashed_claim is None and (not isinstance(claim, str) or not 0 < len(claim) <= 256):
        invalid = "claim_generation_missing"
    elif not isinstance(fingerprint, str) or not 0 < len(fingerprint) <= 128:
        invalid = "step_plan_missing"
    elif row.get("eligibility", {}).get("plan_fingerprint") not in {None, fingerprint}:
        invalid = "step_plan_mismatch"
    elif started is None or started < ready or started > now:
        invalid = "start_timestamp_invalid"
    elif event.get("explicit_local_zero_plan") is True and event.get("budget_granted") is not True and not (
        type(event.get("required_reads")) is int and event.get("required_reads") == 0
    ):
        invalid = "local_zero_plan_unproved"
    if invalid:
        row["invalid_reasons"] = sorted(set(row.get("invalid_reasons", []) + [invalid]))
        return False, invalid
    if row.get("first_actual_step_at"):
        if started < _date(row["first_actual_step_at"]):
            row["invalid_reasons"] = sorted(set(row.get("invalid_reasons", []) + ["earlier_start_conflicts_with_immutable_first"]))
            return False, "immutable_first_start_conflict"
        return False, "duplicate_or_later_step"
    row["first_actual_step_at"] = started.isoformat()
    row["start_proof"] = {"actor_account": event["actor_account"], "actor_verified": True,
        "step_entered": True, "budget_granted": event.get("budget_granted") is True,
        "explicit_local_zero_plan": event.get("explicit_local_zero_plan") is True,
        "plan_fingerprint": fingerprint, "claim_hash": hashed_claim or hashlib.sha256(claim.encode()).hexdigest(),
        "health_verified": event.get("health_verified") is True,
        "health_observed_at": (_date(event.get("health_observed_at")) or datetime.min).isoformat(),
        "budget_observed_at": (_date(event.get("budget_observed_at")) or datetime.min).isoformat(),
        "required_reads": event.get("required_reads") if type(event.get("required_reads")) is int and event["required_reads"] >= 0 else None}
    # A granted budget now must never upgrade an unknown budget at READY origin.
    return True, None


def evaluate(rows: list[dict], coverage: dict | None, now: datetime, start: datetime,
             *, window_end: datetime | None = None) -> dict:
    """Pure strict projection. All-READY success is diagnostic, never the SLO."""
    now, start = _date(now), _date(start)
    end = _date(window_end) if window_end is not None else now
    if now is None or start is None or end is None or start > end or end > now:
        raise ValueError("sla_window_invalid")
    coverage = coverage or {}
    reasons = []
    if coverage.get("transaction_frontier_verified") is not True:
        reasons.append("transaction_frontier_unverified")
    if start < now-timedelta(days=RETENTION_DAYS):
        reasons.append("retention_window_exceeded")
    first, last = _date(coverage.get("started_at")), _date(coverage.get("last_scan_at"))
    if first is None or first > start:
        reasons.append("observation_window_not_covered")
    if last is None or last > now or (now-last).total_seconds() > SOURCE_LEASE_SECONDS:
        reasons.append("source_scan_stale")
    if coverage.get("complete_override") is False:
        reasons.append("source_coverage_incomplete")
    if "expected_source_counts" in coverage:
        if coverage.get("source_inventory_verified") is not True:
            reasons.append("source_inventory_unverified")
        observed = _date(coverage.get("source_observed_at"))
        if observed is None or observed > now or (now-observed).total_seconds() > SOURCE_HEARTBEAT_LEASE_SECONDS:
            reasons.append("source_heartbeat_stale")
        if type(coverage.get("source_pending_events")) is not int or coverage["source_pending_events"] != 0:
            reasons.append("source_events_pending")
        confirmed = _date(coverage.get("source_confirmed_through"))
        if confirmed is None or confirmed > now or confirmed < end:
            reasons.append("source_window_unconfirmed")
    if coverage.get("window_not_yet_confirmed"):
        reasons.append("requested_window_not_yet_confirmed")
    if coverage.get("unresolved_references", 0) or coverage.get("unresolved_reference_overflow"):
        reasons.append("unresolved_episode_references")
    if coverage.get("denominator_loss"):
        reasons.append("unrecovered_denominator_loss")
    unknown_until = _date(coverage.get("unknown_until"))
    if unknown_until and unknown_until >= start:
        reasons.append("coverage_history_unknown")
    if any((_date(gap.get("start")) or datetime.min) < end and (_date(gap.get("end")) or datetime.max) > start
           for gap in coverage.get("gaps", []) if isinstance(gap, dict)):
        reasons.append("persisted_measurement_gap")
    coverage_reasons = list(reasons)
    selected, seen = [], set()
    invalid_rows = 0
    for candidate in rows:
        row = _data(candidate)
        key = row.get("episode_key")
        ready = _date(row.get("ready_at"))
        if not key or ready is None:
            invalid_rows += 1
            continue
        if key in seen:
            invalid_rows += 1
            continue
        seen.add(key)
        if ready > end:
            continue
        actual, closed = _date(row.get("first_actual_step_at")), _date(row.get("closed_at"))
        if ready >= start or actual is None or actual >= start or (closed and closed >= start):
            selected.append(row)
    counts = dict(eligible_mature=0, started_within_120=0, late=0, never_started=0,
        failed=0, unknown=0, known_blocked=0, young=0, total=len(selected), carried_pending=0,
        potential_mature_never_started=0, potential_mature_late=0)
    diagnostic_started = diagnostic_mature = verified_timely = 0
    lower_bound_reasons = list(coverage_reasons)
    for row in selected:
        ready = _date(row["ready_at"])
        actual = _date(row.get("first_actual_step_at"))
        if actual is not None and end < actual <= now:
            actual = None  # A later start cannot rewrite an earlier report.
        proof = row.get("eligibility") or {}
        identity_unknown = bool(row.get("origin_time_unknown") or row.get("invalid_reasons") or row.get("origin_event_received") is not True)
        unknown = bool(row.get("origin_unknown") or identity_unknown
            or proof.get("state") not in {"eligible", "blocked"})
        start_proof = row.get("start_proof") or {}
        actual_valid = actual is not None and (ready <= actual <= end and start_proof.get("actor_verified") is True
                and start_proof.get("actor_account") == row.get("account_id") and start_proof.get("step_entered") is True
                and (start_proof.get("budget_granted") is True or start_proof.get("explicit_local_zero_plan") is True)
                and bool(start_proof.get("plan_fingerprint")) and bool(start_proof.get("claim_hash")))
        if actual is not None and not actual_valid:
            unknown = True
            identity_unknown = True
        if identity_unknown:
            lower_bound_reasons.append("ready_identity_or_start_evidence_unknown")
        counts["unknown"] += int(unknown)
        counts["carried_pending"] += int(ready < start and actual is None)
        mature = ready + timedelta(seconds=SLA_SECONDS) <= end
        if not mature:
            counts["young"] += 1
            continue
        diagnostic_mature += 1
        counts["potential_mature_never_started"] += int(actual is None)
        counts["potential_mature_late"] += int(actual is not None and actual > ready+timedelta(seconds=SLA_SECONDS))
        timely = actual is not None and actual <= ready+timedelta(seconds=SLA_SECONDS)
        diagnostic_started += int(timely and actual_valid and not row.get("invalid_reasons"))
        if timely and actual_valid and not row.get("invalid_reasons"):
            health_at = _date(start_proof.get("health_observed_at"))
            budget_at = _date(start_proof.get("budget_observed_at"))
            required = start_proof.get("required_reads")
            health_proved = (start_proof.get("health_verified") is True and health_at is not None
                and health_at <= now and abs((health_at-actual).total_seconds()) <= ORIGIN_PROOF_SKEW_SECONDS)
            local_zero = start_proof.get("explicit_local_zero_plan") is True and required == 0
            plan_proved = type(required) is int and (required > 0 or local_zero) and bool(start_proof.get("plan_fingerprint"))
            budget_proved = (start_proof.get("budget_granted") is True and budget_at is not None
                and budget_at <= now and abs((budget_at-actual).total_seconds()) <= ORIGIN_PROOF_SKEW_SECONDS)
            verified_timely += int(health_proved and plan_proved and (budget_proved or local_zero))
        closed = _date(row.get("closed_at"))
        counts["failed"] += int(row.get("outcome") in {"failed", "cancelled"} and (closed is None or closed <= end))
        if unknown:
            continue
        if proof["state"] == "blocked":
            counts["known_blocked"] += 1
            continue
        counts["eligible_mature"] += 1
        if timely:
            counts["started_within_120"] += 1
        elif actual is not None:
            counts["late"] += 1
        else:
            counts["never_started"] += 1
    if counts["unknown"]:
        reasons.append("origin_or_start_evidence_unknown")
    if invalid_rows:
        reasons.append("measurement_rows_invalid")
        lower_bound_reasons.append("measurement_rows_invalid")
    complete = not reasons
    denominator = counts["eligible_mature"]
    rate = counts["started_within_120"]/denominator if complete and denominator else None
    if not diagnostic_mature:
        lower_bound_reasons.append("no_mature_potential_episodes")
    if not verified_timely:
        lower_bound_reasons.append("no_verified_timely_starts")
    lower = verified_timely/diagnostic_mature if not lower_bound_reasons else None
    return {"version": VERSION, "window_start": start.isoformat(), "window_end": end.isoformat(),
        "observed_at": now.isoformat(),
        "coverage_complete": complete, "incomplete_reasons": sorted(set(reasons)), **counts,
        "transaction_frontier_verified": coverage.get("transaction_frontier_verified") is True,
        "strict_rate": rate, "rate_type": "origin_eligible_exact",
        "status": "pass" if rate is not None and rate >= .95 else "fail" if rate is not None else "not_evaluable",
        "guaranteed_lower_bound": lower,
        "guaranteed_lower_bound_rate_type": "eligible_during_ready_interval_lower_bound",
        "guaranteed_lower_bound_contract": "eligible at least once in READY interval; NOT origin-eligible subgroup",
        "guaranteed_lower_bound_numerator": verified_timely,
        "guaranteed_lower_bound_denominator": diagnostic_mature,
        "guaranteed_lower_bound_incomplete_reasons": sorted(set(lower_bound_reasons)),
        "guaranteed_lower_bound_status": "proved_at_least_threshold" if lower is not None and lower >= .95 else "insufficient_lower_bound" if lower is not None else "not_evaluable",
        "threshold": .95, "deadline_seconds": SLA_SECONDS, "invalid_rows": invalid_rows,
        "diagnostic_all_ready": {"is_acceptance_metric": False, "mature": diagnostic_mature,
            "observed_started_within_120": diagnostic_started,
            "observed_start_fraction": diagnostic_started/diagnostic_mature if diagnostic_mature else None,
            "includes_unknown_and_blocked": True}}


async def _lock(db: Any, account_id: int) -> None:
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy import text
        await db.execute(text("SELECT pg_advisory_xact_lock(:namespace,:account)"), {"namespace": LOCK_NAMESPACE, "account": account_id})


def _save(row: Any, value: dict) -> None:
    row.value = json.dumps(value, sort_keys=True, separators=(",", ":"))


async def _rows(db: Any, account_id: int, *, limit: int | None = None) -> list:
    from sqlalchemy import select
    from app.core.settings_models import SystemSetting
    query = select(SystemSetting).where(SystemSetting.key.startswith(PREFIX+str(account_id)+".")).order_by(SystemSetting.key)
    return list((await db.scalars(query.limit(MAX_ROWS+1 if limit is None else limit))).all())


async def note_gap(db: Any, account_id: int, now: datetime, reason: str, *, since: datetime | None = None) -> None:
    from app.core.settings_models import SystemSetting
    now = _date(now)
    await _lock(db, account_id)
    key = COVERAGE_PREFIX+str(account_id)
    row = await db.get(SystemSetting, key)
    value = _data(row.value) if row else {"started_at": now.isoformat(), "gaps": []}
    gaps = [gap for gap in value.get("gaps", []) if (_date(gap.get("end")) or datetime.max) >= now-timedelta(days=2)]
    gaps.append({"start": (_date(since) or now-timedelta(microseconds=1)).isoformat(),
        "end": now.isoformat(), "reason": reason[:100]})
    if len(gaps) > MAX_GAPS:
        value["unknown_until"] = (now+timedelta(days=1)).isoformat()
    value["gaps"] = gaps[-MAX_GAPS:]
    if reason in {"episode_capacity_exhausted", "alias_capacity_exhausted", "cursor_capacity_exhausted",
                  "unresolved_reference_capacity_exhausted", "census_truncated", "transport_event_lost"}:
        value["denominator_loss"] = True
    if row is None:
        row = SystemSetting(key=key, value="{}")
        db.add(row)
    _save(row, value)
    await db.flush()


async def _get_or_create(db: Any, event: dict, now: datetime, *, origin_received: bool) -> tuple[Any, dict, str | None]:
    from app.core.settings_models import SystemSetting
    account = _identity(event)[0]
    await _lock(db, account)
    key, index_key = _key(event), _index_key(event)
    index = await db.get(SystemSetting, index_key)
    if index:
        original = _data(index.value)
        if original.get("episode_key") != key:
            old = await db.get(SystemSetting, original.get("episode_key"))
            if old:
                value = _data(old.value)
                value["invalid_reasons"] = sorted(set(value.get("invalid_reasons", []) + ["immutable_ready_conflict"]))
                _save(old, value)
            await note_gap(db, account, now, "immutable_ready_conflict", since=_date(original.get("ready_at")))
            return None, {}, "immutable_ready_conflict"
    row = await db.get(SystemSetting, key)
    if row:
        value = _data(row.value)
        if value.get("scope_hash") != event.get("scope_hash"):
            value["invalid_reasons"] = sorted(set(value.get("invalid_reasons", []) + ["scope_identity_conflict"]))
            _save(row, value)
        return row, value, None
    from sqlalchemy import delete, func, select
    # Count actual transaction contents, not a truncated history or a cache that
    # could survive flush/commit/rollback. Historical JSON is read only at cap.
    await db.flush()
    count = await db.scalar(select(func.count()).select_from(SystemSetting).where(
        SystemSetting.key.startswith(PREFIX+str(account)+".")))
    if count+1 > MAX_ROWS-4:
        db.info[CAPACITY_PRESSURE] = True
    retired = 0
    if count == MAX_ROWS:
        from app.modules.acquisition.qualification_cost import _json_text_field, _postgres_json_rows

        if db.get_bind().dialect.name == "postgresql":
            table = _postgres_json_rows(SystemSetting.key.startswith(PREFIX+str(account)+"."))
            key_column, value_column = table.c.key, table.c.value
            started = table.c.payload["first_actual_step_at"].astext
            closed_at = table.c.payload["closed_at"].astext
        else:
            table = SystemSetting
            key_column, value_column = SystemSetting.key, SystemSetting.value
            started = _json_text_field(db, SystemSetting.value, "first_actual_step_at")
            closed_at = _json_text_field(db, SystemSetting.value, "closed_at")
        rows = list((await db.execute(select(key_column, value_column).select_from(table).where(
            key_column.startswith(PREFIX+str(account)+"."),
            started.is_not(None), started != "",
            closed_at < (now-timedelta(days=RETENTION_DAYS)).isoformat(),
        ).order_by(key_column).limit(16))).all())
        retired_keys = []
        # Only ended AND actually started observations may retire. Never-started
        # failures remain unresolved startup obligations and keep their denominator.
        for candidate in rows:
            value = _data(candidate.value)
            closed = _date(value.get("closed_at"))
            if retired < 16 and value.get("first_actual_step_at") and closed and closed < now-timedelta(days=RETENTION_DAYS):
                old_index = INDEX_PREFIX+str(account)+"."+hashlib.sha256(json.dumps((account, value.get("work_id"), value.get("revision"), value.get("ready_sequence"))).encode()).hexdigest()
                retired_keys.extend((old_index, candidate.key))
                retired += 1
                if retired >= 16:
                    break
        if retired_keys:
            await db.execute(delete(SystemSetting).where(SystemSetting.key.in_(retired_keys)).execution_options(
                synchronize_session="fetch"))
    if count-retired >= MAX_ROWS:
        await note_gap(db, account, now, "episode_capacity_exhausted")
        return None, {}, "episode_capacity_exhausted"
    value = _new(event, now, origin_received=origin_received)
    row = SystemSetting(key=key, value="{}")
    db.add(row)
    if index is None:
        index = SystemSetting(key=index_key, value="{}")
        db.add(index)
    _save(index, {"episode_key": key, "ready_at": value["ready_at"]})
    _save(row, value)
    await db.flush()
    return row, value, None


async def record_ready(db: Any, event: dict, now: datetime) -> dict:
    now = _date(now)
    row, value, reason = await _get_or_create(db, event, now, origin_received=True)
    if row is not None:
        _merge_origin(value, event, now)
        _save(row, value)
        await db.flush()
    return {"recorded": row is not None, "episode_key": _key(event), "reason": reason}


async def record_eligibility(db: Any, event: dict, now: datetime) -> dict:
    """Deliver an authentic origin proof; never recompute historical eligibility."""
    return await record_ready(db, event, now)


async def record_started(db: Any, event: dict, now: datetime) -> dict:
    now = _date(now)
    row, value, reason = await _get_or_create(db, event, now, origin_received=False)
    recorded = False
    if row is not None:
        recorded, reason = _merge_start(value, event, now)
        _save(row, value)
        await db.flush()
    return {"recorded": recorded, "episode_key": _key(event), "reason": reason}


async def record_finished(db: Any, event: dict, now: datetime) -> dict:
    now = _date(now)
    row, value, reason = await _get_or_create(db, event, now, origin_received=False)
    if row is not None and value.get("closed_at") is None:
        closed = _date(event.get("closed_at"))
        if closed is None or not _date(value["ready_at"]) <= closed <= now:
            value["invalid_reasons"] = sorted(set(value.get("invalid_reasons", []) + ["closed_timestamp_invalid"]))
            reason = "closed_timestamp_invalid"
        else:
            value["closed_at"] = closed.isoformat()
            value["outcome"] = event.get("outcome") if event.get("outcome") in {"completed", "failed", "cancelled", "stale", "held", "allowed", "trial", "reject", "observe", "protected"} else "unknown"
            # Reasons are enums only. No arbitrary exception/message text.
            supplied = event.get("reason")
            value["reason"] = supplied if isinstance(supplied, str) and len(supplied) <= 100 and all(char.isalnum() or char in "_:.-" for char in supplied) else "unspecified"
        _save(row, value)
        await db.flush()
    return {"recorded": row is not None and reason is None, "episode_key": _key(event), "reason": reason}


async def report(db: Any, account_id: int, now: datetime, *, since: datetime | None = None) -> dict:
    """Read-only projection: no lock, housekeeping, flush, commit or rollback."""
    from app.core.settings_models import SystemSetting
    now = _date(now)
    start = _date(since) if since is not None else now-timedelta(hours=24) if now is not None else None
    if now is None or start is None or start > now:
        raise ValueError("sla_window_invalid")
    with db.no_autoflush:
        marker = await db.get(SystemSetting, COVERAGE_PREFIX+str(account_id))
        coverage = _data(marker.value) if marker else {}
        rows = await _rows(db, account_id)
    if len(rows) > MAX_ROWS:
        coverage = {**coverage, "complete_override": False}
    confirmed = _date(coverage.get("source_confirmed_through"))
    end = min(now, confirmed) if "expected_source_counts" in coverage and confirmed is not None else now
    effective_start = min(start, end)
    if end < start:
        coverage = {**coverage, "window_not_yet_confirmed": True}
    result = evaluate([_data(row.value) for row in rows], coverage, now, effective_start, window_end=end)
    result["available"] = marker is not None
    result["bounded_row_limit"] = MAX_ROWS
    result["requested_start"] = start.isoformat()
    result["requested_end"] = now.isoformat()
    result["effective_end"] = end.isoformat()
    result["confirmed_through"] = confirmed.isoformat() if confirmed is not None else None
    result["report_lag_seconds"] = (now-end).total_seconds()
    def counts(name):
        values = coverage.get(name)
        if not isinstance(values, dict):
            return None
        return {role: count for role, count in list(values.items())[:512]
            if isinstance(role, str) and 1 <= len(role) <= 64
            and all(char.isalnum() or char in "_:.-" for char in role)
            and type(count) is int and 0 <= count <= 512}
    def stamp(name):
        parsed = _date(coverage.get(name))
        return parsed.isoformat() if parsed is not None else None
    def number(name, maximum):
        value = coverage.get(name, 0)
        return value if type(value) is int and 0 <= value <= maximum else None
    reason = coverage.get("source_inventory_reason")
    if reason is not None and (not isinstance(reason, str) or len(reason) > 100
            or not all(char.isalnum() or char in "_:.-" for char in reason)):
        reason = "unspecified"
    gaps = coverage.get("gaps")
    result["source_coverage"] = {"expected_source_counts": counts("expected_source_counts"),
        "active_source_counts": counts("active_source_counts"),
        "inventory_verified": coverage.get("source_inventory_verified") is True,
        "transaction_frontier_verified": coverage.get("transaction_frontier_verified") is True,
        "source_observed_at": stamp("source_observed_at"),
        "source_confirmed_through": stamp("source_confirmed_through"),
        "source_pending_events": number("source_pending_events", 2**31-1),
        "started_at": stamp("started_at"), "last_scan_at": stamp("last_scan_at"),
        "gap_count": min(len(gaps), MAX_GAPS+1) if isinstance(gaps, list) else 0,
        "denominator_loss": bool(coverage.get("denominator_loss")),
        "unresolved_references": number("unresolved_references", MAX_UNRESOLVED+1),
        "inventory_reason": reason}
    configured_epoch = ready_origin.epoch() or {}
    result["ready_origin_cohort"] = ready_origin.cohort_report(
        [_data(row.value) for row in rows], configured_epoch.get("id"),
        window_start=effective_start, window_end=end)
    return result


async def summary(db: Any, account_id: int, now: datetime, *, hours: int = 24) -> dict:
    parsed = _date(now)
    if parsed is None:
        raise ValueError("sla_window_invalid")
    return await report(db, account_id, parsed, since=parsed-timedelta(hours=hours))


def _link_key(prefix: str, event: dict, *, with_time: bool) -> str:
    account, work, revision, _ = _identity(event)
    seed = [account, work, revision, *([_iso(event.get("ready_at"))] if with_time else [])]
    return prefix+str(account)+"."+hashlib.sha256(json.dumps(seed).encode()).hexdigest()


async def _prefix_rows(db: Any, prefix: str, account: int, cap: int) -> list:
    from sqlalchemy import select
    from app.core.settings_models import SystemSetting
    return list((await db.scalars(select(SystemSetting).where(SystemSetting.key.startswith(prefix+str(account)+".")).limit(cap+1))).all())


async def _prefix_count(db: Any, prefix: str, account: int) -> int:
    from sqlalchemy import func, select
    from app.core.settings_models import SystemSetting
    return int(await db.scalar(select(func.count(SystemSetting.key)).where(SystemSetting.key.startswith(prefix+str(account)+"."))) or 0)


async def _coverage_update(db: Any, account: int, now: datetime, **updates: Any) -> None:
    from app.core.settings_models import SystemSetting
    key = COVERAGE_PREFIX+str(account)
    row = await db.get(SystemSetting, key)
    value = _data(row.value) if row else {"started_at": now.isoformat(), "gaps": []}
    value.update(updates)
    if row is None:
        row = SystemSetting(key=key, value="{}")
        db.add(row)
    _save(row, value)
    await db.flush()


def _canonical(event: dict, pointer: dict, *, preserve_origin: bool = False) -> dict:
    result = {**event, "ready_sequence": pointer["ready_sequence"], "ready_at": pointer["ready_at"],
        "scope_hash": pointer["scope_hash"]}
    if preserve_origin:
        result["preserve_origin"] = True
    return result


def _pending_event(event: dict) -> dict:
    """Retain only bounded measurement proof, never an arbitrary payload."""
    result = {name: event.get(name) for name in ("account_id", "work_id", "revision", "ready_sequence",
        "ready_at", "scope_hash", "family", "started_at", "closed_at", "actor_verified", "actor_account",
        "step_entered", "budget_granted", "explicit_local_zero_plan", "plan_fingerprint", "required_reads",
        "health_verified", "health_observed_at", "budget_observed_at", "outcome")}
    for name in ("scope_hash", "family", "plan_fingerprint", "outcome"):
        raw = result[name]
        result[name] = raw if isinstance(raw, str) and len(raw) <= 128 else None
    for name in ("ready_at", "started_at", "closed_at", "health_observed_at", "budget_observed_at"):
        parsed = _date(result[name])
        result[name] = parsed.isoformat() if parsed else None
    raw_claim = event.get("claim_token")
    if isinstance(raw_claim, str) and 0 < len(raw_claim) <= 256:
        result["claim_hash"] = hashlib.sha256(raw_claim.encode()).hexdigest()
    return result


async def prune_retired_links(db: Any, account: int) -> int:
    """Bound maintenance: remove only aliases/cursors whose episode retired."""
    from sqlalchemy import select
    from app.core.settings_models import SystemSetting
    await _lock(db, account)
    links = [*await _prefix_rows(db, ALIAS_PREFIX, account, MAX_ALIASES),
             *await _prefix_rows(db, CURSOR_PREFIX, account, MAX_CURSORS)]
    keys = {value.get("episode_key") for row in links if (value := _data(row.value)).get("episode_key")}
    existing = set((await db.scalars(select(SystemSetting.key).where(SystemSetting.key.in_(keys)))).all()) if keys else set()
    retired = 0
    for row in links:
        if retired < 1000 and _data(row.value).get("episode_key") not in existing:
            await db.delete(row)
            retired += 1
    if retired:
        await db.flush()
    return retired


async def _unresolved(db: Any, kind: str, event: dict, now: datetime) -> None:
    from app.core.settings_models import SystemSetting
    account = _identity(event)[0]
    key = _link_key(UNRESOLVED_PREFIX, event, with_time=True)
    row = await db.get(SystemSetting, key)
    if row is None:
        count = await _prefix_count(db, UNRESOLVED_PREFIX, account)
        if count >= MAX_UNRESOLVED:
            await note_gap(db, account, now, "unresolved_reference_capacity_exhausted")
            await _coverage_update(db, account, now, unresolved_reference_overflow=True)
            return
        row = SystemSetting(key=key, value="{}")
        db.add(row)
        value = {"observed_at": now.isoformat(), "events": {}, "reference_only": True}
        await _coverage_update(db, account, now, unresolved_references=count+1)
    else:
        value = _data(row.value)
    value.setdefault("events", {}).setdefault(kind, _pending_event(event))
    _save(row, value)
    await note_gap(db, account, now, "unresolved_episode_reference")
    await db.flush()


async def _replay_unresolved(db: Any, source: dict, pointer: dict, now: datetime) -> None:
    from app.core.settings_models import SystemSetting
    key = _link_key(UNRESOLVED_PREFIX, source, with_time=True)
    row = await db.get(SystemSetting, key)
    if row is None:
        return
    for kind, original in _data(row.value).get("events", {}).items():
        event = _canonical(original, pointer)
        if kind == "started":
            await record_started(db, event, now)
        elif kind == "finished":
            await record_finished(db, event, now)
    await db.delete(row)
    await db.flush()
    account = _identity(source)[0]
    remaining = await _prefix_count(db, UNRESOLVED_PREFIX, account)
    await _coverage_update(db, account, now, unresolved_references=remaining)


async def resolve_event(db: Any, kind: str, event: dict, now: datetime) -> dict | None:
    """Resolve live timestamp aliases without making them historical authority.

    A READY observation establishes the origin. Start/finish are references
    only. This function runs in the independent writer, never the actor. New
    READY episodes are recorded atomically before publishing their pointer.
    The caller may idempotently call record_ready again with the returned row.
    """
    from app.core.settings_models import SystemSetting
    if kind not in {"ready", "started", "finished"}:
        raise ValueError("sla_event_kind_invalid")
    now = _date(now)
    account, work, revision, _ = _identity(event)
    await _lock(db, account)
    source_at = _date(event.get("ready_at"))
    alias_key = _link_key(ALIAS_PREFIX, event, with_time=True)
    alias = await db.get(SystemSetting, alias_key)
    if alias is not None:
        pointer = _data(alias.value)
        episode = await db.get(SystemSetting, pointer.get("episode_key"))
        if episode is None:
            await note_gap(db, account, now, "retired_episode_reference")
            return None
        if event.get("scope_hash") != pointer.get("scope_hash"):
            await note_gap(db, account, now, "scope_reference_mismatch")
            return None
        if kind == "ready" and event.get("recovery_only"):
            return None  # A census cannot downgrade authentic origin proof.
        return _canonical(event, pointer, preserve_origin=kind == "ready" and source_at != _date(pointer["ready_at"]))
    if kind != "ready":
        await _unresolved(db, kind, event, now)
        return None
    if event.get("origin_time_unknown") or event.get("origin_state", "READY") not in {"READY", "WAIT_BUDGET", "WAIT_MATURITY"}:
        await note_gap(db, account, now, "ready_origin_unproved")
        await _coverage_update(db, account, now, denominator_loss=True)
        return None
    cursor_key = _link_key(CURSOR_PREFIX, event, with_time=False)
    cursor = await db.get(SystemSetting, cursor_key)
    current = _data(cursor.value) if cursor else None
    prior = await db.get(SystemSetting, current.get("episode_key")) if current else None
    if cursor is not None and prior is None:
        await db.delete(cursor)
        await db.flush()
        cursor, current = None, None
    sequence, canonical_at, preserve = 0, source_at, False
    if current and prior:
        prior_value = _data(prior.value)
        if event.get("scope_hash") != current.get("scope_hash"):
            await note_gap(db, account, now, "scope_reference_mismatch")
            return None
        previous_start = _date(prior_value.get("first_actual_step_at"))
        if previous_start is None:
            sequence, canonical_at, preserve = current["ready_sequence"], _date(current["ready_at"]), True
        else:
            claim = event.get("after_claim_token")
            after_verified_start = isinstance(claim, str) and hashlib.sha256(claim.encode()).hexdigest() == (prior_value.get("start_proof") or {}).get("claim_hash")
            recovered_step = event.get("recovery_only") is True and source_at > previous_start
            provider_ready = (event.get("dependency_ready_verified") == "provider_completed"
                and event.get("origin_state") == "READY" and source_at > previous_start)
            if source_at < previous_start or not (after_verified_start or recovered_step or provider_ready):
                await note_gap(db, account, now, "unproved_ready_step_transition")
                await _coverage_update(db, account, now, denominator_loss=True)
                return None
            sequence = current["ready_sequence"]+1
    if await _prefix_count(db, ALIAS_PREFIX, account) >= MAX_ALIASES:
        await prune_retired_links(db, account)
        if await _prefix_count(db, ALIAS_PREFIX, account) >= MAX_ALIASES:
            await note_gap(db, account, now, "alias_capacity_exhausted")
            return None
    if cursor is None and await _prefix_count(db, CURSOR_PREFIX, account) >= MAX_CURSORS:
        await prune_retired_links(db, account)
        if await _prefix_count(db, CURSOR_PREFIX, account) >= MAX_CURSORS:
            await note_gap(db, account, now, "cursor_capacity_exhausted")
            return None
    canonical = {**event, "ready_sequence": sequence, "ready_at": canonical_at.isoformat(), "preserve_origin": preserve}
    if event.get("recovery_only") or event.get("dependency_ready_verified") == "provider_completed":
        canonical["origin_unknown"] = True
    admitted = await record_ready(db, canonical, now)
    if not admitted["recorded"]:
        return None
    pointer = {"episode_key": admitted["episode_key"], "account_id": account, "work_id": work,
        "revision": revision, "scope_hash": event.get("scope_hash"), "ready_sequence": sequence,
        "ready_at": canonical_at.isoformat()}
    alias = SystemSetting(key=alias_key, value="{}")
    db.add(alias)
    _save(alias, {**pointer, "reference_ready_at": source_at.isoformat()})
    if cursor is None:
        cursor = SystemSetting(key=cursor_key, value="{}")
        db.add(cursor)
    _save(cursor, pointer)
    await db.flush()
    await _replay_unresolved(db, event, pointer, now)
    return None if event.get("recovery_only") and preserve else canonical
