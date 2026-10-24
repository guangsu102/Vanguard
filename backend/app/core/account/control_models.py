"""Work-unit audit records and the durable reconciliation outbox.

Redis remains the live budget authority. These tables never authorize a send.
Account identities intentionally survive account deletion for audit retention.
"""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AccountBudgetReservation(Base):
    __tablename__ = "account_budget_reservation"

    reservation_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    account_id: Mapped[int] = mapped_column(Integer, nullable=False)
    workload: Mapped[str] = mapped_column(String(32), nullable=False)
    lane: Mapped[str] = mapped_column(String(16), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    reserved_reads: Mapped[int] = mapped_column(Integer, nullable=False)
    consumed_reads: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    state: Mapped[str] = mapped_column(String(16), default="reserved", nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime)
    reason: Mapped[str | None] = mapped_column(String(100))

    __table_args__ = (
        CheckConstraint(
            "reserved_reads >= 0 AND consumed_reads >= 0 AND consumed_reads <= reserved_reads",
            name="budget_reads",
        ),
        CheckConstraint(
            "state IN ('reserved','settled','released','expired')", name="budget_state"
        ),
        CheckConstraint(
            "lane IN ('ad','survival','critical','sync','routine','listener')", name="budget_lane"
        ),
        Index("ix_budget_reservation_expiry", "state", "lease_expires_at"),
        Index("ix_budget_reservation_account", "account_id", "created_at"),
    )


class AccountBudgetSnapshot(Base):
    __tablename__ = "account_budget_snapshot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(Integer, nullable=False)
    window: Mapped[str] = mapped_column(String(8), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    lane: Mapped[str] = mapped_column(String(16), nullable=False)
    limit: Mapped[int] = mapped_column(Integer, nullable=False)
    used: Mapped[int] = mapped_column(Integer, nullable=False)
    remaining: Mapped[int] = mapped_column(Integer, nullable=False)
    ttl_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    read_factor: Mapped[float] = mapped_column(Float, nullable=False)
    loan_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    snapshot_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "account_id", "window", "lane", "snapshot_at", name="uq_budget_snapshot_tick"
        ),
        CheckConstraint('"limit" >= 0 AND used >= 0 AND remaining >= 0', name="snapshot_reads"),
        Index("ix_budget_snapshot_time", "snapshot_at"),
    )


class ReconciliationTask(Base):
    __tablename__ = "reconciliation_task"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_key: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    source_id: Mapped[int] = mapped_column(Integer, nullable=False)
    account_id: Mapped[int] = mapped_column(Integer, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    state: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    available_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    lease_owner: Mapped[str | None] = mapped_column(String(120))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Republish periodically even when a previous XADD succeeded: eviction and
    # bounded trimming can remove both the stream and its consumer group.
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    # The start of the current verification cycle. A known sent request can
    # wait for an administrator between cycles without resetting created_at.
    verification_due_at: Mapped[datetime | None] = mapped_column(DateTime)
    # P2 growth reconciliation keeps an explicit evidence envelope and age
    # policy for operators instead of relying on application logs.
    reason: Mapped[str | None] = mapped_column(String(500))
    first_discovered_at: Mapped[datetime | None] = mapped_column(DateTime)
    age_threshold_seconds: Mapped[int | None] = mapped_column(Integer)
    evidence_json: Mapped[str | None] = mapped_column(Text)
    result_json: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("kind IN ('join','ad','inbox','growth')", name="reconciliation_kind"),
        CheckConstraint(
            "state IN ('pending','leased','succeeded','manual_required','cancelled')",
            name="reconciliation_state",
        ),
        CheckConstraint("attempts >= 0", name="reconciliation_attempts"),
        Index("ix_reconciliation_ready", "state", "available_at", "priority", "id"),
        Index("ix_reconciliation_lease", "state", "lease_expires_at"),
        Index("ix_reconciliation_account", "account_id", "kind", "source_id"),
    )
