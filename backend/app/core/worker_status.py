"""Telegram worker status models.

The admin backend is the configuration and state center; long-running Telegram
execution is reported here by dedicated workers.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import BigInteger, Boolean, UniqueConstraint, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class TelegramWorkerRole(str, Enum):
    GROWTH_USER = "growth_user_worker"
    GUARDIAN_BOT = "guardian_bot_worker"
    QQ_ONEBOT = "qq_onebot_worker"


class TelegramWorkerStatusValue(str, Enum):
    STARTING = "starting"
    ONLINE = "online"
    DEGRADED = "degraded"
    OFFLINE = "offline"
    ERROR = "error"


class TelegramWorkerStatus(Base):
    """Heartbeat and runtime status for Telegram execution workers."""

    __tablename__ = "telegram_worker_status"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    worker_id: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, comment="Worker instance id")
    role: Mapped[str] = mapped_column(String(50), nullable=False, comment="Worker role")
    account_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("telegram_account.id", ondelete="SET NULL"),
        nullable=True,
        comment="Promoter account id",
    )
    bot_profile_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("guardian_bot_profile.id", ondelete="SET NULL"),
        nullable=True,
        comment="Guardian bot profile id",
    )
    status: Mapped[str] = mapped_column(String(30), default=TelegramWorkerStatusValue.STARTING.value, nullable=False)
    last_heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    __table_args__ = (
        Index("idx_telegram_worker_status_role", "role"),
        Index("idx_telegram_worker_status_status", "status"),
    )


class TelegramEventInbox(Base):
    """Ordered SDK events durably handed off to business processing."""
    __tablename__ = "telegram_event_inbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("telegram_account.id", ondelete="CASCADE"), nullable=False)
    event_key: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    chat_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    external_attempted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    __table_args__ = (
        UniqueConstraint("account_id", "event_key", name="uq_telegram_event_inbox_identity"),
        Index("ix_telegram_event_inbox_pending", "state", "next_attempt_at", "id"),
        Index("ix_telegram_event_inbox_peer", "account_id", "chat_id", "id"),
    )
