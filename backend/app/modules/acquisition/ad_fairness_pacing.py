"""Deterministic per-account ad fairness pacing for the growth ``ads`` lane.

P4 is a decision layer only: it reorders which account receives the next
``growth_dispatch`` ads ticket.  It never acquires a Telegram client, never
creates a new quota and never bypasses the account Actor / fencing-token
boundary — execution keeps all existing capacity, readiness, survival,
cooldown and age-fairness authority.

Hard gates run first (fail closed, reason recorded):

* account state (active, not banned/restricted/error, risk level normal/watch);
* no active risk pause;
* outbound ad-day budget satisfied when a fresh ledger snapshot exists;
* survival overdue backlog within the account threshold;
* unresolved ad sends within the account threshold.

Passing accounts are scored with fixed weights::

    score = 0.30 * budget_headroom
          + 0.15 * read_recovery_factor
          + 0.20 * (1 - risk_score)
          + 0.10 * cooldown_readiness
          + 0.15 * survival_health
          + 0.10 * recent_success_rate

Order is ``score`` descending, then the recent actual-task-share fairness gap,
then the last dispatch time, then ``account_id``.  Unused budgets keep
headroom high, so a lower-capacity account is not starved by higher-capacity
peers; the fairness gap breaks remaining ties in favour of under-served
accounts.  Every decision (candidates, gate reasons, scores, final selection,
scheduling window) is persisted once per dispatch window; high-cardinality
``target_key`` values stay in logs and aggregate tables, never in labels.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from app.core.config import settings

BUDGET_HEADROOM_WEIGHT = 0.30
READ_RECOVERY_WEIGHT = 0.15
RISK_WEIGHT = 0.20
COOLDOWN_READINESS_WEIGHT = 0.10
SURVIVAL_HEALTH_WEIGHT = 0.15
RECENT_SUCCESS_WEIGHT = 0.10

# Missing durable evidence keeps the component neutral instead of fabricating
# either a healthy or an unhealthy value.
NEUTRAL_COMPONENT = 0.5
SNAPSHOT_FRESH_SECONDS = 300
RECENT_WINDOW = timedelta(hours=24)

GATE_ACCOUNT_STATE = "gated_account_state"
GATE_RISK_PAUSE = "gated_risk_pause"
GATE_BUDGET_WAIT = "gated_budget_wait"
GATE_SURVIVAL_BACKLOG = "gated_survival_backlog"
GATE_UNRESOLVED_BACKLOG = "gated_unresolved_backlog"
GATE_REASONS = frozenset({
    GATE_ACCOUNT_STATE,
    GATE_RISK_PAUSE,
    GATE_BUDGET_WAIT,
    GATE_SURVIVAL_BACKLOG,
    GATE_UNRESOLVED_BACKLOG,
})


def _clamp01(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return min(1.0, max(0.0, value))


@dataclass(slots=True)
class PacingCandidate:
    """One account's gate result and score evidence for a dispatch window."""

    account_id: int
    gate_reason: str | None = None
    score: float = 0.0
    budget_headroom: float = NEUTRAL_COMPONENT
    budget_snapshot_fresh: bool = False
    read_recovery_factor: float = NEUTRAL_COMPONENT
    risk_component: float = 1.0
    cooldown_readiness: float = 1.0
    survival_health: float = 1.0
    recent_success_rate: float = 1.0
    fairness_gap: float = 0.0
    fair_share: float = 0.0
    actual_share: float = 0.0
    last_scheduled_at: float = 0.0
    rank: int = 0

    @property
    def gated(self) -> bool:
        return self.gate_reason is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "gate_reason": self.gate_reason,
            "score": round(self.score, 6),
            "budget_headroom": round(self.budget_headroom, 6),
            "budget_snapshot_fresh": self.budget_snapshot_fresh,
            "read_recovery_factor": round(self.read_recovery_factor, 6),
            "risk_component": round(self.risk_component, 6),
            "cooldown_readiness": round(self.cooldown_readiness, 6),
            "survival_health": round(self.survival_health, 6),
            "recent_success_rate": round(self.recent_success_rate, 6),
            "fairness_gap": round(self.fairness_gap, 6),
            "fair_share": round(self.fair_share, 6),
            "actual_share": round(self.actual_share, 6),
            "last_scheduled_at": self.last_scheduled_at,
            "rank": self.rank,
        }


