"""P5 unified release control plane for the P2-P4 growth release unit.

P5 binds the P2 Outbox lanes, the P3 read/snapshot lanes and the P4 pacing /
admin-metrics / history-governance lanes into one release unit whose account
scope is a single rollout cohort: one auto-selected canary account first,
then a stable-hash cohort at 25% / 50% / 100%, then full.

The controller is deliberately decision-only.  It never acquires a Telegram
client, never creates a send quota and never bypasses the Actor
fencing-token boundary.  Its single traffic-facing lever is the kill switch:
a Redis stopped state that removes the stopped cohort from new ``ads``/``join``
dispatch candidacy (``growth_dispatch`` reads it before publishing and before
executing a protected ticket).  Verification, survival and reconciliation
lanes keep running so unconfirmed results are resolved, never resent.

Every observe cycle samples the release health, evaluates the gates against
the pre-release 24h baseline, persists one audit ``p5_gate_evaluation`` row
and records release / cohort / gate / rollback events as structured logs and
``vanguard_p5_events_total`` counters.  Hard-stop conditions engage the kill
switch immediately; hold conditions pause the rollout for analysis.  All
pre-existing thresholds come from ``app.core.metrics.SLO`` — the gate never
keeps a second copy of an SLO number.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select

from app.core.config import settings
from app.core.metrics import COUNTERS as METRICS_COUNTERS
from app.core.metrics import SLO, record_p5_event
from app.core.metrics import SNAPSHOT as METRICS_SNAPSHOT

STATE_KEY = "vanguard:p5:rollout:v1:state"
STOPPED_KEY = "vanguard:p5:rollout:v1:stopped"
WINDOWS_PREFIX = "vanguard:p5:rollout:v1:windows:"
# Mirrors growth_dispatch.PREFIX; a unit test asserts the two stay equal so
# the gate can read dispatcher bookkeeping without importing the dispatcher.
GROWTH_PREFIX = "vanguard:growth_dispatch:v1:"
GROWTH_LAST_RUNS_KEY = GROWTH_PREFIX + "last_runs"

# Rollout policy constants.  Everything that already exists as an SLO target
# is read from app.core.metrics.SLO at evaluation time instead.
WINDOW_SECONDS = 60
KEPT_WINDOWS = 16
TREND_WINDOWS = 3
JOIN_UNKNOWN_GROWTH_WINDOWS = 2
REGRESSION_RATIO = 1.10
AD_SUCCESS_DROP_POINTS = 0.05
CONSUMPTION_RATIO = 1.20
FAIR_SHARE_MULTIPLE = 2.0
STARVATION_SECONDS = 1800
WARMUP_SECONDS = 2 * 3600
STAGE_MIN_OBSERVE_SECONDS = 2 * 3600
FULL_STABLE_SECONDS = 24 * 3600
UNEXPLAINED_GRACE_WINDOWS = 2
LEASE_BUSY_RECENT_SECONDS = 900
RECEIPT_SAMPLE_LIMIT = 5000
SNAPSHOT_STALE_SECONDS = 300

AD_UNRESOLVED_STATES = ("pending", "unknown", "sending", "reconciliation_required")
TERMINAL_RECONCILIATION_STATES = ("confirmed", "resolved")

P5_EVENTS = {
    "release_started",
    "release_completed",
    "gate_hold",
    "gate_stop",
    "cohort_changed",
    "rollback",
    "recovered",
}

# ---------------------------------------------------------------------------
# Gate verdicts
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class GateReason:
    code: str
    severity: str  # "stop" | "hold"
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "severity": self.severity, "detail": self.detail}


@dataclass(slots=True)
class GateResult:
    result: str  # "pass" | "hold" | "stop"
    reasons: list[GateReason] = field(default_factory=list)

    def as_json(self) -> list[dict[str, str]]:
        return [reason.as_dict() for reason in self.reasons]


@dataclass(slots=True)
class CanaryPick:
    """Outcome of the deterministic canary account selection."""

    account_id: int | None
    reason: str | None
    report: dict[str, Any]


def _consecutive_rises(values: Sequence[Any], rises: int) -> bool:
    """True when the last ``rises`` window-over-window deltas are positive."""
    numbers = [float(value) for value in values if value is not None]
    if len(numbers) < rises + 1:
        return False
    tail = numbers[len(numbers) - rises - 1 :]
    return all(tail[i + 1] > tail[i] for i in range(rises))


def _window_deltas(samples: Sequence[dict[str, Any]], key: str) -> list[float]:
    totals = [
        float(sample[key]) for sample in samples if sample.get(key) is not None
    ]
    return [totals[i] - totals[i - 1] for i in range(1, len(totals))]


def evaluate_gate(
    *,
    baseline: dict[str, Any],
    samples: Sequence[dict[str, Any]],
    current: dict[str, Any],
    stage_age_seconds: float | None = None,
    stage_min_seconds: float | None = None,
    slo: dict[str, Any] | None = None,
) -> GateResult:
    """Judge one observe window against the baseline, trends and SLO limits.

    Pure function: callers gather the metric vector, this only decides.  Hard
    rules engage the kill switch; regression rules hold the rollout for
    analysis.  A missing baseline value skips the relative comparison (there
    was no pre-release data to regress from).
    """
    slo = slo or SLO
    stop: list[GateReason] = []
    hold: list[GateReason] = []

    def current_value(key: str) -> Any:
        return current.get(key)

    # ---- hard stop: any single occurrence stops new cohort admission ----
    # ``samples`` already includes the current window as its last element.
    contention_total = current_value("session_lease_contention_total")
    contention_totals = [
        float(sample["session_lease_contention_total"])
        for sample in samples
        if sample.get("session_lease_contention_total") is not None
    ]
    contention_previous = (
        contention_totals[-2] if len(contention_totals) >= 2
        else baseline.get("session_lease_contention_total")
    )
    if (
        contention_previous is not None
        and contention_total is not None
        and float(contention_total) - float(contention_previous)
        > float(slo["session_lease_contention_15m_limit"])
    ):
        stop.append(GateReason("session_lease_contention", "stop",
                               detail=f"delta={float(contention_total) - float(contention_previous):.0f}"))
    invariant_failures = int(current_value("parent_group_invariant_failures") or 0)
    if invariant_failures > int(slo["parent_group_invariant_failures_limit"]):
        stop.append(GateReason("parent_group_invariant_failure", "stop",
                               detail=f"failures={invariant_failures}"))
    ad_aged = int(current_value("ad_unresolved_aged") or 0)
    if ad_aged > 0:
        stop.append(GateReason("ad_unresolved_aged", "stop",
                               detail=f"count={ad_aged}"))
    join_series = [sample.get("join_unknown") for sample in samples]
    if _consecutive_rises(join_series, JOIN_UNKNOWN_GROWTH_WINDOWS):
        stop.append(GateReason("join_unknown_growth", "stop",
                               detail=f"series={[v for v in join_series if v is not None][-3:]}"))
    for key, code in (
        ("illegal_transitions", "illegal_state_transition"),
        ("orphan_outbox", "orphan_outbox"),
        ("duplicate_active_operations", "duplicate_active_operation"),
        ("reconciliation_unexplained", "reconciliation_unexplained_aging"),
    ):
        value = int(current_value(key) or 0)
        if value > 0:
            stop.append(GateReason(code, "stop", detail=f"count={value}"))
    if current_value("infra_ready") is False:
        stop.append(GateReason("infra_unavailable", "stop",
                               detail="health/redis/database/metrics-snapshot check failed"))
    bypass = int(current_value("cohort_bypass_accounts") or 0)
    if bypass > 0:
        stop.append(GateReason("cohort_allowlist_bypass", "stop",
                               detail=f"accounts={bypass}"))

    # ---- hold: regressions against the pre-release baseline ----
    def regressed(key: str, ratio: float = REGRESSION_RATIO) -> bool:
        base = baseline.get(key)
        value = current_value(key)
        if base is None or value is None:
            return False
        return float(base) > 0 and float(value) > float(base) * ratio

    if regressed("receipt_p95_24h"):
        hold.append(GateReason("receipt_p95_regression", "hold",
                               detail=f"baseline={baseline['receipt_p95_24h']:.0f}s current={float(current_value('receipt_p95_24h')):.0f}s"))
    if regressed("outbox_oldest_age_seconds"):
        hold.append(GateReason("outbox_oldest_regression", "hold",
                               detail=f"baseline={baseline['outbox_oldest_age_seconds']:.0f}s"))
    if regressed("survival_backlog"):
        hold.append(GateReason("survival_backlog_regression", "hold",
                               detail=f"baseline={baseline['survival_backlog']:.0f}"))
    base_rate = baseline.get("ad_success_rate_24h")
    current_rate = current_value("ad_success_rate_24h")
    if (
        base_rate is not None
        and current_rate is not None
        and float(current_rate) < float(base_rate) - AD_SUCCESS_DROP_POINTS
    ):
        hold.append(GateReason("ad_success_rate_drop", "hold",
                               detail=f"baseline={float(base_rate):.3f} current={float(current_rate):.3f}"))
    if regressed("floodwait_events_24h", CONSUMPTION_RATIO):
        hold.append(GateReason("floodwait_increase", "hold",
                               detail=f"baseline={baseline['floodwait_events_24h']:.0f}"))
    base_factor = baseline.get("read_factor_avg")
    current_factor = current_value("read_factor_avg")
    if (
        base_factor is not None
        and float(base_factor) > 0
        and current_factor is not None
        and float(current_factor) > 0
        and float(current_factor) < float(base_factor) * (2.0 - CONSUMPTION_RATIO)
    ):
        hold.append(GateReason("read_budget_consumption_increase", "hold",
                               detail=f"baseline={float(base_factor):.3f} current={float(current_factor):.3f}"))

    # ---- hold: fairness and persistent-rise trends ----
    starvation = float(current_value("ads_oldest_dispatch_wait_seconds") or 0)
    if starvation > STARVATION_SECONDS:
        hold.append(GateReason("account_starvation", "hold",
                               detail=f"oldest_ads_wait={starvation:.0f}s"))
    share_ratio = current_value("fair_share_max_ratio")
    if share_ratio is not None and float(share_ratio) > FAIR_SHARE_MULTIPLE:
        hold.append(GateReason("fair_share_exceeded", "hold",
                               detail=f"max_share={float(share_ratio):.2f}x fair share"))
    # ``_consecutive_rises`` expects the cumulative series and counts
    # window-to-window deltas internally.  Passing precomputed deltas here
    # required one extra sample and silently missed a three-window rise.
    duplicate_series = [sample.get("duplicate_targets_total") for sample in samples]
    if _consecutive_rises(duplicate_series, TREND_WINDOWS):
        hold.append(GateReason("duplicate_target_rising", "hold",
                               detail=f"series={[v for v in duplicate_series if v is not None][-TREND_WINDOWS - 1:]}"))
    queue_series = [sample.get("ads_oldest_dispatch_wait_seconds") for sample in samples]
    if _consecutive_rises(queue_series, TREND_WINDOWS):
        hold.append(GateReason("queue_age_rising", "hold",
                               detail=f"series={[v for v in queue_series if v is not None][-TREND_WINDOWS:]}"))
    survival_series = [sample.get("survival_backlog") for sample in samples]
    if _consecutive_rises(survival_series, TREND_WINDOWS):
        hold.append(GateReason("survival_backlog_rising", "hold",
                               detail=f"series={[v for v in survival_series if v is not None][-TREND_WINDOWS:]}"))
    if stage_min_seconds and (stage_age_seconds is None or stage_age_seconds < stage_min_seconds):
        hold.append(GateReason("stage_min_observation_not_met", "hold",
                               detail=f"stage_age={stage_age_seconds or 0:.0f}s min={stage_min_seconds:.0f}s"))

    if stop:
        return GateResult("stop", [*hold, *stop])
    if hold:
        return GateResult("hold", hold)
    return GateResult("pass", [])


# ---------------------------------------------------------------------------
# Redis runtime state
# ---------------------------------------------------------------------------


async def read_stopped(cache: Any) -> dict[str, Any] | None:
    """The persisted kill-switch payload, or None when no stop is engaged."""
    try:
        raw = await cache.get(STOPPED_KEY)
    except Exception:
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


async def clear_stopped(cache: Any) -> None:
    try:
        await cache.delete(STOPPED_KEY)
    except Exception:
        pass


async def dispatch_blocked_accounts(client: Any) -> set[int]:
    """Accounts blocked from new ads/join tickets by the engaged kill switch."""
    stopped = await read_stopped(client)
    if not stopped:
        return set()
    return {
        int(account_id)
        for account_id in stopped.get("cohort_account_ids") or []
        if isinstance(account_id, int) and account_id > 0
    }


async def ticket_blocked(client: Any, stage: str, account_id: int) -> bool:
    """True when a protected stage ticket must not execute for this account."""
    if stage not in {"ads", "join"}:
        return False
    return int(account_id) in await dispatch_blocked_accounts(client)


async def _write_state(cache: Any, payload: dict[str, Any]) -> None:
    try:
        await cache.set(STATE_KEY, json.dumps(payload, sort_keys=True), ex=30 * 86400)
    except Exception:
        pass


async def _push_window(cache: Any, release_id: str, sample: dict[str, Any]) -> None:
    key = WINDOWS_PREFIX + release_id
    try:
        await cache.rpush(key, json.dumps(sample, sort_keys=True))
        await cache.ltrim(key, -KEPT_WINDOWS, -1)
        await cache.expire(key, 7 * 86400)
    except Exception:
        pass


async def _read_windows(cache: Any, release_id: str) -> list[dict[str, Any]]:
    try:
        raw_list = await cache.lrange(WINDOWS_PREFIX + release_id, 0, -1)
    except Exception:
        return []
    samples = []
    for raw in raw_list or []:
        try:
            sample = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(sample, dict):
            samples.append(sample)
    return samples


async def _counter_total(cache: Any, name: str, labels: tuple[str, ...] = ()) -> float:
    """One cumulative counter total from the shared Redis metrics hash."""
    key = "|".join((name, *labels))
    try:
        values = await cache.hgetall(METRICS_COUNTERS)
        return float(values.get(key, 0) or 0)
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Cohort resolution and canary selection
# ---------------------------------------------------------------------------


async def resolve_cohort(db: Any, now: datetime) -> list[int]:
    """Eligible accounts inside the configured P5 scope, sorted by id.

    ``canary`` keeps only the resolved canary accounts, ``full`` takes every
    eligible account, and ``cohort`` intersects eligibility with the stable
    hash membership (canary accounts are always members).
    """
    if not settings.p5_governs:
        return []
    from app.core.scheduler.growth_dispatch import eligible_accounts

    eligible = sorted(
        int(account_id)
        for account_id in (await db.scalars(eligible_accounts(now))).all()
    )
    if not eligible:
        return []
    mode = settings.p5_rollout_mode
    if mode == "canary":
        canary = settings.p5_canary_accounts
        return [account_id for account_id in eligible if account_id in canary]
    if mode == "full":
        return eligible
    return [
        account_id for account_id in eligible if settings.p5_cohort_member(account_id)
    ]


def canary_sort_key(candidate: Any) -> tuple:
    """Lowest risk, best read recovery, best success, least recent, then id."""
    return (
        -float(candidate.risk_component),
        -float(candidate.read_recovery_factor),
        -float(candidate.recent_success_rate),
        float(candidate.last_scheduled_at or 0.0),
        int(candidate.account_id),
    )


async def select_canary_account(
    db: Any,
    now: datetime,
    *,
    last_runs: dict[str, Any] | None = None,
) -> CanaryPick:
    """Auto-select the healthiest single account for the canary stage.

    Hard exclusions (fail closed, never a hard-coded account number): risk
    pause or restricted state, FloodWait, unrecovered read budget, aged ad
    ``pending/unknown`` rows, unresolved join outcomes, an active
    reconciliation backlog, and recent session-lease contention evidence.
    Survivors are ordered by risk, read recovery, recent success rate, least
    recent scheduling and finally account id, so the pick is unique.
    """
    from app.core.account.models import TelegramAccount
    from app.modules.acquisition import ad_fairness_pacing

    last_runs = last_runs or {}
    candidate_query = select(TelegramAccount.id).where(
        TelegramAccount.is_active.is_(True),
        TelegramAccount.status.not_in(["banned", "restricted", "error"]),
        or_(TelegramAccount.spam_check_status.is_(None),
            TelegramAccount.spam_check_status.not_in(["restricted", "flagged"])),
    )
    eligible = sorted(int(account_id) for account_id in (await db.scalars(candidate_query)).all())
    if not eligible:
        return CanaryPick(None, "no_eligible_accounts", {"candidates": []})

    ads_last_scheduled = ad_fairness_pacing._ads_last_scheduled(last_runs)
    evaluation = await ad_fairness_pacing.evaluate(
        db, eligible, now, last_scheduled=ads_last_scheduled
    )
    by_account = {candidate.account_id: candidate for candidate in evaluation.candidates}

    excluded: dict[int, str] = {}
    for candidate in evaluation.gated:
        excluded[int(candidate.account_id)] = candidate.gate_reason or "gated"

    aged_ads = await _per_account_ad_unresolved_aged(db, eligible, now)
    for account_id, count in aged_ads.items():
        if count > 0:
            excluded.setdefault(account_id, "ad_unresolved")
    join_unknowns = await _per_account_join_unknown(db, eligible)
    for account_id, count in join_unknowns.items():
        if count > 0:
            excluded.setdefault(account_id, "join_outcome_unknown")
    backlog = await _per_account_reconciliation_backlog(db, eligible, now)
    for account_id, count in backlog.items():
        if count > 0:
            excluded.setdefault(account_id, "reconciliation_backlog")
    stale_leases = await _per_account_stale_leases(db, eligible, now)
    for account_id, count in stale_leases.items():
        if count > 0:
            excluded.setdefault(account_id, "lease_contention_evidence")
    recent_busy = _recent_lease_busy(last_runs, now)
    for account_id in recent_busy:
        excluded.setdefault(account_id, "lease_contention_recent")

    report_candidates = []
    healthy = []
    for account_id in eligible:
        candidate = by_account.get(account_id)
        entry = {
            "account_id": account_id,
            "excluded": excluded.get(account_id),
            "risk_component": round(float(candidate.risk_component), 6) if candidate else None,
            "read_recovery_factor": round(float(candidate.read_recovery_factor), 6) if candidate else None,
            "recent_success_rate": round(float(candidate.recent_success_rate), 6) if candidate else None,
            "last_scheduled_at": candidate.last_scheduled_at if candidate else None,
        }
        report_candidates.append(entry)
        if account_id not in excluded and candidate is not None:
            healthy.append(candidate)

    healthy.sort(key=canary_sort_key)
    winner = healthy[0].account_id if healthy else None
    report = {
        "candidates": report_candidates,
        "ordered_healthy": [candidate.account_id for candidate in healthy[:10]],
        "excluded_count": len(excluded),
    }
    return CanaryPick(
        winner,
        None if winner is not None else "no_healthy_canary_candidate",
        report,
    )


def _recent_lease_busy(last_runs: dict[str, Any], now: datetime) -> set[int]:
    """Accounts whose recent dispatch lost the account fence (contention)."""
    recent: set[int] = set()
    now_epoch = now.timestamp()
    for key, raw in (last_runs or {}).items():
        if not isinstance(key, str) or ":" not in key:
            continue
        try:
            entry = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("reason") != "account_operation_lease_busy":
            continue
        at = entry.get("at")
        if not isinstance(at, (int, float)):
            continue
        if now_epoch - float(at) > LEASE_BUSY_RECENT_SECONDS:
            continue
        try:
            recent.add(int(key.rsplit(":", 1)[1]))
        except (TypeError, ValueError, IndexError):
            continue
    return recent


async def _per_account_ad_unresolved_aged(
    db: Any, account_ids: list[int], now: datetime
) -> dict[int, int]:
    from app.core.metrics import SLO as _SLO
    from app.modules.acquisition.models import AdDeliveryLog

    if not account_ids:
        return {}
    cutoff = now - timedelta(seconds=int(_SLO["ad_unknown_warning_seconds"]))
    rows = (
        await db.execute(
            select(AdDeliveryLog.account_id, func.count()).where(
                AdDeliveryLog.account_id.in_(account_ids),
                AdDeliveryLog.status.in_(AD_UNRESOLVED_STATES),
                AdDeliveryLog.created_at < cutoff,
            ).group_by(AdDeliveryLog.account_id)
        )
    ).all()
    return {int(account_id): int(count) for account_id, count in rows}


async def _per_account_join_unknown(db: Any, account_ids: list[int]) -> dict[int, int]:
    from app.modules.acquisition.models import AutoJoinAttempt

    if not account_ids:
        return {}
    rows = (
        await db.execute(
            select(AutoJoinAttempt.account_id, func.count()).where(
                AutoJoinAttempt.account_id.in_(account_ids),
                AutoJoinAttempt.request_state == "outcome_unknown",
                AutoJoinAttempt.reconciliation_status.not_in(TERMINAL_RECONCILIATION_STATES),
            ).group_by(AutoJoinAttempt.account_id)
        )
    ).all()
    return {int(account_id): int(count) for account_id, count in rows}


async def _per_account_reconciliation_backlog(
    db: Any, account_ids: list[int], now: datetime
) -> dict[int, int]:
    from app.core.account.control_models import ReconciliationTask

    if not account_ids:
        return {}
    rows = (
        await db.execute(
            select(ReconciliationTask.account_id, func.count()).where(
                ReconciliationTask.account_id.in_(account_ids),
                ReconciliationTask.state.in_(["pending", "leased"]),
            ).group_by(ReconciliationTask.account_id)
        )
    ).all()
    return {
        int(account_id): int(count)
        for account_id, count in rows
        if account_id is not None
    }


async def _per_account_stale_leases(
    db: Any, account_ids: list[int], now: datetime
) -> dict[int, int]:
    """Expired outbox/reconciliation leases are contention or crash evidence."""
    from app.core.account.control_models import ReconciliationTask
    from app.modules.acquisition.p2_models import GrowthOutbox

    if not account_ids:
        return {}
    rows = (
        await db.execute(
            select(GrowthOutbox.account_id, func.count()).where(
                GrowthOutbox.account_id.in_(account_ids),
                GrowthOutbox.state == "leased",
                GrowthOutbox.lease_expires_at <= now,
            ).group_by(GrowthOutbox.account_id)
        )
    ).all()
    result = {int(account_id): int(count) for account_id, count in rows}
    task_rows = (
        await db.execute(
            select(ReconciliationTask.account_id, func.count()).where(
                ReconciliationTask.account_id.in_(account_ids),
                ReconciliationTask.state == "leased",
                ReconciliationTask.lease_expires_at <= now,
            ).group_by(ReconciliationTask.account_id)
        )
    ).all()
    for account_id, count in task_rows:
        if account_id is not None:
            result[int(account_id)] = result.get(int(account_id), 0) + int(count)
    return result


# ---------------------------------------------------------------------------
# Baseline and per-window metric gathering
# ---------------------------------------------------------------------------


def _p95(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return ordered[index]


async def _ad_health(db: Any, account_ids: list[int], now: datetime) -> dict[str, Any]:
    from app.modules.acquisition.models import AdDeliveryLog

    empty = {
        "ad_success_rate_24h": None,
        "ad_unresolved_aged": 0,
        "ad_unresolved_inflight": 0,
        "survival_backlog": 0,
        "survival_overdue": 0,
    }
    if not account_ids:
        return empty
    aged_cutoff = now - timedelta(seconds=int(SLO["ad_unknown_warning_seconds"]))
    window_start = now - timedelta(hours=24)
    log = AdDeliveryLog
    success, failed, aged, unresolved, survival_pending, survival_overdue = (
        await db.execute(
            select(
                func.count(log.id).filter(
                    log.status == "success",
                    log.telegram_message_id.is_not(None),
                    log.sent_at > window_start,
                ),
                func.count(log.id).filter(
                    log.status == "failed", log.created_at > window_start
                ),
                func.count(log.id).filter(
                    log.status.in_(AD_UNRESOLVED_STATES), log.created_at < aged_cutoff
                ),
                func.count(log.id).filter(log.status.in_(AD_UNRESOLVED_STATES)),
                func.count(log.id).filter(
                    log.status == "success",
                    log.telegram_message_id.is_not(None),
                    log.survival_status == "pending",
                ),
                func.count(log.id).filter(
                    log.status == "success",
                    log.telegram_message_id.is_not(None),
                    log.survival_status == "pending",
                    log.survival_check_due_at < now,
                ),
            ).where(log.account_id.in_(account_ids))
        )
    ).one()
    total = int(success or 0) + int(failed or 0)
    return {
        "ad_success_rate_24h": (int(success or 0) / total) if total else None,
        "ad_unresolved_aged": int(aged or 0),
        "ad_unresolved_inflight": int(unresolved or 0) - int(aged or 0),
        "survival_backlog": int(survival_pending or 0),
        "survival_overdue": int(survival_overdue or 0),
    }


async def _join_unknown(db: Any, account_ids: list[int]) -> int:
    from app.modules.acquisition.models import AutoJoinAttempt

    if not account_ids:
        return 0
    return int(
        await db.scalar(
            select(func.count()).select_from(AutoJoinAttempt).where(
                AutoJoinAttempt.account_id.in_(account_ids),
                AutoJoinAttempt.request_state == "outcome_unknown",
                AutoJoinAttempt.reconciliation_status.not_in(TERMINAL_RECONCILIATION_STATES),
            )
        )
        or 0
    )


async def _parent_group_invariant_failures(db: Any) -> int:
    from app.core.group.models import Group, GroupAccountMembership

    failures = await db.scalar(
        select(func.count(func.distinct(Group.id)))
        .join(GroupAccountMembership, GroupAccountMembership.group_id == Group.id)
        .where(
            GroupAccountMembership.status == "joined",
            GroupAccountMembership.left_at.is_(None),
            GroupAccountMembership.review_status == "approved",
            GroupAccountMembership.ad_status == "active",
            Group.status != "active",
        )
    )
    return int(failures or 0)


async def _reconciliation_health(db: Any, now: datetime) -> dict[str, Any]:
    """Oldest unfinished age plus the unexplained-aging count (global).

    A task ages unexplained when it stays pending/leased past its own
    escalation threshold plus a bounded grace for the in-flight escalator
    pass, and past the SLO critical age.  The default threshold is the P2
    reconciliation max age; the escalator turns such tasks into
    ``manual_required`` and never resends them.
    """
    from app.core.account.control_models import ReconciliationTask

    discovered = func.coalesce(
        ReconciliationTask.first_discovered_at, ReconciliationTask.created_at
    )
    rows = (
        await db.scalars(
            select(
                ReconciliationTask.age_threshold_seconds,
                discovered,
            ).where(ReconciliationTask.state.in_(["pending", "leased"]))
            .order_by(discovered.asc())
            .limit(500)
        )
    ).all()
    default_threshold = int(settings.P2_RECONCILIATION_MAX_AGE_SECONDS)
    grace = UNEXPLAINED_GRACE_WINDOWS * WINDOW_SECONDS
    critical = int(SLO["reconciliation_critical_age_seconds"])
    unexplained = 0
    oldest_age = 0.0
    for threshold, discovered_at in rows:
        if discovered_at is None:
            continue
        age = max(0.0, (now - discovered_at).total_seconds())
        oldest_age = max(oldest_age, age)
        effective = max(int(threshold or default_threshold) + grace, critical)
        if age > effective:
            unexplained += 1
    return {
        "reconciliation_oldest_age_seconds": oldest_age,
        "reconciliation_unexplained": unexplained,
    }


async def _p2_invariants(db: Any, since: datetime | None) -> dict[str, int]:
    """Illegal transitions, orphan outbox rows and duplicate active operations."""
    from app.modules.acquisition.p2_models import (
        GrowthOperation,
        GrowthOperationEvent,
        GrowthOutbox,
    )
    from app.modules.acquisition.p2_outbox import _TRANSITIONS, TERMINAL_OPERATION_STATES

    illegal = 0
    if since is not None:
        pairs = (
            await db.execute(
                select(
                    GrowthOperationEvent.from_state,
                    GrowthOperationEvent.to_state,
                    func.count(),
                ).where(
                    GrowthOperationEvent.event_type == "state_transition",
                    GrowthOperationEvent.created_at >= since,
                ).group_by(
                    GrowthOperationEvent.from_state, GrowthOperationEvent.to_state
                )
            )
        ).all()
        for from_state, to_state, count in pairs:
            if to_state not in _TRANSITIONS.get(from_state or "", set()):
                illegal += int(count or 0)

    orphan = int(
        await db.scalar(
            select(func.count()).select_from(GrowthOutbox)
            .join(GrowthOperation, GrowthOperation.id == GrowthOutbox.operation_id)
            .where(
                GrowthOutbox.state.in_(
                    ["pending", "leased", "retryable_failed", "reconciliation_required"]
                ),
                or_(
                    GrowthOperation.archived_at.is_not(None),
                    and_(
                        GrowthOperation.state.in_(TERMINAL_OPERATION_STATES),
                        GrowthOperation.reconciliation_required.is_(False),
                    ),
                ),
            )
        )
        or 0
    )

    duplicate_rows = (
        await db.execute(
            select(func.count()).select_from(
                select(
                    GrowthOperation.account_id,
                    GrowthOperation.target_key,
                ).where(
                    or_(
                        GrowthOperation.state.in_(
                            ["reserved", "sent", "receipt_pending", "confirmed", "survival_pending"]
                        ),
                        GrowthOperation.reconciliation_required.is_(True),
                    )
                ).group_by(
                    GrowthOperation.account_id, GrowthOperation.target_key
                ).having(func.count() > 1).subquery()
            )
        )
    ).scalar()
    return {
        "illegal_transitions": illegal,
        "orphan_outbox": orphan,
        "duplicate_active_operations": int(duplicate_rows or 0),
    }


async def _outbox_and_receipts(
    db: Any, account_ids: list[int], now: datetime
) -> dict[str, Any]:
    from app.modules.acquisition.p2_models import GrowthOperation, GrowthOutbox

    result: dict[str, Any] = {"outbox_oldest_age_seconds": 0.0, "receipt_p95_24h": None}
    if account_ids:
        oldest = await db.scalar(
            select(func.min(GrowthOutbox.available_at)).where(
                GrowthOutbox.kind == "send_ad",
                GrowthOutbox.state.in_(["pending", "retryable_failed"]),
                GrowthOutbox.account_id.in_(account_ids),
            )
        )
        if oldest is not None:
            result["outbox_oldest_age_seconds"] = max(0.0, (now - oldest).total_seconds())
        window_start = now - timedelta(hours=24)
        rows = (
            await db.scalars(
                select(GrowthOperation.sent_at, GrowthOperation.confirmed_at).where(
                    GrowthOperation.account_id.in_(account_ids),
                    GrowthOperation.sent_at.is_not(None),
                    GrowthOperation.confirmed_at.is_not(None),
                    GrowthOperation.confirmed_at > window_start,
                ).order_by(GrowthOperation.confirmed_at.desc()).limit(RECEIPT_SAMPLE_LIMIT)
            )
        ).all()
        latencies = [
            max(0.0, (confirmed - sent).total_seconds())
            for sent, confirmed in rows
            if sent is not None and confirmed is not None
        ]
        result["receipt_p95_24h"] = _p95(latencies)
    return result


async def _floodwait_and_budget(
    db: Any, account_ids: list[int], now: datetime
) -> dict[str, Any]:
    from app.core.account.control_models import AccountBudgetSnapshot
    from app.core.account.models import AccountRiskEvent

    window_start = now - timedelta(hours=24)
    floodwait = int(
        await db.scalar(
            select(func.count()).select_from(AccountRiskEvent).where(
                AccountRiskEvent.created_at > window_start,
                AccountRiskEvent.reason.ilike("%flood%"),
            )
        )
        or 0
    )
    latest = await db.scalar(select(func.max(AccountBudgetSnapshot.snapshot_at)))
    read_factor_avg = None
    if latest is not None and account_ids:
        avg = await db.scalar(
            select(func.avg(AccountBudgetSnapshot.read_factor)).where(
                AccountBudgetSnapshot.snapshot_at == latest,
                AccountBudgetSnapshot.lane == "ad",
                AccountBudgetSnapshot.window == "day",
                AccountBudgetSnapshot.account_id.in_(account_ids),
            )
        )
        if avg is not None:
            read_factor_avg = float(avg)
    return {"floodwait_events_24h": floodwait, "read_factor_avg": read_factor_avg}


async def _fair_share(db: Any, account_ids: list[int], now: datetime) -> dict[str, Any]:
    """Per-account 2h task share versus the equal fair share of the cohort."""
    from app.modules.acquisition.models import AdDeliveryLog, AutoJoinAttempt

    if not account_ids:
        return {"fair_share_max_ratio": None, "per_account_tasks_2h": {}}
    window_start = now - timedelta(hours=2)
    counts: dict[int, int] = {int(account_id): 0 for account_id in account_ids}
    rows = (
        await db.execute(
            select(AdDeliveryLog.account_id, func.count()).where(
                AdDeliveryLog.account_id.in_(account_ids),
                AdDeliveryLog.status == "success",
                AdDeliveryLog.telegram_message_id.is_not(None),
                AdDeliveryLog.sent_at > window_start,
            ).group_by(AdDeliveryLog.account_id)
        )
    ).all()
    for account_id, count in rows:
        counts[int(account_id)] = counts.get(int(account_id), 0) + int(count)
    join_rows = (
        await db.execute(
            select(AutoJoinAttempt.account_id, func.count()).where(
                AutoJoinAttempt.account_id.in_(account_ids),
                AutoJoinAttempt.request_sent_at > window_start,
            ).group_by(AutoJoinAttempt.account_id)
        )
    ).all()
    for account_id, count in join_rows:
        counts[int(account_id)] = counts.get(int(account_id), 0) + int(count)
    total = sum(counts.values())
    fair_share = total / len(account_ids) if account_ids else 0.0
    ratio = (max(counts.values()) / fair_share) if fair_share > 0 else None
    return {"fair_share_max_ratio": ratio, "per_account_tasks_2h": counts}


async def _cohort_bypass_count(
    db: Any, cohort_ids: list[int], since: datetime | None
) -> int:
    """Growth operations created outside the cohort since the release start."""
    from app.modules.acquisition.p2_models import GrowthOperation

    if since is None or not cohort_ids:
        return 0
    rows = (
        await db.execute(
            select(GrowthOperation.account_id, func.count()).where(
                or_(
                    GrowthOperation.created_at >= since,
                    GrowthOperation.reserved_at >= since,
                ),
                GrowthOperation.account_id.not_in(cohort_ids),
            ).group_by(GrowthOperation.account_id)
        )
    ).all()
    return len(rows)


async def _dispatch_wait_seconds(cache: Any) -> float:
    """The ads lane's oldest waiting dispatch time (starvation signal)."""
    try:
        from app.core.scheduler.growth_dispatch import snapshot

        state = await snapshot(cache)
        lanes = state.get("lanes") or {}
        return float(lanes.get("ads", {}).get("oldest_dispatch_wait_seconds") or 0)
    except Exception:
        return 0.0


