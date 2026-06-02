"""Telegram worker status models.

The admin backend is the configuration and state center; long-running Telegram
execution is reported here by dedicated workers.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class TelegramWorkerRole(str, Enum):
    GROWTH_USER = "growth_user_worker"
    GUARDIAN_BOT = "guardian_bot_worker"


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
