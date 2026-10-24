"""Low-cardinality, cross-process business telemetry backed by atomic Redis writes.

Scrapes never query PostgreSQL. One elected aggregator refreshes gauges every
30 seconds; work counters and histograms are shared by all execution processes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from datetime import datetime, timedelta
from typing import Any

from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, HistogramMetricFamily
from sqlalchemy import case, func, or_, select

from app.core.config import settings

PREFIX = "vanguard:metrics:v1:"
COUNTERS = PREFIX + "counters"
SNAPSHOT = PREFIX + "snapshot"
LANES = {"ad", "survival", "critical", "sync", "routine", "listener", "background"}
PURPOSES = {"join", "ad", "survival", "review", "reconciliation", "sync", "listener", "other"}
STATUSES = {"sent", "success", "failed", "pending", "unknown", "released", "skipped"}
REASONS = {
    "none",
    "budget",
    "cooldown",
    "session",
    "permission",
    "unknown",
    "verification",
    "other",
}
BUCKETS = (1, 5, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, float("inf"))
SLO = {
    "join_admission_to_send_p95_seconds": 120,
    "ad_admission_to_send_p95_seconds": 900,
    "reconciliation_p95_seconds": 300,
    "reconciliation_critical_age_seconds": 1800,
    "ad_unknown_warning_seconds": 900,
    "ad_unknown_critical_seconds": 3600,
    "survival_overdue_limit": 0,
    "session_lease_contention_15m_limit": 0,
    "parent_group_invariant_failures_limit": 0,
    "inbox_oldest_warning_seconds": 120,
    "p4_receipt_latency_p95_seconds": 900,
    "p4_duplicate_target_rate_limit": 0.05,
    "p4_ad_outbox_oldest_seconds": 1800,
    "p4_history_cleanup_failure_limit": 0,
}

# P4 pacing/governance label alphabets. Account IDs are the only high-ish
# cardinality label and are bounded by the finite active-account set; groups
# and target keys never become metric labels.
P4_PACING_STAGES = {"ads"}
P4_PACING_REASONS = {
    "eligible",
    "selected",
    "no_candidates",
    "gated_account_state",
    "gated_risk_pause",
    "gated_budget_wait",
    "gated_survival_backlog",
    "gated_unresolved_backlog",
}
P4_DUPLICATE_KINDS = {"join_group", "send_ad"}
P4_HISTORY_MODES = {"dry_run", "archive", "cleanup"}
P4_HISTORY_OUTCOMES = {"succeeded", "failed"}
# P5 rollout lifecycle events; the observer is the only writer.
P5_EVENTS = {
    "release_started",
    "release_completed",
    "gate_hold",
    "gate_stop",
    "cohort_changed",
    "rollback",
    "recovered",
}

COUNTER_LABELS = {
    "vanguard_join_attempts": ("status", "reason_class"),
    "vanguard_ad_deliveries": ("status", "reason_class"),
    "vanguard_rpc_reads": ("lane", "purpose", "outcome"),
    "vanguard_session_lease_contention": (),
}
HISTOGRAMS = {
    "vanguard_join_admission_to_send_seconds",
    "vanguard_ad_admission_to_send_seconds",
    "vanguard_reconciliation_duration_seconds",
    "vanguard_sync_admission_to_read_seconds",
    "vanguard_sync_claim_wait_seconds",
    "vanguard_inbox_claim_wait_seconds",
    "vanguard_p4_receipt_latency_seconds",
}
HISTOGRAM_LABELS = {"vanguard_inbox_claim_wait_seconds": ("lane",)}
EXTRA_COUNTER_LABELS = {
    "vanguard_sync_borrowed_reads_total": ("window",),
    "vanguard_sync_protection_triggers_total": ("reason",),
    "vanguard_inbox_coalesced_total": ("lane",),
    "vanguard_inbox_starvation_total": ("lane",),
    "vanguard_qualification_checkpoint_events_total": ("event",),
    "vanguard_p4_pacing_decisions_total": ("stage", "reason"),
    "vanguard_p4_duplicate_targets_total": ("kind",),
    "vanguard_p4_history_batches_total": ("mode", "outcome"),
    "vanguard_p5_events_total": ("event",),
}
GAUGE_LABELS = {
    "vanguard_budget_remaining": ("lane", "window"),
    "vanguard_reconciliation_tasks": ("kind", "state"),
    "vanguard_reconciliation_oldest_age_seconds": ("kind",),
    "vanguard_queue_lag_seconds": ("queue",),
    "vanguard_parent_group_invariant_failures": (),
    "vanguard_survival_overdue": (),
    "vanguard_ad_unresolved_oldest_age_seconds": (),
    "vanguard_budget_ledger_enabled": (),
    "vanguard_reconciliation_shadow_mode": (),
    "vanguard_reconciliation_enabled": (),
    "vanguard_sync_target_percent": (),
    "vanguard_sync_actual_percent": (),
    "vanguard_sync_max_actual_percent": (),
    "vanguard_sync_wait_p95_seconds": (),
    "vanguard_sync_borrowed_reads": (),
    "vanguard_inbox_lane_pending": ("lane",),
    "vanguard_inbox_lane_oldest_age_seconds": ("lane",),
    "vanguard_inbox_coalesced": ("lane",),
    "vanguard_inbox_baseline_pending": (),
    "vanguard_inbox_baseline_oldest_age_seconds": (),
    "vanguard_inbox_claimed": ("lane",),
    "vanguard_p1_enabled": (),
    "vanguard_qualification_checkpoint_hit_ratio": (),
    "vanguard_p4_admin_metrics_enabled": (),
    "vanguard_p4_ad_pacing_enabled": (),
    "vanguard_p4_ad_pacing_shadow": (),
    "vanguard_p4_history_governance_enabled": (),
    "vanguard_p4_pacing_score": ("account_id",),
    "vanguard_p4_fair_share": ("account_id",),
    "vanguard_p4_actual_share": ("account_id",),
    "vanguard_p4_read_recovery_factor": ("account_id",),
    "vanguard_p4_account_risk_level": ("account_id",),
    "vanguard_p4_ad_outbox_pending": (),
    "vanguard_p4_ad_outbox_oldest_age_seconds": (),
    "vanguard_p4_survival_backlog": (),
    "vanguard_p4_ad_unresolved": (),
    "vanguard_p4_receipt_latency_p95_seconds": (),
    "vanguard_p4_reconciliation_overdue": (),
    "vanguard_p4_history_eligible_operations": (),
    "vanguard_p4_history_oldest_eligible_age_seconds": (),
    "vanguard_p5_release_active": (),
    "vanguard_p5_rollout_stopped": (),
    "vanguard_p5_cohort_size": (),
    "vanguard_p5_gate_result": (),
    "vanguard_p5_stage_age_seconds": (),
}

RECORD_LUA = """
if ARGV[1]~='' then
 if not redis.call('SET',KEYS[2],'1','NX','EX',604800) then return 0 end
