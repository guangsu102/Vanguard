"""Database models for QQ group governance through OneBot providers."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class QQBotConnection(Base):
    __tablename__ = "qq_bot_connection"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    app_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="offline", nullable=False)
    bot_openid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_connected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


class QQManagedGroup(Base):
    __tablename__ = "qq_managed_group"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    connection_id: Mapped[int] = mapped_column(
        ForeignKey("qq_bot_connection.id", ondelete="CASCADE"),
        nullable=False,
    )
    group_openid: Mapped[str] = mapped_column(String(128), nullable=False)
    local_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="active", nullable=False)
    monitoring_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notifications_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    auto_recall_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    receive_all_messages_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    proactive_messages_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    bot_added_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    bot_removed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    connection = relationship("QQBotConnection", lazy="joined")

    __table_args__ = (
        UniqueConstraint("connection_id", "group_openid", name="uq_qq_group_connection_openid"),
        Index("idx_qq_managed_group_status", "status"),
        Index("idx_qq_managed_group_last_message", "last_message_at"),
    )


class QQGroupMessage(Base):
    __tablename__ = "qq_group_message"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("qq_managed_group.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider_message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    member_openid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    member_role: Mapped[str | None] = mapped_column(String(30), nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    attachments_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_at_bot: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    moderation_status: Mapped[str] = mapped_column(String(30), default="unreviewed", nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    recalled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    group = relationship("QQManagedGroup", lazy="joined")

    __table_args__ = (
        UniqueConstraint("group_id", "provider_message_id", name="uq_qq_message_group_provider"),
        Index("idx_qq_group_message_time", "group_id", "occurred_at"),
        Index("idx_qq_group_message_member", "member_openid"),
    )


class QQGroupEvent(Base):
    __tablename__ = "qq_group_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("qq_managed_group.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    member_openid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("idx_qq_group_event_group_time", "group_id", "occurred_at"),
        Index("idx_qq_group_event_type", "event_type"),
    )


class QQGroupCommand(Base):
    __tablename__ = "qq_group_command"

    id: Mapped[str] = mapped_column(
        String(32),
        primary_key=True,
        default=lambda: uuid.uuid4().hex,
    )
    group_id: Mapped[int] = mapped_column(
        ForeignKey("qq_managed_group.id", ondelete="CASCADE"),
        nullable=False,
    )
    command_type: Mapped[str] = mapped_column(String(40), nullable=False)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    group = relationship("QQManagedGroup", lazy="joined")

    __table_args__ = (
        Index("idx_qq_group_command_status", "status", "created_at"),
        Index("idx_qq_group_command_group", "group_id", "created_at"),
    )
