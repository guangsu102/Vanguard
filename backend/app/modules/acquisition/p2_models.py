"""Durable P2 growth operation, outbox and reconciliation models.

The existing acquisition tables remain the read model for legacy growth
workers. These tables provide the canonical, versioned operation record used
by the P2 enqueue/dispatch path. They deliberately contain no Telegram client
code; an account Actor is the only component allowed to perform the external
side effect.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    inspect,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class GrowthOperation(Base):
    """One idempotent join or advertisement operation."""

    __tablename__ = "growth_operation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_account.id", ondelete="RESTRICT"), nullable=False
    )
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("group.id", ondelete="SET NULL"), nullable=True
    )
    campaign_id: Mapped[int | None] = mapped_column(
        ForeignKey("ad_campaign.id", ondelete="SET NULL"), nullable=True
    )
    target_key: Mapped[str] = mapped_column(String(500), nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    snapshot_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("qualification_snapshot.snapshot_id", ondelete="RESTRICT"),
        nullable=False,
    )
    group_version: Mapped[str] = mapped_column(String(128), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(128), nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    budget_reservation_id: Mapped[str | None] = mapped_column(
        ForeignKey("account_budget_reservation.reservation_id", ondelete="RESTRICT"),
        nullable=True,
    )
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="reserved")
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reconciliation_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    provider_request_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    provider_receipt_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    survival_due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    # Permanent marker: audit children may be compacted, but the operation
    # identity must remain a duplicate-send tombstone forever.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Every ORM update includes the version observed by its transaction in the
    # WHERE clause. Services increment it explicitly for each audited change.
    __mapper_args__ = {"version_id_col": state_version, "version_id_generator": False}

    outbox = relationship("GrowthOutbox", back_populates="operation", uselist=False)
    attempts = relationship(
        "GrowthOperationAttempt", back_populates="operation", cascade="all, delete-orphan"
    )
    events = relationship(
        "GrowthOperationEvent", back_populates="operation", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("kind IN ('join_group','send_ad')", name="growth_operation_kind"),
        CheckConstraint(
            "state IN ('reserved','sent','receipt_pending','confirmed','survival_pending','resolved','failed','cancelled')",
            name="growth_operation_state",
        ),
        CheckConstraint("state_version >= 0", name="growth_operation_state_version"),
        CheckConstraint("attempt_count >= 0", name="growth_operation_attempt_count"),
        CheckConstraint(
            "state <> 'reserved' OR budget_reservation_id IS NOT NULL",
            name="growth_operation_reserved_budget",
        ),
        CheckConstraint(
            "NOT (state IN ('resolved','failed','cancelled') AND reconciliation_required)",
            name="growth_operation_terminal_reconciliation",
        ),
        CheckConstraint(
            "state NOT IN ('confirmed','survival_pending','resolved') "
            "OR provider_receipt_json IS NOT NULL",
            name="growth_operation_external_evidence",
        ),
        Index(
            "ix_growth_operation_ready",
            "kind",
            "state",
            "reconciliation_required",
            "updated_at",
        ),
        Index("ix_growth_operation_target", "kind", "account_id", "target_key", "state"),
        Index("ix_growth_operation_snapshot", "snapshot_id", "group_version", "rule_version"),
        Index(
            "uq_growth_operation_active_target",
            "account_id",
            "target_key",
            unique=True,
            postgresql_where=text(
                "state IN ('reserved','sent','receipt_pending','confirmed','survival_pending') "
                "OR reconciliation_required"
            ),
            sqlite_where=text(
                "state IN ('reserved','sent','receipt_pending','confirmed','survival_pending') "
                "OR reconciliation_required"
            ),
        ),
    )


@event.listens_for(GrowthOperation, "before_update")
def _reject_immutable_operation_fields(_mapper, _connection, target: GrowthOperation) -> None:
    """Keep the qualification snapshot and logical identity immutable.

    State transitions deliberately update only operational columns.  A direct
    mutation of the snapshot, identity or payload would make a previously
    reserved Telegram action point at different evidence, so fail it before
    SQLAlchemy emits an UPDATE.  The database migration repeats the important
    structural parts (foreign keys/checks); this hook covers the values that
    PostgreSQL cannot express with a normal CHECK constraint.
    """

    state = inspect(target)
    immutable = (
        "kind",
        "idempotency_key",
        "account_id",
        "group_id",
        "campaign_id",
        "target_key",
        "run_id",
        "snapshot_id",
        "group_version",
        "rule_version",
        "evidence_hash",
        "budget_reservation_id",
        "payload_json",
        "payload_hash",
    )
    if any(state.attrs[name].history.has_changes() for name in immutable):
        raise ValueError("p2_operation_identity_immutable")


class GrowthOutbox(Base):
    """One durable dispatch row per growth operation."""

    __tablename__ = "growth_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operation_id: Mapped[int] = mapped_column(
        ForeignKey("growth_operation.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_account.id", ondelete="RESTRICT"), nullable=False
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    payload_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    available_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )
    lease_owner: Mapped[str | None] = mapped_column(String(160), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fencing_token: Mapped[str | None] = mapped_column(String(160), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    provider_request_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    operation = relationship("GrowthOperation", back_populates="outbox")

    __table_args__ = (
        CheckConstraint("kind IN ('join_group','send_ad')", name="growth_outbox_kind"),
        CheckConstraint(
            "state IN ('pending','leased','retryable_failed','succeeded','reconciliation_required','dead','cancelled')",
            name="growth_outbox_state",
        ),
        CheckConstraint("attempts >= 0", name="growth_outbox_attempts"),
        CheckConstraint("payload_version >= 1", name="growth_outbox_payload_version"),
        CheckConstraint(
            "state <> 'leased' OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="growth_outbox_lease_fields",
        ),
        CheckConstraint(
            "state = 'leased' OR (lease_owner IS NULL AND lease_expires_at IS NULL AND fencing_token IS NULL)",
            name="growth_outbox_lease_only_processing",
        ),
        Index("ix_growth_outbox_ready", "state", "available_at", "priority", "id"),
        Index("ix_growth_outbox_lease", "state", "lease_expires_at"),
        Index("ix_growth_outbox_account", "account_id", "state", "available_at"),
    )


class GrowthOperationAttempt(Base):
    """Append-only external attempt and provider receipt record."""

    __tablename__ = "growth_operation_attempt"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operation_id: Mapped[int] = mapped_column(
        ForeignKey("growth_operation.id", ondelete="CASCADE"), nullable=False
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_generation: Mapped[str | None] = mapped_column(String(160), nullable=True)
    provider_request_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False, default="started")
    receipt_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("operation_id", "attempt_no", name="uq_growth_operation_attempt_no"),
        CheckConstraint("attempt_no > 0", name="growth_operation_attempt_positive"),
        Index("ix_growth_operation_attempt_operation", "operation_id", "attempt_no"),
    )

    operation = relationship("GrowthOperation", back_populates="attempts")


class GrowthOperationEvent(Base):
    """Append-only state transition audit record."""

    __tablename__ = "growth_operation_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operation_id: Mapped[int] = mapped_column(
        ForeignKey("growth_operation.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    from_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (Index("ix_growth_operation_event_operation", "operation_id", "created_at"),)

    operation = relationship("GrowthOperation", back_populates="events")