@dataclass(slots=True)
class PacingEvaluation:
    """The full, auditable outcome of one pacing decision window."""

    stage: str
    candidate_count: int
    candidates: list[PacingCandidate] = field(default_factory=list)
    order: list[int] = field(default_factory=list)

    @property
    def passing(self) -> list[PacingCandidate]:
        return [candidate for candidate in self.candidates if not candidate.gated]

    @property
    def gated(self) -> list[PacingCandidate]:
        return [candidate for candidate in self.candidates if candidate.gated]

    def gate_reason_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for candidate in self.gated:
            counts[candidate.gate_reason] = counts.get(candidate.gate_reason, 0) + 1
        return counts


def score_candidate(candidate: PacingCandidate) -> float:
    """Fixed-weight score; identical inputs always produce identical output."""
    return round(
        BUDGET_HEADROOM_WEIGHT * candidate.budget_headroom
        + READ_RECOVERY_WEIGHT * candidate.read_recovery_factor
        + RISK_WEIGHT * candidate.risk_component
        + COOLDOWN_READINESS_WEIGHT * candidate.cooldown_readiness
        + SURVIVAL_HEALTH_WEIGHT * candidate.survival_health
        + RECENT_SUCCESS_WEIGHT * candidate.recent_success_rate,
        6,
    )


def sort_key(candidate: PacingCandidate) -> tuple:
    """Score, fairness gap, last dispatch time, then account id."""
    return (
        -candidate.score,
        -candidate.fairness_gap,
        candidate.last_scheduled_at,
        candidate.account_id,
    )


async def evaluate(
    db: Any,
    candidate_ids: list[int],
    now: datetime,
    *,
    last_scheduled: dict[int, float] | None = None,
) -> PacingEvaluation:
    """Compute one deterministic decision over the ads candidate accounts."""
    from app.core.account.models import TelegramAccount

    evaluation = PacingEvaluation(stage="ads", candidate_count=len(candidate_ids))
    if not candidate_ids:
        return evaluation
    last_scheduled = last_scheduled or {}
    ids = sorted({int(account_id) for account_id in candidate_ids})
    accounts = {
        account.id: account
        for account in (await db.scalars(
            select(TelegramAccount).where(TelegramAccount.id.in_(ids))
        )).all()
    }
    budget = await _ad_day_budget(db, ids, now)
    delivery = await _delivery_signals(db, ids, now)
    schedule = await _schedule_readiness(db, ids, now)

    survival_limit = max(1, settings.P4_SURVIVAL_BACKLOG_LIMIT)
    unresolved_limit = max(1, settings.P4_UNRESOLVED_BACKLOG_LIMIT)
    read_scale = max(0.01, settings.TELEGRAM_READ_BUDGET_PERCENT / 100.0)
    total_recent_success = sum(signal["success"] for signal in delivery.values())
    candidates: list[PacingCandidate] = []
    for account_id in ids:
        account = accounts.get(account_id)
        candidate = PacingCandidate(account_id=account_id)
        candidate.last_scheduled_at = float(last_scheduled.get(account_id, 0.0))
        if account is None:
            candidate.gate_reason = GATE_ACCOUNT_STATE
            candidates.append(candidate)
            continue
        risk_level = str(getattr(account.risk_level, "value", account.risk_level) or "")
        if (
            not account.is_active
            or account.status in ("banned", "restricted", "error")
            or risk_level not in ("normal", "watch")
        ):
            candidate.gate_reason = GATE_ACCOUNT_STATE
        elif account.risk_pause_until is not None and account.risk_pause_until > now:
            candidate.gate_reason = GATE_RISK_PAUSE
        else:
            snapshot = budget.get(account_id)
            candidate.budget_snapshot_fresh = (
                snapshot is not None
                and (now - snapshot["snapshot_at"]).total_seconds() <= SNAPSHOT_FRESH_SECONDS
            )
            if candidate.budget_snapshot_fresh and snapshot["remaining"] <= 0:
                candidate.gate_reason = GATE_BUDGET_WAIT
            else:
                signal = delivery.get(account_id, {
                    "success": 0, "failed": 0, "unresolved": 0,
                    "survival_overdue": 0, "survival_pending": 0,
                })
                if signal["survival_overdue"] > survival_limit:
                    candidate.gate_reason = GATE_SURVIVAL_BACKLOG
                elif signal["unresolved"] > unresolved_limit:
                    candidate.gate_reason = GATE_UNRESOLVED_BACKLOG
        if not candidate.gated:
            _fill_score_components(
                candidate, account, budget.get(account_id), delivery.get(account_id),
                schedule.get(account_id), survival_limit, read_scale,
            )
        candidates.append(candidate)

    for candidate in candidates:
        if candidate.gated:
            continue
        if total_recent_success > 0:
            success = delivery.get(candidate.account_id, {}).get("success", 0)
            candidate.actual_share = _clamp01(success / total_recent_success)
        candidate.fair_share = 1.0 / max(1, evaluation.candidate_count)
        candidate.fairness_gap = round(candidate.fair_share - candidate.actual_share, 6)
        candidate.score = score_candidate(candidate)

    ranked = sorted((c for c in candidates if not c.gated), key=sort_key)
    for position, candidate in enumerate(ranked, start=1):
        candidate.rank = position
    evaluation.candidates = candidates
    evaluation.order = [candidate.account_id for candidate in ranked]
    return evaluation


