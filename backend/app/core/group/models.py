"""
Group Models

Database models for Telegram group management.
"""

from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class GroupLevel(str, Enum):
    """Group rating level."""
    A = "A"  # High value
    B = "B"  # Medium value
    C = "C"  # Low value
    UNRATED = "unrated"


class GroupLevelConfig(Base):
    """
    Configurable group level settings.

    Allows administrators to customize level thresholds and operation
    permissions through the web interface.
    """

    __tablename__ = "group_level_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    level: Mapped[GroupLevel] = mapped_column(
        SQLEnum(GroupLevel),
        unique=True,
        nullable=False,
        comment="等级"
    )

    # Level threshold (minimum score for this level)
    min_score: Mapped[float] = mapped_column(
        Numeric(5, 2),
        default=0,
        comment="最低评分阈值"
    )

    # Operation permissions
    can_send_ads: Mapped[bool] = mapped_column(Boolean, default=False, comment="可发广告")
    can_mention_users: Mapped[bool] = mapped_column(Boolean, default=False, comment="可@用户")
    can_share_links: Mapped[bool] = mapped_column(Boolean, default=False, comment="可发链接")
    can_initiate_private: Mapped[bool] = mapped_column(Boolean, default=False, comment="可主动私聊")

    # Rate limits
    daily_message_limit: Mapped[int] = mapped_column(Integer, default=0, comment="每日消息上限")
    message_interval: Mapped[int] = mapped_column(Integer, default=60, comment="消息间隔(秒)")
    private_message_interval: Mapped[int] = mapped_column(Integer, default=30, comment="私聊间隔(秒)")

    # Scoring weights
    rule_weight: Mapped[float] = mapped_column(Numeric(3, 2), default=0.30, comment="群规权重")
    admin_weight: Mapped[float] = mapped_column(Numeric(3, 2), default=0.25, comment="管理员权重")
    history_weight: Mapped[float] = mapped_column(Numeric(3, 2), default=0.20, comment="历史权重")
    convert_weight: Mapped[float] = mapped_column(Numeric(3, 2), default=0.15, comment="转化权重")
    activity_weight: Mapped[float] = mapped_column(Numeric(3, 2), default=0.10, comment="活跃度权重")

    # Auto adjustment thresholds
    auto_downgrade_kick_threshold: Mapped[int] = mapped_column(Integer, default=3, comment="自动降级-踢出次数阈值")
    auto_downgrade_warning_threshold: Mapped[int] = mapped_column(Integer, default=5, comment="自动降级-警告次数阈值")
    auto_downgrade_success_rate_threshold: Mapped[float] = mapped_column(Numeric(3, 2), default=0.50, comment="自动降级-成功率阈值")
    auto_upgrade_no_warning_days: Mapped[int] = mapped_column(Integer, default=30, comment="自动升级-无警告天数")
    auto_upgrade_high_success_days: Mapped[int] = mapped_column(Integer, default=14, comment="自动升级-高成功率天数")
    auto_upgrade_high_convert_days: Mapped[int] = mapped_column(Integer, default=14, comment="自动升级-高转化天数")

    # Metadata
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="等级描述")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False
    )

    __table_args__ = (
        Index("idx_level_config", "level"),
    )


