"""Durable P4 pacing decision, daily fairness aggregate and history governance models.

These tables store the P4 decision layer only.  They never become a Telegram
execution path: pacing reorders existing ``growth_dispatch`` tickets and history
governance only archives or deletes already-terminal rows.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class GrowthPacingDecision(Base):
    """One ads-lane dispatch decision with its full candidate evidence.

    ``candidates_json`` keeps the per-account gate reason, score components,
    fairness gap and last dispatch time.  High-cardinality ``target_key`` values
    stay out of this row (and out of Prometheus labels); group level detail
    remains in the ordinary structured logs and aggregate tables.
    """

    __tablename__ = "growth_pacing_decision"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    stage: Mapped[str] = mapped_column(String(24), nullable=False, default="ads")
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    window_started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    window_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    gated_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    scheduled_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    selected_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("telegram_account.id", ondelete="SET NULL"), nullable=True
    )
    applied: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    gate_reasons_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    candidates_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        CheckConstraint("candidate_count >= 0 AND gated_count >= 0 AND scheduled_count >= 0", name="pacing_decision_counts"),
        Index("ix_growth_pacing_decision_recent", "created_at"),
        Index("ix_growth_pacing_decision_stage", "stage", "created_at"),
    )


class GrowthPacingDailyAccountStat(Base):
    """Per-account daily fairness aggregate derived from pacing decisions.

    One row per (stat_date, account_id).  Derived queue statistics live here
    for the 370-day audit horizon while raw decision rows expire after the
    short retention window.
    """

    __tablename__ = "growth_pacing_daily_account_stat"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    stat_date: Mapped[str] = mapped_column(DateTime, nullable=False)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_account.id", ondelete="CASCADE"), nullable=False
    )
    decision_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    gated_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    scheduled_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    fair_share_avg: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    actual_share_avg: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    ad_success_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        UniqueConstraint("stat_date", "account_id", name="uq_pacing_daily_account_stat"),
        CheckConstraint(
            "candidate_count >= 0 AND gated_count >= 0 AND scheduled_count >= 0 "
            "AND decision_count >= 0 AND ad_success_count >= 0",
            name="pacing_daily_stat_counts",
        ),
        Index("ix_pacing_daily_account_stat_date", "stat_date"),
    )


class GrowthHistoryGovernanceBatch(Base):
    """One history governance pass: dry-run report, archive batch or cleanup.

    ``cutoffs_json`` stores the retention policy inputs (retention days and
    the derived cutoff timestamps) so each batch is independently auditable
    and replayable.  A failed batch can be retried safely: archiving is
    idempotent per operation because the archive row is keyed by the original
    operation ID.
    """

    __tablename__ = "growth_history_governance_batch"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    dry_run: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    cutoffs_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    candidates_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    archived_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deleted_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        CheckConstraint("mode IN ('dry_run','archive','cleanup')", name="history_batch_mode"),
        CheckConstraint(
            "status IN ('running','succeeded','failed')", name="history_batch_status"
        ),
        CheckConstraint("archived_count >= 0 AND deleted_count >= 0", name="history_batch_counts"),
        Index("ix_history_governance_batch_created", "created_at"),
        Index("ix_history_governance_batch_mode", "mode", "created_at"),
    )


class GrowthOperationArchive(Base):
    """Archived terminal growth operation with its full audit chain.

    The original operation ID, idempotency key, state version, provider
    receipt, evidence hash and the append-only attempt/event chain are
    preserved verbatim.  Archived rows never re-enter the P2 state machine and
    never trigger Telegram operations.
    """

    __tablename__ = "growth_operation_archive"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("growth_history_governance_batch.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    account_id: Mapped[int] = mapped_column(Integer, nullable=False)
    group_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    campaign_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_key: Mapped[str] = mapped_column(String(500), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(128), nullable=False)
    group_version: Mapped[str] = mapped_column(String(128), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(128), nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    budget_reservation_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    reconciliation_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    provider_request_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    provider_receipt_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    survival_due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    outbox_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    attempts_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    events_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    archived_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("operation_id", name="uq_growth_operation_archive_operation"),
        CheckConstraint("state IN ('resolved','failed','cancelled')", name="archive_terminal_state"),
        Index("ix_growth_operation_archive_archived_at", "archived_at"),
        Index("ix_growth_operation_archive_account", "account_id", "archived_at"),
    )


# Partial indexes used by the history governance cleanup queries.  They are
# declared on the existing P2 tables via the migration only; keeping the
# predicate text here documents the intended cleanup shapes.
HISTORY_CLEANUP_INDEXES = {
    "ix_growth_operation_terminal_resolved": text(
        "state IN ('resolved','failed','cancelled')"
    ),
    "ix_growth_outbox_terminal_completed": text(
        "state IN ('succeeded','dead','cancelled')"
    ),
}
