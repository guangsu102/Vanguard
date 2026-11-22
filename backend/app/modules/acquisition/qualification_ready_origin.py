"""Local R21 candidate: bounded checkpoint facts, never execution authority."""
from __future__ import annotations

from datetime import datetime, timezone
from functools import wraps
import hashlib
import json
import os
from typing import Any, Callable
from uuid import uuid4

NAMESPACE = "ready_origin_r21"
ENV = "QUALIFICATION_READY_ORIGIN_EPOCH"
LIMIT = 4096
READY_STATES = {"READY", "WAIT_BUDGET", "WAIT_MATURITY"}


def _date(value: Any) -> datetime | None:
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        return result.astimezone(timezone.utc).replace(tzinfo=None) if result.tzinfo else result
    except (TypeError, ValueError, AttributeError):
        return None


def _iso(value: Any) -> str | None:
    result = _date(value)
    return result.isoformat() if result else None


def _object(raw: Any) -> dict[str, Any]:
    try:
        value = raw if isinstance(raw, dict) else json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def optional(fallback: Any = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(function)
        def guarded(*args: Any, **kwargs: Any) -> Any:
            try:
                return function(*args, **kwargs)
            except Exception:
                return fallback
        return guarded
    return decorate


def epoch() -> dict[str, Any] | None:
    value = _object(os.getenv(ENV))
    name, started, accounts = value.get("id"), _date(value.get("started_at")), value.get("accounts")
    if (not isinstance(name, str) or not 1 <= len(name) <= 64 or started is None
            or not isinstance(accounts, list) or not accounts or len(accounts) > 32
            or any(type(account) is not int or account <= 0 for account in accounts)):
        return None
    return {"id": name, "started_at": started.isoformat(), "accounts": sorted(set(accounts))}


def identity(work: Any) -> dict[str, Any]:
    return {"account_id": work.account_id, "work_id": work.id, "revision": work.revision,
        "scope_hash": _hash(work.scope_key), "membership_id": work.membership_id,
        "group_id": work.group_id, "membership_incarnation": _iso(work.membership_joined_at),
        "family": work.family, "kind": work.kind}


def _same_identity(source: dict[str, Any], target: dict[str, Any]) -> bool:
    original = _object(source.get("identity"))
    if not original:
        return False
    if original.get("work_id") is None:
        # The unique business identity was committed in the initial INSERT.
        # A later real-row observation may attach its database locator only.
        return (source.get("locator_at_origin") == "pending_insert"
            and (target.get("work_id") is None or type(target.get("work_id")) is int and target["work_id"] > 0)
            and {key: value for key, value in original.items() if key != "work_id"}
                == {key: value for key, value in target.items() if key != "work_id"})
    return original == target


def _current(work: Any) -> Any:
    return _object(work.checkpoint_json).get(NAMESPACE) or {}


def _save(work: Any, value: dict[str, Any]) -> bool:
    raw = json.dumps(value, sort_keys=True, default=str)
    if len(raw.encode()) > LIMIT:
        return False
    data = _object(work.checkpoint_json)
    data[NAMESPACE] = value
    encoded = json.dumps(data, ensure_ascii=False, default=str)
    work.checkpoint_json = encoded
    return True


@optional(False)
def capture_ready(work: Any, event: dict | None, now: datetime, *, decision: dict | None = None) -> bool:
    """Pure synchronous mutation included in the caller's existing Work UPDATE.

    decision is an optional already observed, outside-transaction joint decision.
    No real READY site currently supplies that missing joint input.
    """
    configured, ready, observed = epoch(), _date(work.ready_at), _date(now)
    if (configured is None or work.account_id not in configured["accounts"]
            or work.family != "qualification" or work.state not in READY_STATES
            or getattr(work, "kind", None) == "migration"
            or ready is None or observed is None
            or ready < _date(configured["started_at"]) or observed < _date(configured["started_at"])):
        return False
    target = identity(work)
    if target["membership_incarnation"] is None:
        return False
    previous = _current(work)
    if (_same_identity(previous, target) and previous.get("ready_at") == ready.isoformat()
            and previous.get("epoch") == configured):
        if (previous.get("origin_reason") == "scheduled_due_not_observed" and observed >= ready
                and not previous.get("first_due_observed_at")):
            return _save(work, {**previous, "first_due_observed_at": observed.isoformat(),
                "due_observation_reason": "due_first_observed_late" if (observed-ready).total_seconds() > 1
                    else "due_observed_without_joint_decision"})
        return True  # Repeat hooks/claims never reset or backfill an origin.
    progress = _object(work.progress_json)
    progress = progress.get("pending_collection") or progress
    plan = progress.get("read_plan") or {}
    reason, proof = "origin_decision_missing", {}
    if ready > observed:
        reason = "scheduled_due_not_observed"
    elif (observed-ready).total_seconds() > 1:
        reason = "due_first_observed_late"
    elif isinstance(decision, dict):
        at = _date(decision.get("observed_at"))
        candidate = _object(decision.get("proof"))
        if (decision.get("identity") == target and at is not None
                and abs((at-ready).total_seconds()) <= 1 and at <= observed
                and decision.get("plan_fingerprint") == _hash(plan)
                and candidate.get("plan_fingerprint") == _hash(plan)
                and candidate.get("required_reads") == plan.get("required_reads")
                and candidate.get("anchor_ready_at") == ready.isoformat()
                and candidate.get("observed_at") == at.isoformat()):
            proof, reason = candidate, "original_joint_decision"
        else:
            reason = "origin_decision_identity_plan_or_time_mismatch"
    origin_id = uuid4().hex
    value = {"epoch": configured, "origin_id": origin_id, "identity": target,
        "locator_at_origin": "pending_insert" if target["work_id"] is None else "known_row",
        "ready_at": ready.isoformat(), "origin_state": work.state,
        "origin_observed_at": observed.isoformat(), "plan": {
            name: plan.get(name) for name in ("phase", "operation", "scope", "version", "required_reads")},
        "eligibility": proof, "origin_reason": reason,
        "overwritten_count": min(2**31-1, int(previous.get("overwritten_count", 0))
            + int(bool(previous.get("origin_id")) and previous.get("origin_id") != origin_id)),
        "actual_admission": None, "claim": None}
    return _save(work, value)


@optional(False)
def capture_claim(work: Any, event: dict | None, now: datetime, *, health: bool | None) -> bool:
    current = _current(work)
    if not _same_identity(current, identity(work)) or not work.claim_token:
        return False
    if current.get("ready_at") != _iso(work.ready_at):
        return False
    current = {**current, "claim": {"claim_hash": _hash(work.claim_token),
        "observed_at": _iso(now), "health": health if type(health) is bool else None,
        "payable": None, "reason": "claim_is_not_funded_admission"},
        "first_due_observed_at": current.get("first_due_observed_at") or _iso(now),
        "due_observation_reason": "due_first_observed_late" if (_date(now)-_date(work.ready_at)).total_seconds() > 1
            else "due_observed_without_joint_decision"}
    return _save(work, current)


@optional(False)
def capture_admission(work: Any, observation: dict | None) -> bool:
    """Use the real funded-step observation; never change origin eligibility."""
    current = _current(work)
    if not isinstance(observation, dict) or not _same_identity(current, identity(work)):
        return False
    target = identity(work)
    if (any(observation.get(key) != target[key] for key in ("account_id", "work_id", "revision", "scope_hash"))
            or not work.claim_token or _hash(observation.get("claim_token")) != _hash(work.claim_token)
            or observation.get("budget_granted") is not True or observation.get("actor_verified") is not True
            or type(observation.get("required_reads")) is not int or not 1 <= observation["required_reads"] <= 8):
        return False
    captured = {name: observation.get(name) for name in ("started_at", "health_verified", "health_observed_at",
        "budget_observed_at", "budget_granted", "required_reads", "plan_fingerprint", "actor_verified")}
    captured.update(source="existing_budget_step_entered", claim_hash=_hash(work.claim_token))
    return _save(work, {**current, "actual_admission": captured})


def enrich_reference(work: Any, event: dict | None) -> dict | None:
    if event is None:
        return None
    unknown = {**event, "origin_unknown": True, "eligibility": {}}
    unknown.pop("ready_origin_epoch", None)
    try:
        configured = epoch()
        if configured is None:
            return event  # Keep the legacy minimum-object reference contract.
        current = _current(work)
        target = identity(work)
        if (not _same_identity(current, target)
                or _date(current.get("ready_at")) != _date(event.get("ready_at"))
                or configured != current.get("epoch")):
            return unknown
        observed = {**current, "row_binding": target} if type(target["work_id"]) is int and target["work_id"] > 0 else current
        return {**event, **target, "origin_state": current["origin_state"], "ready_origin_epoch": observed,
            "eligibility": current.get("eligibility") or {},
            "origin_unknown": current.get("origin_reason") != "original_joint_decision"}
    except Exception:
        return unknown  # Optional enrichment cannot discard an existing SLA reference.


@optional()
def final_reference(work: Any, event: dict) -> dict | None:
    """Called after commit with no ORM mutation and no DB/network access."""
    if work.state not in READY_STATES | {"RUNNING"}:
        return None
    clean = {key: value for key, value in event.items() if key != "_ready_origin_work"}
    clean.update(account_id=work.account_id, work_id=work.id, revision=work.revision,
        scope_hash=_hash(work.scope_key), family=work.family, ready_at=_iso(work.ready_at), origin_state=work.state)
    return enrich_reference(work, clean)


def preserve_checkpoint(previous: str | None, replacement: str | None) -> str | None:
    try:
        data, old = _object(replacement), _object(previous)
        if NAMESPACE in old:
            data[NAMESPACE] = old[NAMESPACE]
            return json.dumps(data, ensure_ascii=False, default=str)
        return replacement
    except Exception:
        return replacement  # A measurement failure never replaces business stages.


def admission_summary(event: dict) -> dict:
    return {name: event.get(name) for name in ("started_at", "health_verified", "health_observed_at",
        "budget_observed_at", "budget_granted", "required_reads", "plan_fingerprint", "actor_verified")}


async def annotate_existing(db: Any, event: dict, episode_key: str | None, setting_model: Any) -> bool:
    """Existing independent writer only; reuse its session/episode/commit."""
    source = event.get("ready_origin_epoch")
    if not isinstance(source, dict) or not episode_key:
        return False
    binding = _object(source.get("row_binding"))
    if (not _same_identity(source, binding) or type(binding.get("work_id")) is not int
            or binding["work_id"] <= 0 or any(event.get(key) != value for key, value in binding.items())
            or _date(source.get("ready_at")) != _date(event.get("ready_at"))):
        return False
    row = await db.get(setting_model, episode_key)
    if row is None:
        return False
    value = _object(row.value)
    value["ready_origin_epoch"] = source
    if event.get("ready_origin_admission"):
        value["ready_origin_admission"] = admission_summary(event["ready_origin_admission"])
    row.value = json.dumps(value, sort_keys=True, default=str)
    return True


def cohort_report(rows: list[dict], epoch_id: str | None, *, window_start: datetime, window_end: datetime) -> dict:
    start, end = _date(window_start), _date(window_end)
    selected, invalid = [], 0
    for row in rows:
        source = _object(row.get("ready_origin_epoch"))
        if not epoch_id or _object(source.get("epoch")).get("id") != epoch_id:
            continue
        ready = _date(row.get("ready_at"))
        if start is None or end is None or ready is None or start > end:
            invalid += 1
        elif start <= ready <= end:
            if _date(source.get("ready_at")) != ready:
                invalid += 1
            else:
                selected.append({**row, "ready_origin_epoch": source})
    counts = {name: 0 for name in ("eligible", "blocked", "unknown")}
    for row in selected:
        state = _object(row.get("eligibility")).get("state", "unknown")
        counts[state if isinstance(state, str) and state in counts else "unknown"] += 1
    return {"epoch": epoch_id, "observed_cohort_count": len(selected), "origin_counts": counts,
        "window_start": _iso(start), "window_end": _iso(end), "invalid_checkpoint_rows": invalid,
        "actual_admission_observations": sum(bool(row.get("ready_origin_admission"))
            or bool(row["ready_origin_epoch"].get("actual_admission")) for row in selected),
        "checkpoint_overwrite_count": sum(value for row in selected
            if type(value := row["ready_origin_epoch"].get("overwritten_count", 0)) is int and 0 <= value < 2**31),
        "coverage": "observed_checkpoint_cohort_only", "population_coverage_complete": False,
        "overall_strict_rate": None, "cohort_rate": None,
        "limitations": ["old_origins_not_backfilled", "missing_or_overwritten_checkpoint_unknown",
            "no_original_joint_decision_at_current_ready_sites", "cohort_cannot_establish_population_95_percent"]}
