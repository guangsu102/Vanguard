"""Durable P5 release record and gate-evaluation audit models.

These tables are the P5 release control plane only.  They never become a
Telegram execution path: the rollout controller records releases, cohorts,
gate verdicts and rollbacks, while the kill switch itself is the Redis stopped
state read by ``growth_dispatch`` before any new ads/join ticket is admitted.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class P5ReleaseRecord(Base):
    """One immutable P5 release candidate and its full rollout lifecycle.

    ``release_id`` is operator-supplied and immutable: a remediated rollout
    ships under a new release id, which also clears the previous stopped
    cohort.  The record keeps the pinned image digest, migration revision and
    flag snapshot from the preflight freeze, the resolved cohort per stage,
    the pre-release 24h baseline used by the gates, and the rollback
    evidence (reason, metrics snapshot, affected cohort, recovery time).
    """

    __tablename__ = "p5_release_record"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    release_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="frozen")
    image_digest: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    migration_revision: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="off")
    cohort_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    canary_account_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    cohort_account_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    stage_history_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    enabled_flags_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    config_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    selected_canary_account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    selected_canary_report_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    baseline_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    gate_result: Mapped[str | None] = mapped_column(String(8), nullable=True)
    gate_reasons_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rollback_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rollback_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    rollback_metrics_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    affected_cohort_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    recovered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        UniqueConstraint("release_id", name="uq_p5_release_record_release_id"),
        CheckConstraint(
            "status IN ('frozen','active','completed','rolled_back')",
            name="p5_release_status",
        ),
        CheckConstraint("cohort_percent >= 0 AND cohort_percent <= 100", name="p5_release_percent"),
        Index("ix_p5_release_record_created", "created_at"),
    )


class P5GateEvaluation(Base):
    """One observe-window gate verdict for one release.

    Every observation cycle persists its result, reasons and the metric
    vector it judged, so the release audit trail answers what the gate saw
    without replaying history.  Rows are append-only; they intentionally
    carry ``release_id`` as plain text so the audit outlives any record-row
    retention.
    """

    __tablename__ = "p5_gate_evaluation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    release_id: Mapped[str] = mapped_column(String(128), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    stage: Mapped[str] = mapped_column(String(24), nullable=False, default="")
    result: Mapped[str] = mapped_column(String(8), nullable=False)
    reasons_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    metrics_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    cohort_account_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    window_started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        CheckConstraint("result IN ('pass','hold','stop')", name="p5_gate_result"),
        Index("ix_p5_gate_evaluation_release_time", "release_id", "evaluated_at"),
        Index("ix_p5_gate_evaluation_result_time", "result", "evaluated_at"),
    )


# Rollout flags captured at freeze time.  Secrets never enter the snapshot;
# only the release-relevant booleans and identifiers are recorded.
P5_SNAPSHOT_FLAGS: tuple[str, ...] = (
    "GROWTH_METRICS_ENABLED",
    "BUDGET_LEDGER_ENABLED",
    "RECONCILIATION_STREAM_ENABLED",
    "RECONCILIATION_SHADOW_MODE",
    "P1_RUNTIME_RELIABILITY_ENABLED",
    "P2_OUTBOX_ENABLED",
    "P2_JOIN_OUTBOX_ENABLED",
    "P2_AD_OUTBOX_ENABLED",
    "P2_SHADOW_MODE",
    "P2_ACCOUNT_IDS",
    "P3_ENTITY_CACHE",
    "P3_GROUP_FACT_CACHE",
    "P3_SURVIVAL_CACHE",
    "P3_QUALIFICATION_SNAPSHOT",
    "P4_AD_PACING",
    "P4_AD_PACING_SHADOW",
    "P4_AD_PACING_ACCOUNT_IDS",
    "P4_ADMIN_METRICS",
    "P4_HISTORY_GOVERNANCE",
    "P4_HISTORY_GOVERNANCE_DRY_RUN",
    "P5_ROLLOUT_MODE",
    "P5_CANARY_ACCOUNT_IDS",
    "P5_COHORT_PERCENT",
    "P5_HISTORY_APPLY",
    "P5_RELEASE_ID",
)


def snapshot_flags(settings: Any) -> dict[str, Any]:
    """Redacted flag snapshot for the release record (booleans and ids only)."""
    return {name: getattr(settings, name, None) for name in P5_SNAPSHOT_FLAGS}