end
for i=2,#ARGV,2 do redis.call('HINCRBYFLOAT',KEYS[1],ARGV[i],ARGV[i+1]) end
return 1
"""


def reason_class(value: str | None) -> str:
    value = str(value or "").lower()
    if not value:
        return "none"
    for pattern, category in (
        ("budget", "budget"),
        ("quota", "budget"),
        ("flood", "cooldown"),
        ("cooldown", "cooldown"),
        ("lease", "session"),
        ("session", "session"),
        ("permission", "permission"),
        ("forbidden", "permission"),
        ("unknown", "unknown"),
        ("verification", "verification"),
    ):
        if pattern in value:
            return category
    return "other"


def purpose_class(value: str) -> str:
    if "reconcil" in value:
        return "reconciliation"
    if "survival" in value:
        return "survival"
    if "qualification" in value or "review" in value:
        return "review"
    if "join" in value:
        return "join"
    if value == "ad_delivery":
        return "ad"
    if "listener" in value:
        return "listener"
    return "other"


def _field(name: str, labels: tuple[str, ...] = ()) -> str:
    return "|".join((name, *labels))


def _safe_counter_labels(name: str, labels: list[str]) -> bool:
    """Treat Redis as untrusted telemetry storage at exposition time too."""
    expected = {**COUNTER_LABELS, **EXTRA_COUNTER_LABELS}.get(name)
    if expected is None or len(labels) != len(expected):
        return False
    if name in {"vanguard_join_attempts", "vanguard_ad_deliveries"}:
        return labels[0] in STATUSES and labels[1] in REASONS
    if name == "vanguard_rpc_reads":
        return (
            labels[0] in LANES
            and labels[1] in PURPOSES
            and labels[2] in {"success", "error", "flood", "unknown"}
        )
    if name == "vanguard_sync_borrowed_reads_total":
        return labels[0] in {"hour", "day"}
    if name == "vanguard_sync_protection_triggers_total":
        return labels[0] in {"advertising", "survival", "critical_review", "session", "unknown"}
    if name in {"vanguard_inbox_coalesced_total", "vanguard_inbox_starvation_total"}:
        return labels[0] in {"listener_fact", "business"}
    if name == "vanguard_qualification_checkpoint_events_total":
        return labels[0] in {"hit", "miss", "write", "duplicate_read"}
    if name == "vanguard_p4_pacing_decisions_total":
        return labels[0] in P4_PACING_STAGES and labels[1] in P4_PACING_REASONS
    if name == "vanguard_p4_duplicate_targets_total":
        return labels[0] in P4_DUPLICATE_KINDS
    if name == "vanguard_p4_history_batches_total":
        return labels[0] in P4_HISTORY_MODES and labels[1] in P4_HISTORY_OUTCOMES
    if name == "vanguard_p5_events_total":
        return labels[0] in P5_EVENTS
    return False


def _safe_gauge_labels(name: str, labels: list[str]) -> bool:
    expected = GAUGE_LABELS.get(name)
    if expected is None or len(labels) != len(expected):
        return False
    if not labels:
        return True
    if name == "vanguard_budget_remaining":
        return labels[0] in LANES and labels[1] in {"hour", "day"}
    if name == "vanguard_reconciliation_tasks":
        return labels[0] in {"join", "ad", "inbox", "growth"} and labels[1] in {
            "pending", "leased", "succeeded", "manual_required", "cancelled"
        }
    if name == "vanguard_reconciliation_oldest_age_seconds":
        return labels[0] in {"join", "ad", "inbox", "growth"}
    if name == "vanguard_queue_lag_seconds":
        return labels[0] in {
            "reconciliation", "inbox", "growth_ads", "growth_join",
            "qualification", "growth_maintenance"
        }
    if name in {"vanguard_inbox_lane_pending", "vanguard_inbox_lane_oldest_age_seconds", "vanguard_inbox_coalesced", "vanguard_inbox_claimed"}:
        return labels[0] in {"listener_fact", "business"}
    if name in {
        "vanguard_p4_pacing_score",
        "vanguard_p4_fair_share",
        "vanguard_p4_actual_share",
        "vanguard_p4_read_recovery_factor",
        "vanguard_p4_account_risk_level",
    }:
        # Account IDs are a bounded, operator-owned set. Anything that is not
        # a plain positive integer (a group or target key) is dropped at
        # exposition time.
        return labels[0].isdigit() and int(labels[0]) > 0
    return False


async def _record(fields: dict[str, float], *, event_key: str = "", cache: Any = None) -> None:
    if not settings.GROWTH_METRICS_ENABLED:
        return
    try:
        if cache is None:
            from app.core.redis import get_redis

            cache = await get_redis()
        dedupe = PREFIX + "seen:" + hashlib.sha256(event_key.encode()).hexdigest()
        args: list[Any] = [event_key]
        for name, value in fields.items():
            args.extend((name, value))
        async with asyncio.timeout(1):
            await cache.eval(RECORD_LUA, 2, COUNTERS, dedupe, *args)
    except Exception:
        # Telemetry never changes the original RPC outcome or triggers a replay.
        pass


async def record_delivery(
    kind: str,
    status: str,
    *,
    reason: str | None = None,
    event_key: str,
    admitted_at: datetime | None = None,
    sent_at: datetime | None = None,
) -> None:
    if kind not in {"join", "ad"} or status not in STATUSES:
        raise ValueError("invalid_metric_label")
    name = "vanguard_join_attempts" if kind == "join" else "vanguard_ad_deliveries"
    fields = {_field(name, (status, reason_class(reason))): 1}
    if status == "sent" and admitted_at and sent_at:
        fields.update(
            histogram_fields(
                f"vanguard_{kind}_admission_to_send_seconds",
                max(0, (sent_at - admitted_at).total_seconds()),
            )
        )
    await _record(fields, event_key=event_key)


def histogram_fields(name: str, seconds: float, labels: tuple[str, ...] = ()) -> dict[str, float]:
    if name not in HISTOGRAMS or not math.isfinite(seconds) or seconds < 0:
        raise ValueError("invalid_metric_observation")
    if name == "vanguard_inbox_claim_wait_seconds":
        if len(labels) != 1 or labels[0] not in {"listener_fact", "business"}:
            raise ValueError("invalid_metric_label")
    elif labels:
        raise ValueError("invalid_metric_label")
    return {
        **{_field(name, (*labels, str(bound))): 1 for bound in BUCKETS if seconds <= bound},
        _field(name, (*labels, "sum")): seconds,
        _field(name, (*labels, "count")): 1,
    }


async def record_reconciliation(seconds: float, *, event_key: str) -> None:
    await _record(
        histogram_fields("vanguard_reconciliation_duration_seconds", seconds), event_key=event_key
    )


async def record_rpc(lane: str, purpose: str, outcome: str, count: int) -> None:
    if (
        lane not in LANES
        or purpose not in PURPOSES
        or outcome not in {"success", "error", "flood", "unknown"}
    ):
        raise ValueError("invalid_metric_label")
    if count:
        await _record({_field("vanguard_rpc_reads", (lane, purpose, outcome)): count})


async def record_session_contention() -> None:
    await _record({"vanguard_session_lease_contention": 1})


async def record_sync_observation(
    *, admission_to_read_seconds: float | None = None,
    claim_wait_seconds: float | None = None,
    borrowed_reads: int = 0,
    window: str = "hour",
    protection_reason: str | None = None,
) -> None:
    fields: dict[str, float] = {}
    if admission_to_read_seconds is not None:
        fields.update(histogram_fields("vanguard_sync_admission_to_read_seconds", admission_to_read_seconds))
    if claim_wait_seconds is not None:
        fields.update(histogram_fields("vanguard_sync_claim_wait_seconds", claim_wait_seconds))
    if borrowed_reads > 0 and window in {"hour", "day"}:
        fields[_field("vanguard_sync_borrowed_reads_total", (window,))] = borrowed_reads
    if protection_reason:
        fields[_field("vanguard_sync_protection_triggers_total", (protection_reason,))] = 1
    if fields:
        await _record(fields)


async def record_inbox_observation(*, lane: str, coalesced: int = 0, starvation: int = 0,
                                   claim_wait_seconds: float | None = None, event_key: str = "") -> None:
    if lane not in {"listener_fact", "business"}:
        raise ValueError("invalid_metric_label")
    fields: dict[str, float] = {}
    if coalesced:
        fields[_field("vanguard_inbox_coalesced_total", (lane,))] = coalesced
    if starvation:
        fields[_field("vanguard_inbox_starvation_total", (lane,))] = starvation
    if claim_wait_seconds is not None:
        fields.update(histogram_fields("vanguard_inbox_claim_wait_seconds", claim_wait_seconds, (lane,)))
    if fields:
        await _record(fields, event_key=event_key)


async def record_checkpoint(event: str) -> None:
    if event not in {"hit", "miss", "write", "duplicate_read"}:
        raise ValueError("invalid_metric_label")
    await _record({_field("vanguard_qualification_checkpoint_events_total", (event,)): 1})


async def record_pacing_decision(stage: str, reason: str, count: int = 1) -> None:
    """P4 pacing decision counter; labels are two small fixed alphabets."""
    from app.core.config import settings

    if not settings.P4_ADMIN_METRICS:
        return
    if stage not in P4_PACING_STAGES or reason not in P4_PACING_REASONS:
        raise ValueError("invalid_metric_label")
    if count:
        await _record({_field("vanguard_p4_pacing_decisions_total", (stage, reason)): count})


async def record_receipt_latency(seconds: float, *, sent_at: datetime | None = None,
                                 confirmed_at: datetime | None = None) -> None:
    """P4 provider-receipt latency from send to durable confirmation."""
    from app.core.config import settings

    if not settings.P4_ADMIN_METRICS:
        return
    if seconds is None and sent_at and confirmed_at:
        seconds = max(0.0, (confirmed_at - sent_at).total_seconds())
    if seconds is None:
        return
    await _record(
        histogram_fields("vanguard_p4_receipt_latency_seconds", float(seconds))
    )


async def record_duplicate_target(kind: str) -> None:
    """P4 duplicate-target counter: an active/unknown target blocked new work."""
    from app.core.config import settings

    if not settings.P4_ADMIN_METRICS:
        return
    if kind not in P4_DUPLICATE_KINDS:
        raise ValueError("invalid_metric_label")
    await _record({_field("vanguard_p4_duplicate_targets_total", (kind,)): 1})


async def record_history_batch(mode: str, outcome: str) -> None:
    """P4 history governance batch outcome counter."""
    from app.core.config import settings

    if not settings.P4_ADMIN_METRICS:
        return
    if mode not in P4_HISTORY_MODES or outcome not in P4_HISTORY_OUTCOMES:
        raise ValueError("invalid_metric_label")
    await _record({_field("vanguard_p4_history_batches_total", (mode, outcome)): 1})


async def record_p5_event(event: str) -> None:
    """P5 rollout lifecycle event counter (release/gate/cohort/rollback)."""
    if event not in P5_EVENTS:
        raise ValueError("invalid_metric_label")
    await _record({_field("vanguard_p5_events_total", (event,)): 1})


def age(now: datetime, value: datetime | None) -> float:
    return max(0, (now - value).total_seconds()) if value else 0


def inbox_baseline_at() -> datetime | None:
    """A fixed operator-supplied boundary, never a moving age threshold."""
    value = getattr(settings, "P1_INBOX_BASELINE_AT", None)
    if not value:
        return None
    if not isinstance(value, datetime):
        try:
            value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if value.tzinfo is not None:
        from datetime import UTC
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value


def histogram_quantile(counters: dict, name: str, quantile: float = 0.95) -> float | None:
    count = float(counters.get(_field(name, ("count",)), 0) or 0)
    if count <= 0:
        return None
    target, previous_count, previous_bound = count * quantile, 0.0, 0.0
    for bound in BUCKETS:
        cumulative = float(counters.get(_field(name, (str(bound),)), 0) or 0)
        if cumulative >= target:
            if math.isinf(bound):
                return previous_bound
            fraction = (target - previous_count) / max(1, cumulative - previous_count)
            return previous_bound + (bound - previous_bound) * fraction
        previous_count, previous_bound = cumulative, bound
    return None


async def aggregate(db: Any, cache: Any) -> dict:
    """Bounded group queries once per 30s, never once per scrape or per RPC."""
    from app.core.account.budget_ledger import SNAPSHOT_AT
    from app.core.account.control_models import AccountBudgetSnapshot, ReconciliationTask
    from app.core.account.reconciliation_stream import rollout_filter
    from app.core.worker_status import TelegramEventInbox
    from app.modules.acquisition.models import AdDeliveryLog

    now = datetime.utcnow()
    gauges: list[dict] = []

    def gauge(name: str, value: float, *labels: str) -> None:
        gauges.append({"name": name, "value": value, "labels": list(labels)})

    states = (
        await db.execute(
            select(ReconciliationTask.kind, ReconciliationTask.state, func.count()).group_by(
                ReconciliationTask.kind, ReconciliationTask.state
            )
        )
    ).all()
    counts = {(kind, state): count for kind, state, count in states}
    for kind in ("join", "ad", "inbox", "growth"):
        for state in ("pending", "leased", "succeeded", "manual_required", "cancelled"):
            gauge("vanguard_reconciliation_tasks", counts.get((kind, state), 0), kind, state)
    oldest = (
        await db.execute(
            select(ReconciliationTask.kind, func.min(func.coalesce(
                ReconciliationTask.verification_due_at, ReconciliationTask.created_at)))
            .where(
                ReconciliationTask.state.in_(["pending", "leased"]),
                rollout_filter(ReconciliationTask.account_id),
            )
            .group_by(ReconciliationTask.kind)
        )
    ).all()
    oldest_by_kind = {kind: age(now, at) for kind, at in oldest}
    for kind in ("join", "ad", "inbox", "growth"):
        gauge("vanguard_reconciliation_oldest_age_seconds", oldest_by_kind.get(kind, 0), kind)
    oldest_due = await db.scalar(
        select(func.min(ReconciliationTask.available_at)).where(
            ReconciliationTask.state.in_(["pending", "leased"]),
            rollout_filter(ReconciliationTask.account_id),
            ReconciliationTask.available_at <= now,
        )
    )
    gauge("vanguard_queue_lag_seconds", age(now, oldest_due), "reconciliation")
    baseline = inbox_baseline_at()
    ordinary = TelegramEventInbox.kind.notin_(["listener_fact", "deleted"])
    active_inbox = TelegramEventInbox.state.in_(["pending", "processing", "executing"])
    inbox = await db.scalar(
        select(func.min(TelegramEventInbox.received_at)).where(
            active_inbox, ordinary,
            TelegramEventInbox.received_at >= baseline if baseline else True,
        )
    )
    gauge("vanguard_queue_lag_seconds", age(now, inbox), "inbox")
    baseline_count, baseline_oldest = (0, None)
    if baseline:
        baseline_count, baseline_oldest = (await db.execute(select(
            func.count(), func.min(TelegramEventInbox.received_at),
        ).where(active_inbox, ordinary, TelegramEventInbox.received_at < baseline))).one()
    gauge("vanguard_inbox_baseline_pending", int(baseline_count or 0))
    gauge("vanguard_inbox_baseline_oldest_age_seconds", age(now, baseline_oldest))
    # Historical rows retain their original column defaults; infer their lane
    # from kind rather than rewriting history during the additive migration.
    inbox_lane = case((TelegramEventInbox.kind.in_(["listener_fact", "deleted"]), "listener_fact"), else_="business")
    inbox_status = []
    for lane in ("listener_fact", "business"):
        lane_oldest = await db.scalar(
            select(func.min(TelegramEventInbox.received_at)).where(
                inbox_lane == lane,
                TelegramEventInbox.state.in_(["pending", "processing", "executing"]),
            )
        )
        lane_pending = await db.scalar(
            select(func.count()).select_from(TelegramEventInbox).where(
                inbox_lane == lane,
                TelegramEventInbox.state.in_(["pending", "processing", "executing"]),
            )
        )
        lane_coalesced = await db.scalar(
            select(func.coalesce(func.sum(TelegramEventInbox.coalesced_count), 0)).where(
                inbox_lane == lane,
            )
        )
        gauge("vanguard_inbox_lane_pending", int(lane_pending or 0), lane)
        gauge("vanguard_inbox_lane_oldest_age_seconds", age(now, lane_oldest), lane)
        gauge("vanguard_inbox_coalesced", int(lane_coalesced or 0), lane)
        claimed = await db.scalar(select(func.count()).select_from(TelegramEventInbox).where(
            inbox_lane == lane, TelegramEventInbox.state.in_(["processing", "executing"]),
        ))
        gauge("vanguard_inbox_claimed", int(claimed or 0), lane)
        inbox_status.append({"lane": lane, "pending": int(lane_pending or 0),
                             "oldest_seconds": age(now, lane_oldest), "claimed": int(claimed or 0),
                             "coalesced": int(lane_coalesced or 0)})
    account_claims = (await db.execute(select(
        TelegramEventInbox.account_id, inbox_lane.label("lane"),
        func.sum(case((active_inbox, 1), else_=0)),
        func.min(case((active_inbox, TelegramEventInbox.received_at), else_=None)),
        func.max(TelegramEventInbox.started_at),
        func.sum(case((TelegramEventInbox.completed_at >= now - timedelta(minutes=15), 1), else_=0)),
    ).where(or_(active_inbox, TelegramEventInbox.completed_at >= now - timedelta(minutes=15)))
        .group_by(TelegramEventInbox.account_id, inbox_lane))).all()
    from app.core.scheduler.growth_dispatch import PREFIX as DISPATCH_PREFIX
    from app.core.scheduler.growth_dispatch import QUEUED_SECONDS, STAGES, ticket_key

    for queue in ("growth_ads", "growth_join", "qualification", "growth_maintenance"):
        stamps = []
        for stage, spec in STAGES.items():
            if spec.queue != queue:
                continue
            tickets = await cache.zrange(
                DISPATCH_PREFIX + stage + ":outstanding", 0, 15, withscores=True
            )
            for account_id, expires_at in tickets:
                value = await cache.get(ticket_key(stage, int(account_id)))
                if isinstance(value, str) and value.startswith("q:"):
                    stamps.append(expires_at - QUEUED_SECONDS)
        gauge(
            "vanguard_queue_lag_seconds", max(0, time.time() - min(stamps)) if stamps else 0, queue
        )
    unresolved = await db.scalar(
        select(func.min(AdDeliveryLog.created_at)).where(
            AdDeliveryLog.status.in_(["pending", "unknown", "sending", "reconciliation_required"])
        )
    )
    gauge("vanguard_ad_unresolved_oldest_age_seconds", age(now, unresolved))
    overdue = await db.scalar(
        select(func.count())
        .select_from(AdDeliveryLog)
        .where(
            AdDeliveryLog.status == "success",
            AdDeliveryLog.survival_status == "pending",
            AdDeliveryLog.survival_check_due_at < now,
        )
    )
    gauge("vanguard_survival_overdue", int(overdue or 0))
    # A joined, approved, active membership must promote its core parent group.
    from app.core.group.models import Group, GroupAccountMembership

    failures = await db.scalar(
        select(func.count(func.distinct(Group.id)))
        .join(
            GroupAccountMembership,
            GroupAccountMembership.group_id == Group.id,
        )
        .where(
            GroupAccountMembership.status == "joined",
            GroupAccountMembership.left_at.is_(None),
            GroupAccountMembership.review_status == "approved",
            GroupAccountMembership.ad_status == "active",
            Group.status != "active",
        )
    )
    gauge("vanguard_parent_group_invariant_failures", int(failures or 0))
    latest = await db.scalar(select(func.max(AccountBudgetSnapshot.snapshot_at)))
    totals = []
    if latest:
        totals = (
            await db.execute(
                select(
                    AccountBudgetSnapshot.lane,
                    AccountBudgetSnapshot.window,
                    func.sum(AccountBudgetSnapshot.remaining),
                )
                .where(
                    AccountBudgetSnapshot.snapshot_at == latest,
                )
                .group_by(AccountBudgetSnapshot.lane, AccountBudgetSnapshot.window)
            )
        ).all()
    for lane, window, remaining in totals:
        gauge("vanguard_budget_remaining", remaining, lane, window)
    sync_target = settings.P1_SYNC_TARGET_PERCENT if settings.P1_RUNTIME_RELIABILITY_ENABLED else 20
    gauge("vanguard_sync_target_percent", sync_target)
    # ``loan_json`` is written once per account/window by the budget snapshot
    # job.  Read the sync row only, so a loan is counted once rather than once
    # for every lane row in the same snapshot.  This keeps the gauge useful
    # even when no Telegram RPC is currently in flight.
    borrowed_by_window = {"hour": 0, "day": 0}
    if latest:
        loan_rows = (
            await db.execute(
                select(
                    AccountBudgetSnapshot.window,
                    AccountBudgetSnapshot.loan_json,
                ).where(
                    AccountBudgetSnapshot.snapshot_at == latest,
                    AccountBudgetSnapshot.lane == "sync",
                )
            )
        ).all()
        for window, raw in loan_rows:
            if window not in borrowed_by_window:
                continue
            try:
                payload = json.loads(raw or "{}")
                borrowed_by_window[window] += max(0, int(payload.get("sync_borrowed", 0) or 0))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
    gauge("vanguard_sync_borrowed_reads", borrowed_by_window["hour"])
    # The actual share is calculated from the same durable snapshot used for
    # budget_remaining.  It is a bounded aggregate, never an account label.
    sync_used = await db.scalar(select(func.sum(AccountBudgetSnapshot.used)).where(
        AccountBudgetSnapshot.lane == "sync", AccountBudgetSnapshot.window == "hour",
        AccountBudgetSnapshot.snapshot_at == latest,
    )) if latest else 0
    total_limit = await db.scalar(select(func.sum(AccountBudgetSnapshot.limit)).where(
        AccountBudgetSnapshot.lane.in_(["ad", "survival", "critical", "sync", "routine"]),
        AccountBudgetSnapshot.window == "hour",
        AccountBudgetSnapshot.snapshot_at == latest,
    )) if latest else 0
    # Five disjoint lane caps sum to the unchanged total. Listener control
    # reads are carved from survival and must not add a sixth allocation.
    actual_percent = (float(sync_used or 0) / float(total_limit) * 100) if total_limit else 0
    gauge("vanguard_sync_actual_percent", actual_percent)
    sync_accounts = []
    if latest:
        per_account = (await db.execute(select(
            AccountBudgetSnapshot.account_id,
            func.sum(case((AccountBudgetSnapshot.lane == "sync", AccountBudgetSnapshot.used), else_=0)),
            func.sum(AccountBudgetSnapshot.limit),
        ).where(
            AccountBudgetSnapshot.lane.in_(["ad", "survival", "critical", "sync", "routine"]),
            AccountBudgetSnapshot.window == "hour", AccountBudgetSnapshot.snapshot_at == latest,
        ).group_by(AccountBudgetSnapshot.account_id))).all()
        sync_accounts = [{"account_id": account_id,
                          "enabled": settings.p1_sync_enabled_for(account_id),
                          "actual_percent": float(used or 0) / float(limit or 1) * 100}
                         for account_id, used, limit in per_account]
    gauge("vanguard_sync_max_actual_percent", max((a["actual_percent"] for a in sync_accounts if a["enabled"]), default=0))
    # Checkpoint events are counters and therefore shared across workers.  A
    # ratio is computed from the current counter snapshot; if no event has
    # been recorded yet, expose 0 rather than fabricating a healthy ratio.
    checkpoint_hit_ratio = 0.0
    try:
        counter_values = await cache.hgetall(COUNTERS)
        hit = float(counter_values.get(_field("vanguard_qualification_checkpoint_events_total", ("hit",)), 0) or 0)
        miss = float(counter_values.get(_field("vanguard_qualification_checkpoint_events_total", ("miss",)), 0) or 0)
        if hit + miss > 0:
            checkpoint_hit_ratio = hit / (hit + miss)
    except (AttributeError, TypeError, ValueError):
        counter_values = {}
        pass
    wait_p95 = histogram_quantile(counter_values, "vanguard_sync_claim_wait_seconds")
    if wait_p95 is not None:
        gauge("vanguard_sync_wait_p95_seconds", wait_p95)
    gauge("vanguard_qualification_checkpoint_hit_ratio", checkpoint_hit_ratio)
    gauge("vanguard_budget_ledger_enabled", int(settings.BUDGET_LEDGER_ENABLED))
    gauge("vanguard_reconciliation_shadow_mode", int(settings.RECONCILIATION_SHADOW_MODE))
    gauge("vanguard_reconciliation_enabled", int(settings.RECONCILIATION_STREAM_ENABLED))
    gauge("vanguard_p1_enabled", int(settings.P1_RUNTIME_RELIABILITY_ENABLED))
    p4_section = {"enabled": settings.P4_ADMIN_METRICS}
    if settings.P4_ADMIN_METRICS:
        p4_section.update(await _aggregate_p4(db, cache, now, gauge))
    p5_section = await _aggregate_p5(db, cache, gauge)
    snapshot_at = await cache.get(SNAPSHOT_AT)
    result = {
        "at": time.time(),
        "gauges": gauges,
        "ledger_snapshot_at": float(snapshot_at) if snapshot_at else 0,
        "slo": SLO,
        "p4": p4_section,
        "p5": p5_section,
        "budget": {
            "enabled": settings.BUDGET_LEDGER_ENABLED,
            "snapshot_at": snapshot_at,
            "sync_borrowed_reads": borrowed_by_window,
            "remaining": [
                {"lane": lane, "window": window, "remaining": value}
                for lane, window, value in totals
            ],
        },
        "reconciliation": {
            "enabled": settings.RECONCILIATION_STREAM_ENABLED,
            "shadow_mode": settings.RECONCILIATION_SHADOW_MODE,
            "states": [{"kind": k, "state": s, "count": c} for k, s, c in states],
            "oldest_unfinished_seconds": oldest_by_kind,
        },
        "runtime_reliability": {
            "enabled": settings.P1_RUNTIME_RELIABILITY_ENABLED,
            "sync": {"target_percent": sync_target, "actual_percent": actual_percent,
                     "wait_p95_seconds": wait_p95, "borrowed_reads": borrowed_by_window,
                     "rollout_mode": settings.P1_SYNC_ROLLOUT_MODE,
                     "canary_account_ids": sorted(settings.p1_sync_canary_accounts),
                     "accounts": sync_accounts},
            "inbox": {"lanes": inbox_status,
                      "baseline_at": baseline.isoformat() if baseline else None,
                      "baseline_pending": int(baseline_count or 0),
                      "baseline_oldest_seconds": age(now, baseline_oldest),
                      "account_claims": [{"account_id": a, "lane": lane, "pending": int(count or 0),
                                           "oldest_seconds": age(now, oldest),
                                           "last_claimed_at": claimed.isoformat() if claimed else None,
                                           "completed_15m": int(completed or 0)}
                                          for a, lane, count, oldest, claimed, completed in account_claims]},
            "qualification": {"checkpoint_hit_ratio": checkpoint_hit_ratio},
        },
    }
    await cache.set(SNAPSHOT, json.dumps(result))
    return result


async def _aggregate_p4(db: Any, cache: Any, now: datetime, gauge: Any) -> dict[str, Any]:
    """Bounded P4 gauges: flags, per-account pacing evidence and queue health.

    Per-account values come from the latest persisted pacing decision, so the
    exposition never grows beyond the finite candidate set of one window and
    group/target keys stay out of labels entirely.
    """
    from sqlalchemy import func as sa_func

    from app.modules.acquisition.models import AdDeliveryLog
    from app.modules.acquisition.p2_models import GrowthOutbox
    from app.modules.acquisition.p4_models import GrowthPacingDecision

    gauge("vanguard_p4_admin_metrics_enabled", 1)
    gauge("vanguard_p4_ad_pacing_enabled", int(settings.P4_AD_PACING and settings.p4_pacing_active))
    gauge("vanguard_p4_ad_pacing_shadow", int(settings.P4_AD_PACING and settings.P4_AD_PACING_SHADOW))
    gauge("vanguard_p4_history_governance_enabled", int(settings.P4_HISTORY_GOVERNANCE))
    section: dict[str, Any] = {
        "ad_pacing_enabled": settings.P4_AD_PACING,
        "ad_pacing_shadow": settings.P4_AD_PACING_SHADOW,
        "canary_account_ids": sorted(settings.p4_pacing_canary_accounts),
        "history_governance_enabled": settings.P4_HISTORY_GOVERNANCE,
        "history_governance_dry_run": settings.P4_HISTORY_GOVERNANCE_DRY_RUN,
    }

    decision = await db.scalar(
        select(GrowthPacingDecision).order_by(GrowthPacingDecision.id.desc()).limit(1)
    )
    accounts_table: list[dict[str, Any]] = []
    if decision is not None:
        try:
            candidates = json.loads(decision.candidates_json or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            candidates = []
        risk_levels: dict[int, str] = {}
        if candidates:
            from app.core.account.models import TelegramAccount

            for account_id, risk_level in (
                await db.execute(
                    select(TelegramAccount.id, TelegramAccount.risk_level).where(
                        TelegramAccount.id.in_([c.get("account_id") for c in candidates if isinstance(c, dict)])
                    )
                )
            ).all():
                risk_levels[int(account_id)] = str(getattr(risk_level, "value", risk_level) or "")
        risk_order = ["normal", "watch", "limited", "frozen", "quarantined"]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            account_id = candidate.get("account_id")
            if not isinstance(account_id, int) or account_id <= 0:
                continue
            label = str(account_id)
            gauge("vanguard_p4_pacing_score", float(candidate.get("score") or 0), label)
            gauge("vanguard_p4_fair_share", float(candidate.get("fair_share") or 0), label)
            gauge("vanguard_p4_actual_share", float(candidate.get("actual_share") or 0), label)
            gauge(
                "vanguard_p4_read_recovery_factor",
                float(candidate.get("read_recovery_factor") or 0),
                label,
            )
            level = risk_levels.get(account_id, "normal")
            gauge("vanguard_p4_account_risk_level", float(risk_order.index(level) if level in risk_order else 0), label)
            accounts_table.append({**candidate, "risk_level": level})
        section["last_decision"] = {
            "id": decision.id,
            "stage": decision.stage,
            "mode": decision.mode,
            "applied": bool(decision.applied),
            "window_started_at": decision.window_started_at.isoformat(),
            "candidate_count": decision.candidate_count,
            "gated_count": decision.gated_count,
            "scheduled_count": decision.scheduled_count,
            "selected_account_id": decision.selected_account_id,
            "gate_reasons": json.loads(decision.gate_reasons_json or "{}"),
        }
        section["accounts"] = accounts_table

    outbox = GrowthOutbox
    pending_outbox, oldest_outbox = (
        await db.execute(
            select(
                sa_func.count(outbox.id),
                sa_func.min(outbox.available_at),
            ).where(
                outbox.kind == "send_ad",
                outbox.state.in_(["pending", "retryable_failed"]),
            )
        )
    ).one()
    gauge("vanguard_p4_ad_outbox_pending", int(pending_outbox or 0))
    gauge("vanguard_p4_ad_outbox_oldest_age_seconds", age(now, oldest_outbox))

    log = AdDeliveryLog
    backlog, unresolved = (
        await db.execute(
            select(
                sa_func.count(log.id).filter(
                    log.status == "success",
                    log.telegram_message_id.is_not(None),
                    log.survival_status == "pending",
                ),
                sa_func.count(log.id).filter(
                    log.status.in_(["pending", "unknown", "sending", "reconciliation_required"])
                ),
            )
        )
    ).one()
    gauge("vanguard_p4_survival_backlog", int(backlog or 0))
    gauge("vanguard_p4_ad_unresolved", int(unresolved or 0))

    from app.core.account.control_models import ReconciliationTask

    overdue_tasks = await db.scalar(
        select(sa_func.count()).select_from(ReconciliationTask).where(
            ReconciliationTask.state.in_(["pending", "leased"]),
            ReconciliationTask.available_at <= now,
        )
    )
    gauge("vanguard_p4_reconciliation_overdue", int(overdue_tasks or 0))

    try:
        counter_values = await cache.hgetall(COUNTERS)
    except (AttributeError, TypeError, ValueError):
        counter_values = {}
    receipt_p95 = histogram_quantile(counter_values, "vanguard_p4_receipt_latency_seconds")
    if receipt_p95 is not None:
        gauge("vanguard_p4_receipt_latency_p95_seconds", receipt_p95)
        section["receipt_latency_p95_seconds"] = receipt_p95

    from app.modules.acquisition.history_governance import candidate_summary

    try:
        summary = await candidate_summary(db, now)
    except Exception:
        summary = {}
    eligible = int(summary.get("operations") or 0)
    gauge("vanguard_p4_history_eligible_operations", eligible)
    gauge(
        "vanguard_p4_history_oldest_eligible_age_seconds",
        float(summary.get("oldest_eligible_age_seconds") or 0),
    )
    section["history"] = {
        "eligible_operations": eligible,
        "oldest_eligible_age_seconds": float(summary.get("oldest_eligible_age_seconds") or 0),
        "expired_pacing_decisions": int(summary.get("pacing_decisions") or 0),
        "expired_daily_stats": int(summary.get("daily_stats") or 0),
        "expired_archives": int(summary.get("archives") or 0),
    }
    section["ad_outbox"] = {
        "pending": int(pending_outbox or 0),
        "oldest_age_seconds": age(now, oldest_outbox),
    }
    section["survival_backlog"] = int(backlog or 0)
    section["ad_unresolved"] = int(unresolved or 0)
    section["reconciliation_overdue"] = int(overdue_tasks or 0)
    return section


async def _aggregate_p5(db: Any, cache: Any, gauge: Any) -> dict[str, Any]:
    """P5 rollout gauges from the latest release record and Redis state.

    Emitted on every aggregation pass, including when P5 is off, so the
    stopped gauge can never go stale while a kill switch is engaged.
    """
    try:
        from app.modules.acquisition.p5_rollout import status_payload

        payload = await status_payload(db, cache)
    except Exception:
        payload = {}
    gauge("vanguard_p5_release_active", int(payload.get("release_status") == "active" and bool(payload.get("governs"))))
    gauge("vanguard_p5_rollout_stopped", int(bool(payload.get("stopped"))))
    gauge("vanguard_p5_cohort_size", int(payload.get("cohort_size") or 0))
    gauge(
        "vanguard_p5_gate_result",
        {"pass": 0, "hold": 1, "stop": 2}.get(payload.get("gate_result"), -1),
    )
    gauge("vanguard_p5_stage_age_seconds", float(payload.get("stage_age_seconds") or 0))
    return payload


async def cached_status(cache: Any) -> dict:
    raw = await cache.get(SNAPSHOT)
    data = json.loads(raw) if raw else {}
    return {
        "slo": {"targets": SLO, "snapshot_age_seconds": time.time() - data["at"] if data else None},
        "budget": data.get(
            "budget", {"enabled": settings.BUDGET_LEDGER_ENABLED, "state": "unavailable"}
        ),
        "reconciliation": data.get("reconciliation", {"state": "unavailable"}),
        "runtime_reliability": data.get("runtime_reliability", {"state": "unavailable"}),
        "p4": data.get("p4", {"enabled": settings.P4_ADMIN_METRICS, "state": "unavailable"}),
        "p5": data.get(
            "p5",
            {"governs": settings.p5_governs, "mode": settings.p5_rollout_mode, "state": "unavailable"},
        ),
    }


async def render(cache: Any) -> bytes:
    counters = await cache.hgetall(COUNTERS)
    raw = await cache.get(SNAPSHOT)
    snapshot = json.loads(raw) if raw else {}

    class Collector:
        def collect(self):
            for name, labels in {**COUNTER_LABELS, **EXTRA_COUNTER_LABELS}.items():
                metric = CounterMetricFamily(name, name, labels=labels)
                if not labels:
                    metric.add_metric([], float(counters.get(name, 0)))
                for field, value in counters.items():
                    parts = field.split("|")
                    if parts[0] == name and _safe_counter_labels(name, parts[1:]) and labels:
                        metric.add_metric(parts[1:], float(value))
                yield metric
            for name in sorted(HISTOGRAMS):
                metric = HistogramMetricFamily(name, name, labels=HISTOGRAM_LABELS.get(name, ()))
                label_sets = [("listener_fact",), ("business",)] if name in HISTOGRAM_LABELS else [()]
                for values in label_sets:
                    metric.add_metric(
                        values, [
                        (
                            "+Inf" if math.isinf(bound) else str(bound),
                            float(counters.get(_field(name, (*values, str(bound))), 0)),
                        )
                        for bound in BUCKETS
                        ],
                        float(counters.get(_field(name, (*values, "sum")), 0)),
                    )
                yield metric
            for name, labels in GAUGE_LABELS.items():
                metric = GaugeMetricFamily(name, name, labels=labels)
                for item in snapshot.get("gauges", []):
                    if item["name"] == name and _safe_gauge_labels(name, item["labels"]):
                        metric.add_metric(item["labels"], item["value"])
                yield metric
            for name, stamp in (
                ("vanguard_metrics_snapshot_age_seconds", snapshot.get("at", 0)),
                (
                    "vanguard_budget_ledger_snapshot_age_seconds",
                    snapshot.get("ledger_snapshot_at", 0),
                ),
            ):
                metric = GaugeMetricFamily(name, name)
                metric.add_metric([], max(0, time.time() - stamp))
                yield metric

    registry = CollectorRegistry()
    registry.register(Collector())
    return generate_latest(registry)


async def aggregation_loop() -> None:
    import structlog

    from app.core.database import get_db_session
    from app.core.redis import get_redis

    while True:
        try:
            cache = await get_redis()
            if settings.GROWTH_METRICS_ENABLED and await cache.set(
                PREFIX + "aggregate_lock", "1", nx=True, ex=29
            ):
                async with asyncio.timeout(25):
                    async with get_db_session() as db:
                        await aggregate(db, cache)
        except Exception as exc:
            structlog.get_logger().warning(
                "metrics_aggregation_failed", error_type=type(exc).__name__
            )
        await asyncio.sleep(30)