def _fill_score_components(
    candidate: PacingCandidate,
    account: Any,
    snapshot: dict[str, Any] | None,
    signal: dict[str, int] | None,
    readiness: dict[str, int] | None,
    survival_limit: int,
    read_scale: float,
) -> None:
    signal = signal or {}
    if snapshot is not None and candidate.budget_snapshot_fresh:
        limit = max(0, int(snapshot["limit"]))
        candidate.budget_headroom = (
            _clamp01(snapshot["remaining"] / limit) if limit > 0 else 0.0
        )
        candidate.read_recovery_factor = _clamp01(
            float(snapshot.get("read_factor", read_scale) or read_scale) / read_scale
        )
    candidate.risk_component = _clamp01(
        1.0 - max(0.0, min(100.0, float(account.risk_score or 0.0))) / 100.0
    )
    if readiness is not None and readiness["total"] > 0:
        candidate.cooldown_readiness = _clamp01(readiness["due"] / readiness["total"])
    overdue = int(signal.get("survival_overdue", 0))
    candidate.survival_health = _clamp01(1.0 - overdue / survival_limit)
    success = int(signal.get("success", 0))
    failed = int(signal.get("failed", 0))
    candidate.recent_success_rate = (
        _clamp01(success / (success + failed)) if success + failed else 1.0
    )


async def _ad_day_budget(db: Any, ids: list[int], now: datetime) -> dict[int, dict[str, Any]]:
    """Latest durable ad-day ledger snapshot per account (no Redis fan-out)."""
    from app.core.account.control_models import AccountBudgetSnapshot

    latest = await db.scalar(select(func.max(AccountBudgetSnapshot.snapshot_at)))
    if latest is None:
        return {}
    rows = (
        await db.execute(
            select(
                AccountBudgetSnapshot.account_id,
                AccountBudgetSnapshot.limit,
                AccountBudgetSnapshot.used,
                AccountBudgetSnapshot.remaining,
                AccountBudgetSnapshot.read_factor,
                AccountBudgetSnapshot.snapshot_at,
            ).where(
                AccountBudgetSnapshot.snapshot_at == latest,
                AccountBudgetSnapshot.lane == "ad",
                AccountBudgetSnapshot.window == "day",
                AccountBudgetSnapshot.account_id.in_(ids),
            )
        )
    ).all()
    return {
        int(row.account_id): {
            "limit": int(row.limit or 0),
            "used": int(row.used or 0),
            "remaining": int(row.remaining or 0),
            "read_factor": float(row.read_factor or 1.0),
            "snapshot_at": row.snapshot_at,
        }
        for row in rows
    }