async def _infra_ready(cache: Any) -> bool:
    """/health dependencies, Redis and a fresh metrics snapshot."""
    try:
        from app.core.runtime_health import dependencies

        await dependencies()
    except Exception:
        return False
    try:
        raw = await cache.get(METRICS_SNAPSHOT)
        data = json.loads(raw) if raw else {}
        if not data or time.time() - float(data.get("at") or 0) > SNAPSHOT_STALE_SECONDS:
            return False
    except Exception:
        return False
    return True


async def _metric_vector(
    db: Any,
    cache: Any,
    *,
    cohort_ids: list[int],
    since: datetime | None,
    now: datetime,
) -> dict[str, Any]:
    """One cohort-scoped health vector shared by baselines and samples."""
    vector: dict[str, Any] = {"at": now.timestamp(), "cohort_account_ids": sorted(cohort_ids)}
    vector.update(await _ad_health(db, cohort_ids, now))
    vector["join_unknown"] = await _join_unknown(db, cohort_ids)
    vector["parent_group_invariant_failures"] = await _parent_group_invariant_failures(db)
    vector.update(await _reconciliation_health(db, now))
    vector.update(await _p2_invariants(db, since))
    vector.update(await _outbox_and_receipts(db, cohort_ids, now))
    vector.update(await _floodwait_and_budget(db, cohort_ids, now))
    vector.update(await _fair_share(db, cohort_ids, now))
    vector["cohort_bypass_accounts"] = await _cohort_bypass_count(db, cohort_ids, since)
    vector["ads_oldest_dispatch_wait_seconds"] = await _dispatch_wait_seconds(cache)
    vector["session_lease_contention_total"] = await _counter_total(
        cache, "vanguard_session_lease_contention"
    )
    duplicate_totals = [
        await _counter_total(cache, "vanguard_p4_duplicate_targets_total", (kind,))
        for kind in ("join_group", "send_ad")
    ]
    vector["duplicate_targets_total"] = sum(duplicate_totals)
    return vector


