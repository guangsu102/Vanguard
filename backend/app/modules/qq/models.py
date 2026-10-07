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
    http_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    websocket_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    access_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    join_api_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    join_api_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    automation_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    join_interval_seconds: Mapped[int] = mapped_column(Integer, default=600, nullable=False)
    send_interval_seconds: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    max_joins_per_day: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    max_sends_per_day: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    next_join_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_send_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
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
    membership_status: Mapped[str] = mapped_column(String(20), default="unknown", nullable=False)
    membership_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
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


class QQAdCampaign(Base):
    """QQ delivery settings; creatives are shared with Telegram."""

    __tablename__ = "qq_ad_campaign"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    auto_join_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    send_mode: Mapped[str] = mapped_column(String(20), default="interval", nullable=False)
    min_wait_after_join_minutes: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=1440, nullable=False)
    scheduled_times_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    timezone: Mapped[str] = mapped_column(String(60), default="Asia/Shanghai", nullable=False)
    max_sends_per_group_per_day: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    max_sends_per_account_per_day: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    start_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    end_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


class QQCampaignTarget(Base):
    __tablename__ = "qq_campaign_target"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("qq_ad_campaign.id", ondelete="CASCADE"))
    group_number: Mapped[str] = mapped_column(String(20), nullable=False)
    local_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    verify_message: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (
        UniqueConstraint("campaign_id", "group_number", name="uq_qq_campaign_target"),
    )


class QQAdBinding(Base):
    __tablename__ = "qq_ad_binding"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    connection_id: Mapped[int] = mapped_column(
        ForeignKey("qq_bot_connection.id", ondelete="CASCADE")
    )
    campaign_id: Mapped[int] = mapped_column(ForeignKey("qq_ad_campaign.id", ondelete="CASCADE"))
    creative_id: Mapped[int] = mapped_column(ForeignKey("ad_creative.id", ondelete="CASCADE"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)

    __table_args__ = (
        UniqueConstraint("connection_id", "campaign_id", "creative_id", name="uq_qq_ad_binding"),
    )


class QQJoinTask(Base):
    __tablename__ = "qq_join_task"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    connection_id: Mapped[int] = mapped_column(
        ForeignKey("qq_bot_connection.id", ondelete="CASCADE")
    )
    group_number: Mapped[str] = mapped_column(String(20), nullable=False)
    verify_message: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="queued", nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    joined_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("connection_id", "group_number", name="uq_qq_join_account_group"),
        Index("idx_qq_join_task_status", "status", "connection_id"),
    )


class QQAdSchedule(Base):
    __tablename__ = "qq_ad_schedule"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    connection_id: Mapped[int] = mapped_column(
        ForeignKey("qq_bot_connection.id", ondelete="CASCADE")
    )
    campaign_id: Mapped[int] = mapped_column(ForeignKey("qq_ad_campaign.id", ondelete="CASCADE"))
    group_number: Mapped[str] = mapped_column(String(20), nullable=False)
    next_due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="active", nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("connection_id", "campaign_id", "group_number", name="uq_qq_ad_schedule"),
        Index("idx_qq_ad_schedule_due", "status", "next_due_at"),
    )


class QQAutomationLog(Base):
    """Persist a unique operation before invoking an external write."""

    __tablename__ = "qq_automation_log"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    connection_id: Mapped[int] = mapped_column(
        ForeignKey("qq_bot_connection.id", ondelete="CASCADE")
    )
    campaign_id: Mapped[int | None] = mapped_column(
        ForeignKey("qq_ad_campaign.id", ondelete="SET NULL"), nullable=True
    )
    creative_id: Mapped[int | None] = mapped_column(
        ForeignKey("ad_creative.id", ondelete="SET NULL"), nullable=True
    )
    group_number: Mapped[str] = mapped_column(String(20), nullable=False)
    operation_type: Mapped[str] = mapped_column(String(20), nullable=False)
    operation_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(30), default="sending", nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_qq_automation_log_account_time", "connection_id", "created_at"),
        Index("idx_qq_automation_log_group_time", "group_number", "created_at"),
    )