async def _delivery_signals(db: Any, ids: list[int], now: datetime) -> dict[int, dict[str, int]]:
    """One grouped query for success rate, unresolved sends and survival backlog."""
    from app.modules.acquisition.models import AdDeliveryLog
    from app.modules.acquisition.survival_schedule import overdue_clause

    log = AdDeliveryLog
    window_start = now - RECENT_WINDOW
    capacity = await _capacity_settings(db)
    rows = (
        await db.execute(
            select(
                log.account_id,
                func.count(log.id).filter(
                    log.status == "success",
                    log.telegram_message_id.is_not(None),
                    log.sent_at > window_start,
                ).label("success"),
                func.count(log.id).filter(
                    log.status == "failed",
                    log.created_at > window_start,
                ).label("failed"),
                func.count(log.id).filter(
                    log.status.in_(["pending", "unknown", "sending", "reconciliation_required"])
                ).label("unresolved"),
                func.count(log.id).filter(
                    log.status == "success",
                    log.telegram_message_id.is_not(None),
                    log.survival_status == "pending",
                ).label("survival_pending"),
                func.count(log.id).filter(
                    log.status == "success",
                    log.telegram_message_id.is_not(None),
                    log.survival_status == "pending",
                    overdue_clause(now, capacity),
                ).label("survival_overdue"),
            ).where(log.account_id.in_(ids)).group_by(log.account_id)
        )
    ).all()
    return {
        int(row.account_id): {
            "success": int(row.success or 0),
            "failed": int(row.failed or 0),
            "unresolved": int(row.unresolved or 0),
            "survival_pending": int(row.survival_pending or 0),
            "survival_overdue": int(row.survival_overdue or 0),
        }
        for row in rows
    }


async def _capacity_settings(db: Any) -> dict[str, Any]:
    from app.core.automation_settings import get_ad_capacity_settings

    return await get_ad_capacity_settings(db)


async def _schedule_readiness(db: Any, ids: list[int], now: datetime) -> dict[int, dict[str, int]]:
    """Group cooldown readiness: due schedule tuples over all tuples."""
    from app.modules.acquisition.models import AdDeliveryScheduleState

    state = AdDeliveryScheduleState
    rows = (
        await db.execute(
            select(
                state.account_id,
                func.count(state.id).label("total"),
                func.count(state.id).filter(state.next_due_at <= now).label("due"),
            ).where(state.account_id.in_(ids)).group_by(state.account_id)
        )
    ).all()
    return {
        int(row.account_id): {"total": int(row.total or 0), "due": int(row.due or 0)}
        for row in rows
    }


def canary_order(
    evaluation: PacingEvaluation,
    legacy_order: list[int],
    canary: set[int],
) -> list[int]:
    """Merge paced canary accounts with the legacy order for everyone else.

    Canary accounts that pass the gates are placed by pacing rank first;
    non-canary accounts keep the existing due-time rotation untouched, and
    gated canary accounts are held back exactly as a full rollout would.
    """
    if not canary:
        return list(evaluation.order)
    passing = [c for c in evaluation.candidates if not c.gated and c.account_id in canary]
    passing.sort(key=lambda c: c.rank)
    paced = [candidate.account_id for candidate in passing]
    return paced + [int(i) for i in legacy_order if int(i) not in canary]


def decision_row(
    evaluation: PacingEvaluation,
    *,
    mode: str,
    applied: bool,
    window_started_at: datetime,
    window_seconds: int,
    selected_account_id: int | None,
    scheduled_count: int,
) -> dict[str, Any]:
    """Payload for one persisted decision record."""
    gate_reasons = evaluation.gate_reason_counts()
    return {
        "stage": evaluation.stage,
        "mode": mode,
        "window_started_at": window_started_at,
        "window_seconds": window_seconds,
        "candidate_count": evaluation.candidate_count,
        "gated_count": len(evaluation.gated),
        "scheduled_count": scheduled_count,
        "selected_account_id": selected_account_id,
        "applied": applied,
        "gate_reasons_json": json.dumps(gate_reasons, sort_keys=True),
        "candidates_json": json.dumps(
            [candidate.as_dict() for candidate in evaluation.candidates], sort_keys=True
        ),
    }


async def record_decision(db: Any, payload: dict[str, Any]) -> Any:
    """Persist one decision; telemetry must never break the dispatcher."""
    from app.modules.acquisition.p4_models import GrowthPacingDecision

    row = GrowthPacingDecision(**payload)
    db.add(row)
    await db.flush()
    return row