async def compute_baseline(
    db: Any, cache: Any, cohort_ids: list[int], now: datetime
) -> dict[str, Any]:
    """Pre-release 24h aggregates captured once at release activation."""
    return await _metric_vector(db, cache, cohort_ids=cohort_ids, since=None, now=now)


async def gather_sample(
    db: Any,
    cache: Any,
    *,
    cohort_ids: list[int],
    since: datetime | None,
    now: datetime,
    infra_ready: bool | None = None,
) -> dict[str, Any]:
    """One observe-window sample, including the infra verdict."""
    sample = await _metric_vector(
        db, cache, cohort_ids=cohort_ids, since=since, now=now
    )
    if infra_ready is None:
        infra_ready = await _infra_ready(cache)
    sample["infra_ready"] = bool(infra_ready)
    return sample


# ---------------------------------------------------------------------------
# Release lifecycle
# ---------------------------------------------------------------------------


def _json_list(raw: str | None) -> list[Any]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _json_dict(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


async def _load_release(db: Any, release_id: str):
    from app.modules.acquisition.p5_models import P5ReleaseRecord

    if not release_id:
        return None
    return await db.scalar(
        select(P5ReleaseRecord).where(P5ReleaseRecord.release_id == release_id)
    )


async def _latest_active_release(db: Any):
    from app.modules.acquisition.p5_models import P5ReleaseRecord

    return await db.scalar(
        select(P5ReleaseRecord)
        .where(P5ReleaseRecord.status == "active")
        .order_by(P5ReleaseRecord.id.desc())
        .limit(1)
    )


async def freeze_release_candidate(
    db: Any,
    *,
    release_id: str,
    image_digest: str,
    migration_revision: str,
    now: datetime | None = None,
) -> Any:
    """Idempotently freeze one release candidate with its pinned evidence.

    An existing release id is immutable: freezing again with a different
    image digest or migration revision is rejected instead of rewritten, so
    a remediated rollout must ship under a new release id.
    """
    import structlog

    from app.modules.acquisition.p5_models import P5ReleaseRecord, snapshot_flags

    now = now or datetime.utcnow()
    release_id = (release_id or "").strip()
    if not release_id:
        raise ValueError("p5_release_id_required")
    existing = await db.scalar(
        select(P5ReleaseRecord).where(P5ReleaseRecord.release_id == release_id)
    )
    if existing is not None:
        if existing.image_digest != image_digest or existing.migration_revision != migration_revision:
            raise ValueError("p5_release_id_immutable")
        return existing
    record = P5ReleaseRecord(
        release_id=release_id,
        status="frozen",
        image_digest=image_digest,
        migration_revision=migration_revision,
        mode=settings.p5_rollout_mode,
        cohort_percent=int(settings.P5_COHORT_PERCENT),
        canary_account_ids_json=json.dumps(sorted(settings.p5_canary_accounts)),
        enabled_flags_json=json.dumps(snapshot_flags(settings), sort_keys=True),
        config_snapshot_json=json.dumps(snapshot_flags(settings), sort_keys=True),
    )
    db.add(record)
    await db.commit()
    structlog.get_logger().info(
        "p5_release_frozen",
        release_id=release_id,
        image_digest=image_digest,
        migration_revision=migration_revision,
    )
    return record


async def engage_stop(
    db: Any,
    cache: Any,
    release: Any,
    reasons: list[str],
    now: datetime,
    *,
    metrics_snapshot: dict[str, Any] | None = None,
    event: str = "gate_stop",
) -> None:
    """Engage the kill switch and record the rollback evidence.

    The stopped cohort keeps its ads/join admission blocked (both the P2 and
    the legacy path) until a new release id supersedes it.  Already-created
    outbox, attempt, receipt and reconciliation rows are never touched; the
    maintenance lanes keep resolving unconfirmed results.
    """
    import structlog

    cohort = _json_list(release.cohort_account_ids_json)
    stopped = {
        "release_id": release.release_id,
        "reasons": reasons,
        "stopped_at": now.isoformat(),
        "cohort_account_ids": [int(i) for i in cohort if isinstance(i, int)],
    }
    try:
        await cache.set(STOPPED_KEY, json.dumps(stopped, sort_keys=True))
    except Exception:
        pass
    if release.status != "rolled_back":
        release.status = "rolled_back"
    if release.rollback_at is None:
        release.rollback_at = now
        release.rollback_reason = "; ".join(reasons) or "unspecified"
        release.rollback_metrics_json = json.dumps(metrics_snapshot or {}, sort_keys=True)
        release.affected_cohort_json = json.dumps(sorted(stopped["cohort_account_ids"]))
        try:
            await record_p5_event("rollback")
        except Exception:
            pass
        structlog.get_logger().critical(
            "p5_rollout_stopped",
            release_id=release.release_id,
            reasons=reasons,
            cohort_size=len(stopped["cohort_account_ids"]),
        )
    if event == "gate_stop":
        try:
            await record_p5_event("gate_stop")
        except Exception:
            pass
    try:
        await db.commit()
    except Exception:
        pass


async def _engage_stop_without_record(
    db: Any, cache: Any, reasons: list[str], now: datetime
) -> None:
    """Fail closed when a governing deployment has no frozen release record."""
    import structlog

    cohort = await resolve_cohort(db, now)
    stopped = {
        "release_id": settings.P5_RELEASE_ID.strip(),
        "reasons": reasons,
        "stopped_at": now.isoformat(),
        "cohort_account_ids": sorted(cohort),
    }
    try:
        await cache.set(STOPPED_KEY, json.dumps(stopped, sort_keys=True))
    except Exception:
        pass
    structlog.get_logger().critical(
        "p5_rollout_fail_closed", reasons=reasons, cohort_size=len(cohort)
    )


def _stage_key() -> str:
    mode = settings.p5_rollout_mode
    if mode == "canary":
        return "canary"
    if mode == "full":
        return "full"
    return f"cohort_{int(settings.P5_COHORT_PERCENT)}"


async def observe_cycle(
    db: Any,
    cache: Any,
    *,
    now: datetime | None = None,
    infra_ready: bool | None = None,
) -> dict[str, Any]:
    """One observe cycle: activate, sample, gate, record, act.

    Runs every minute from the beat schedule.  In shadow mode the same
    selection, cohort preview and gate evaluation are recorded without ever
    activating the release or engaging the kill switch.
    """
    import structlog

    logger = structlog.get_logger()
    now = now or datetime.utcnow()
    mode = settings.p5_rollout_mode
    release_id = settings.P5_RELEASE_ID.strip()
    summary: dict[str, Any] = {"mode": mode, "release_id": release_id, "result": None}

    release = await _load_release(db, release_id)

    if mode == "off":
        # The operator kill switch: an active release whose deployment
        # returned to off is rolled back and its cohort stays blocked.
        active = release if release is not None and release.status == "active" else None
        if active is None:
            active = await _latest_active_release(db)
        if active is not None and active.rollback_at is None:
            await engage_stop(
                db, cache, active, ["p5_rollout_mode_off"], now, event="rollback"
            )
            summary["result"] = "stop"
            summary["rolled_back_release"] = active.release_id
        await _write_state(cache, {
            "mode": mode,
            "release_id": release_id,
            "status": (release.status if release is not None else None),
            "updated_at": now.isoformat(),
            "stopped": await read_stopped(cache) is not None,
        })
        return summary

    if release is None or release.status not in {"frozen", "active"}:
        if settings.p5_governs:
            await _engage_stop_without_record(
                db, cache, ["p5_release_record_missing"], now
            )
            summary["result"] = "stop"
        else:
            summary["result"] = "hold"
        logger.error(
            "p5_release_record_invalid", mode=mode, release_id=release_id,
            status=release.status if release is not None else None,
        )
        return summary

    if mode == "shadow":
        pick = await select_canary_account(db, now)
        preview = await resolve_cohort(db, now)
        summary.update({
            "result": "hold" if pick.account_id is None else "pass",
            "selected_canary": pick.account_id,
            "cohort_preview": preview,
        })
        await _write_state(cache, {
            "mode": mode,
            "release_id": release_id,
            "status": release.status,
            "selected_canary": pick.account_id,
            "cohort_preview": preview,
            "updated_at": now.isoformat(),
            "stopped": await read_stopped(cache) is not None,
        })
        return summary

    # ---- governing release: activate once ----
    if release.status == "frozen":
        release.status = "active"
        release.started_at = now
        release.mode = mode
        release.cohort_percent = int(settings.P5_COHORT_PERCENT)
        release.canary_account_ids_json = json.dumps(sorted(settings.p5_canary_accounts))
        try:
            await record_p5_event("release_started")
        except Exception:
            pass
        logger.info("p5_release_started", release_id=release_id, mode=mode)
    # A new release supersedes a previous stop; record the recovery time.
    stopped = await read_stopped(cache)
    if stopped and stopped.get("release_id") != release.release_id:
        await clear_stopped(cache)
        from app.modules.acquisition.p5_models import P5ReleaseRecord

        previous_stopped = await db.scalar(
            select(P5ReleaseRecord).where(
                P5ReleaseRecord.release_id == stopped.get("release_id")
            )
        )
        if previous_stopped is not None and previous_stopped.recovered_at is None:
            previous_stopped.recovered_at = now
        try:
            await record_p5_event("recovered")
        except Exception:
            pass
        logger.info(
            "p5_previous_stop_cleared", previous_release_id=stopped.get("release_id")
        )

    cohort_ids = await resolve_cohort(db, now)

    # Fail-closed canary: without a resolved account the cohort is empty and
    # the observer records who the auto-selection would pick.
    hold_extra: list[GateReason] = []
    if mode == "canary" and not settings.p5_canary_accounts:
        pick = await select_canary_account(db, now)
        release.selected_canary_account_id = pick.account_id
        release.selected_canary_report_json = json.dumps(pick.report, sort_keys=True)
        # Auto-selection is advisory.  An operator must resolve the chosen
        # account into P5_CANARY_ACCOUNT_IDS before any traffic is admitted.
        hold_extra.append(GateReason(
            "canary_unresolved", "hold",
            detail=pick.reason or f"selected_account={pick.account_id}",
        ))
        logger.warning(
            "p5_canary_unresolved", release_id=release_id,
            reason=pick.reason, selected_account=pick.account_id,
        )

    baseline = _json_dict(release.baseline_json)
    if not baseline:
        baseline = await compute_baseline(db, cache, cohort_ids, now)
        release.baseline_json = json.dumps(baseline, sort_keys=True)

    # A governing release requires its release-unit masters to stay on: P4
    # admin metrics must keep collecting and the P2 lanes that were frozen
    # must not silently disappear mid-rollout (deliberate lane closures are a
    # rollback action and land in the record as a hold, never silently).
    frozen_flags = _json_dict(release.enabled_flags_json)
    drift = [
        name
        for name in (
            "P2_OUTBOX_ENABLED",
            "P2_JOIN_OUTBOX_ENABLED",
            "P2_AD_OUTBOX_ENABLED",
            "P4_ADMIN_METRICS",
        )
        if frozen_flags.get(name) is True and getattr(settings, name, None) is False
    ]
    if drift:
        hold_extra.append(GateReason("release_flags_drift", "hold", detail=",".join(drift)))
        logger.warning("p5_release_flags_drift", release_id=release_id, flags=drift)

    stages = _json_list(release.stage_history_json)
    stage_key = _stage_key()
    stage_started_at = now
    if not stages or {int(i) for i in stages[-1].get("cohort_account_ids", [])} != set(cohort_ids):
        stages.append({
            "stage": stage_key,
            "cohort_account_ids": sorted(cohort_ids),
            "started_at": now.isoformat(),
            "percent": int(settings.P5_COHORT_PERCENT),
        })
        release.stage_history_json = json.dumps(stages)
        try:
            await record_p5_event("cohort_changed")
        except Exception:
            pass
        logger.info(
            "p5_cohort_stage",
            release_id=release_id,
            stage=stage_key,
            cohort_size=len(cohort_ids),
        )
    else:
        try:
            stage_started_at = datetime.fromisoformat(stages[-1]["started_at"])
        except (KeyError, TypeError, ValueError):
            stage_started_at = now
    release.cohort_account_ids_json = json.dumps(sorted(cohort_ids))

    await db.commit()

    sample = await gather_sample(
        db, cache, cohort_ids=cohort_ids, since=release.started_at, now=now,
        infra_ready=infra_ready,
    )
    await _push_window(cache, release.release_id, sample)
    samples = await _read_windows(cache, release.release_id)
    stage_age = max(0.0, (now - stage_started_at).total_seconds())
    stage_min = WARMUP_SECONDS if mode == "canary" else STAGE_MIN_OBSERVE_SECONDS

    gate = evaluate_gate(
        baseline=baseline,
        samples=samples,
        current=sample,
        stage_age_seconds=stage_age,
        stage_min_seconds=stage_min,
    )
    if hold_extra and gate.result != "stop":
        gate = GateResult(
            "hold" if gate.result == "pass" else gate.result,
            [*gate.reasons, *hold_extra],
        )

    from app.modules.acquisition.p5_models import P5GateEvaluation

    db.add(P5GateEvaluation(
        release_id=release.release_id,
        mode=mode,
        stage=stage_key,
        result=gate.result,
        reasons_json=json.dumps(gate.as_json()),
        metrics_json=json.dumps(sample, sort_keys=True, default=str),
        cohort_account_ids_json=json.dumps(sorted(cohort_ids)),
        window_started_at=now - timedelta(seconds=WINDOW_SECONDS),
        evaluated_at=now,
    ))
    release.gate_result = gate.result
    release.gate_reasons_json = json.dumps(gate.as_json())

    summary["result"] = gate.result
    summary["cohort_size"] = len(cohort_ids)
    summary["reasons"] = [reason.code for reason in gate.reasons]

    if gate.result == "stop":
        await engage_stop(
            db,
            cache,
            release,
            [reason.code for reason in gate.reasons if reason.severity == "stop"],
            now,
            metrics_snapshot=sample,
        )
    else:
        if gate.result == "hold":
            try:
                await record_p5_event("gate_hold")
            except Exception:
                pass
            logger.warning(
                "p5_gate_hold",
                release_id=release.release_id,
                reasons=[reason.code for reason in gate.reasons],
            )
        if (
            mode == "full"
            and gate.result == "pass"
            and stage_age >= FULL_STABLE_SECONDS
            and release.completed_at is None
        ):
            release.status = "completed"
            release.completed_at = now
            try:
                await record_p5_event("release_completed")
            except Exception:
                pass
            logger.info("p5_release_completed", release_id=release.release_id)
        await db.commit()

    await _write_state(cache, {
        "mode": mode,
        "release_id": release.release_id,
        "status": release.status,
        "stage": stage_key,
        "stage_started_at": stage_started_at.isoformat(),
        "cohort_account_ids": sorted(cohort_ids),
        "cohort_percent": int(settings.P5_COHORT_PERCENT),
        "gate_result": gate.result,
        "gate_reasons": gate.as_json(),
        "updated_at": now.isoformat(),
        "stopped": await read_stopped(cache) is not None,
    })
    return summary


async def status_payload(db: Any, cache: Any) -> dict[str, Any]:
    """Read-only rollout status for /api/operations/status and metrics."""
    release = await _load_release(db, settings.P5_RELEASE_ID.strip())
    if release is None:
        release = await _latest_active_release(db)
    if release is None:
        from app.modules.acquisition.p5_models import P5ReleaseRecord

        release = await db.scalar(
            select(P5ReleaseRecord).order_by(P5ReleaseRecord.id.desc()).limit(1)
        )
    stopped = await read_stopped(cache)
    stages = _json_list(release.stage_history_json) if release is not None else []
    stage_started_at = None
    if stages:
        try:
            stage_started_at = datetime.fromisoformat(stages[-1]["started_at"])
        except (KeyError, TypeError, ValueError):
            stage_started_at = None
    now = datetime.utcnow()
    return {
        "governs": settings.p5_governs,
        "mode": settings.p5_rollout_mode,
        "release_id": release.release_id if release is not None else None,
        "release_status": release.status if release is not None else None,
        "image_digest": release.image_digest if release is not None else None,
        "migration_revision": release.migration_revision if release is not None else None,
        "started_at": release.started_at.isoformat() if release is not None and release.started_at else None,
        "completed_at": release.completed_at.isoformat() if release is not None and release.completed_at else None,
        "rollback_at": release.rollback_at.isoformat() if release is not None and release.rollback_at else None,
        "rollback_reason": release.rollback_reason if release is not None else None,
        "cohort_percent": int(settings.P5_COHORT_PERCENT),
        "canary_account_ids": sorted(settings.p5_canary_accounts),
        "cohort_account_ids": _json_list(release.cohort_account_ids_json) if release is not None else [],
        "cohort_size": len(_json_list(release.cohort_account_ids_json)) if release is not None else 0,
        "selected_canary_account_id": release.selected_canary_account_id if release is not None else None,
        "gate_result": release.gate_result if release is not None else None,
        "gate_reasons": _json_list(release.gate_reasons_json) if release is not None else [],
        "stage": stages[-1].get("stage") if stages else None,
        "stage_age_seconds": max(0.0, (now - stage_started_at).total_seconds())
        if stage_started_at is not None
        else None,
        "history_apply": settings.P5_HISTORY_APPLY,
        "stopped": stopped is not None,
        "stopped_reasons": (stopped or {}).get("reasons"),
        "stopped_at": (stopped or {}).get("stopped_at"),
        "stopped_cohort_size": len((stopped or {}).get("cohort_account_ids") or []),
    }