class Group(Base):
    """Telegram group model."""
    
    __tablename__ = "group"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, comment="Telegram群组ID")
    title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, comment="群名称")
    username: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, comment="群用户名")
    member_count: Mapped[int] = mapped_column(Integer, default=0, comment="成员数")
    status: Mapped[str] = mapped_column(String(30), default="active", nullable=False, comment="群状态")
    discovery_source: Mapped[str] = mapped_column(String(50), default="manual", nullable=False, comment="发现来源")
    source_keyword: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, comment="来源关键词")
    last_message_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="最后发言时间")
    
    # Rating
    level: Mapped[GroupLevel] = mapped_column(
        SQLEnum(GroupLevel),
        default=GroupLevel.UNRATED,
        nullable=False,
        comment="等级"
    )
    level_score: Mapped[float] = mapped_column(
        Numeric(5, 2),
        default=0,
        comment="评分"
    )
    
    # Score dimensions
    rule_score: Mapped[int] = mapped_column(Integer, default=0, comment="群规管控分")
    admin_score: Mapped[int] = mapped_column(Integer, default=0, comment="管理员态度分")
    history_score: Mapped[int] = mapped_column(Integer, default=0, comment="历史表现分")
    convert_score: Mapped[int] = mapped_column(Integer, default=0, comment="转化效果分")
    activity_score: Mapped[int] = mapped_column(Integer, default=0, comment="活跃度分")
    
    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False
    )
    
    __table_args__ = (
        Index("idx_level", "level"),
        Index("idx_group_status", "status"),
        Index("idx_group_source_keyword", "source_keyword"),
    )

    account_memberships: Mapped[list["GroupAccountMembership"]] = relationship(
        "GroupAccountMembership",
        back_populates="group",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class GroupAccountMembership(Base):
    """Telegram account membership in a managed group."""

    __tablename__ = "group_account_membership"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("group.id", ondelete="CASCADE"),
        nullable=False,
        comment="群组表ID",
    )
    telegram_group_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="Telegram群组ID")
    account_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_account.id", ondelete="CASCADE"),
        nullable=False,
        comment="Telegram账号ID",
    )
    status: Mapped[str] = mapped_column(String(30), default="joined", nullable=False, comment="加群状态")
    join_method: Mapped[str] = mapped_column(String(50), default="manual", nullable=False, comment="加群方式")
    source_keyword: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, comment="来源关键词")
    joined_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=datetime.utcnow, nullable=True, comment="加入时间")
    left_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="离开时间")
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="最后检查时间")
    warmup_status: Mapped[str] = mapped_column(
        String(40),
        default="joined_pending_test",
        nullable=False,
        comment="广告预热状态: joined_pending_test/probe_scheduled/writable_verified/ad_eligible/blocked/ad_delivered",
    )
    probe_status: Mapped[str] = mapped_column(
        String(40),
        default="not_started",
        nullable=False,
        comment="软广探测状态: not_started/scheduled/success/failed/skipped",
    )
    probe_due_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="探测消息计划时间")
    last_probe_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="最近探测时间")
    ad_eligible_after: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="允许软广时间")
    last_probe_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="最近探测失败原因")
    ad_status: Mapped[str] = mapped_column(
        String(40),
        default="warming",
        nullable=False,
        comment="账号-群软广状态: warming/probing/active/paused/blocked",
    )
    account_group_daily_cap: Mapped[int] = mapped_column(Integer, default=50, nullable=False, comment="单账号单群每日软广上限")
    ad_pause_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="账号-群广告暂停截止时间")
    ad_failure_streak: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="账号-群连续广告失败次数")
    interaction_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="互动预热开始时间")
    interaction_sent_today: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="今日互动消息数")
    first_ad_allowed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="首次允许软广时间")
    last_ad_survived_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="最近广告存活时间")
    last_ad_deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="最近广告删除时间")
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="备注")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    group = relationship("Group", back_populates="account_memberships")
    account = relationship("TelegramAccount", lazy="joined")

    __table_args__ = (
        UniqueConstraint("group_id", "account_id", name="uq_group_account_membership_group_account"),
        Index("idx_group_membership_group", "group_id"),
        Index("idx_group_membership_account", "account_id"),
        Index("idx_group_membership_tg_group", "telegram_group_id"),
        Index("idx_group_membership_status", "status"),
        Index("idx_group_membership_warmup", "account_id", "warmup_status", "probe_status"),
        Index("idx_group_membership_ad_eligible", "account_id", "ad_eligible_after"),
        Index("idx_group_membership_ad_status", "account_id", "ad_status"),
    )
