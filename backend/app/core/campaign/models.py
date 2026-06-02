"""
Campaign Models

Database models for marketing campaign tracking and management.
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
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class CampaignType(str, Enum):
    """Campaign type enumeration."""
    TRIAL = "trial"           # 试用
    PROMO = "promo"           # 促销活动
    DISCOUNT = "discount"     # 折扣
    GIFT_CARD = "gift_card"  # 礼品卡


class CampaignScope(str, Enum):
    """Campaign scope."""
    GLOBAL = "global"
    MANAGED_GROUP = "managed_group"


class CampaignTriggerTiming(str, Enum):
    """Campaign trigger timing enumeration."""

    AFTER_REGISTER = "after_register"
    IMMEDIATE = "immediate"
    DELAYED = "delayed"
    SCHEDULED = "scheduled"
    MANUAL = "manual"
    PERIODIC = "periodic"


class CampaignDistributionMode(str, Enum):
    """Campaign reward or broadcast distribution mode."""

    WELCOME = "welcome"
    DELAYED = "delayed"
    SCHEDULED = "scheduled"
    MANUAL = "manual"
    PERIODIC = "periodic"


def _enum_values(enum_cls: type[Enum]) -> list[str]:
    return [item.value for item in enum_cls]


class Campaign(Base):
    """Marketing campaign configuration model."""
    
    __tablename__ = "campaign"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, comment="活动名称")
    campaign_type: Mapped[CampaignType] = mapped_column(
        SQLEnum(CampaignType),
        nullable=False,
        comment="活动类型"
    )
    campaign_scope: Mapped[CampaignScope] = mapped_column(
        SQLEnum(CampaignScope),
        default=CampaignScope.GLOBAL,
        nullable=False,
        comment="活动范围",
    )
    
    # Trigger settings
    trigger_timing: Mapped[CampaignTriggerTiming] = mapped_column(
        SQLEnum(CampaignTriggerTiming, values_callable=_enum_values, native_enum=False),
        default=CampaignTriggerTiming.AFTER_REGISTER,
        comment="触发时机",
    )
    trigger_event: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, comment="群活动触发事件")
    validity_hours: Mapped[int] = mapped_column(Integer, default=168, comment="有效期(小时)")
    target_group_ids: Mapped[Optional[str]] = mapped_column(String(500), nullable=True, comment="目标群组ID列表JSON")
    bot_account_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="Bot账号ID")
    distribution_mode: Mapped[Optional[CampaignDistributionMode]] = mapped_column(
        SQLEnum(CampaignDistributionMode, values_callable=_enum_values, native_enum=False),
        nullable=True,
        comment="分发模式",
    )
    reward_policy_json: Mapped[Optional[str]] = mapped_column(String(2000), nullable=True, comment="奖励策略JSON")
    broadcast_policy_json: Mapped[Optional[str]] = mapped_column(String(2000), nullable=True, comment="广播策略JSON")
    eligibility_policy_json: Mapped[Optional[str]] = mapped_column(String(2000), nullable=True, comment="资格策略JSON")
    
    # Trial settings
    trial_plan_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="试用套餐ID")
    trial_hours: Mapped[int] = mapped_column(Integer, default=24, comment="试用时长(小时)")
    trial_traffic_gb: Mapped[int] = mapped_column(Integer, default=50, comment="试用流量(GB)")
    
    # Gift card settings
    gift_card_template_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="礼品卡模板ID")
    
    # Status
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否启用")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False
    )


class CampaignTracking(Base):
    """Campaign tracking model for conversion tracking."""
    
    __tablename__ = "campaign_tracking"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        comment="用户ID"
    )
    
    campaign_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, comment="活动名称")
    source: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, comment="来源")
    group_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, comment="群组ID")
    keyword: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, comment="触发关键词")
    bot_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, comment="Bot账号ID")
    
    # Timestamps
    registered_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="注册时间")
    converted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="转化时间")
    validity_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="有效期开始时间")

    # Rewards
    trial_granted: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否发放试用")
    coupon_granted: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否发放优惠卷")
    
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    
    # Relationships
    user = relationship("User", backref="campaign_tracking")
    
    __table_args__ = (
        Index("idx_user_id", "user_id"),
        Index("idx_source", "source"),
        Index("idx_campaign", "campaign_name"),
        Index("idx_registered_at", "registered_at"),
    )