async def finalize_published_decision(
    db: Any,
    decision_id: int,
    published_account_ids: set[int],
) -> None:
    """Record the accounts whose broker publish actually succeeded.

    Pacing evaluation happens before broker capacity and Redis reservation are
    checked.  Keeping this write separate makes ``scheduled_count`` an actual
    dispatch result instead of a prediction.
    """
    from app.modules.acquisition.p4_models import GrowthPacingDecision

    row = await db.scalar(select(GrowthPacingDecision).where(
        GrowthPacingDecision.id == int(decision_id),
    ).with_for_update().execution_options(populate_existing=True))
    if row is None:
        return
    try:
        candidates = json.loads(row.candidates_json or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        candidates = []
    observed: set[int] = set()
    if isinstance(candidates, list):
        for candidate in candidates:
            if isinstance(candidate, dict):
                account_id = candidate.get("account_id")
                candidate["published"] = (
                    candidate.get("published") is True or account_id in published_account_ids
                )
                if candidate["published"] and isinstance(account_id, int):
                    observed.add(account_id)
        row.candidates_json = json.dumps(candidates, sort_keys=True)
    row.scheduled_count = len(observed)
    await db.commit()


async def record_metrics(evaluation: PacingEvaluation, *, selected_account_id: int | None) -> None:
    """Low-cardinality Prometheus counters for one decision window."""
    if not settings.P4_ADMIN_METRICS:
        return
    from app.core import metrics

    if not evaluation.candidates:
        await metrics.record_pacing_decision(evaluation.stage, "no_candidates", 1)
        return
    for candidate in evaluation.gated:
        await metrics.record_pacing_decision(evaluation.stage, candidate.gate_reason, 1)
    for candidate in evaluation.passing:
        await metrics.record_pacing_decision(
            evaluation.stage,
            "selected" if candidate.account_id == selected_account_id else "eligible",
            1,
        )


async def apply_to_dispatch(
    db: Any,
    legacy_order: list[int],
    now_epoch: float,
    *,
    window_seconds: int,
    last_runs: dict[str, Any] | None = None,
) -> list[int]:
    """Reorder one ads dispatch window and persist the decision.

    Returns the final account order (identical to ``legacy_order`` in shadow
    mode or when every candidate is gated out of the canary).  The dispatcher
    treats this as advisory: execution revalidates every authority as before.
    """
    import structlog

    from app.core.config import settings

    logger = structlog.get_logger()
    db.info.pop("p4_last_decision_id", None)
    if not settings.P4_AD_PACING or not legacy_order:
        return legacy_order
    now_dt = datetime.utcfromtimestamp(now_epoch)
    last_scheduled = _ads_last_scheduled(last_runs or {})
    evaluation = await evaluate(db, legacy_order, now_dt, last_scheduled=last_scheduled)
    active = settings.p4_pacing_active
    canary = settings.p4_pacing_canary_accounts
    paced = canary_order(evaluation, legacy_order, canary)
    if active:
        order = paced
        selected_account_id = order[0] if order else None
    else:
        # Shadow mode keeps the live dispatch untouched and records the
        # selection pacing would have made for comparison.
        order = legacy_order
        selected_account_id = evaluation.order[0] if evaluation.order else None
    payload = decision_row(
        evaluation,
        mode="pacing" if active else "shadow",
        applied=active,
        window_started_at=now_dt,
        window_seconds=window_seconds,
        selected_account_id=selected_account_id,
        scheduled_count=0,
    )
    try:
        decision = await record_decision(db, payload)
        await db.commit()
        # The scheduler uses this session-local handoff after broker publish.
        # It avoids changing the public list-returning API used by tests and
        # by older callers.
        db.info["p4_last_decision_id"] = int(decision.id)
    except Exception:
        # An audit-row failure never blocks the dispatch it describes.
        logger.warning("p4_pacing_decision_record_failed", error_type="commit")
        await db.rollback()
    try:
        await record_metrics(evaluation, selected_account_id=selected_account_id)
    except Exception:
        logger.warning("p4_pacing_metrics_failed", error_type="record")
    logger.info(
        "p4_pacing_decision",
        stage=evaluation.stage,
        mode=payload["mode"],
        candidates=evaluation.candidate_count,
        gated=payload["gated_count"],
        selected=selected_account_id,
    )
    return order


def _ads_last_scheduled(last_runs: dict[str, Any]) -> dict[int, float]:
    """Parse the dispatcher's ``last_runs`` hash into ads timestamps."""
    result: dict[int, float] = {}
    for key, raw in last_runs.items():
        if not isinstance(key, str) or not key.startswith("ads:"):
            continue
        try:
            account_id = int(key.split(":", 1)[1])
        except (TypeError, ValueError):
            continue
        try:
            entry = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(entry, dict) and isinstance(entry.get("at"), (int, float)):
            result[account_id] = float(entry["at"])
    return result
