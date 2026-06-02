"""
Guardian Module - Models

Database models for violation tracking, moderation rules, and verification.
"""

import json
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
    UniqueConstraint,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


# =============================================================================
# Enums
# =============================================================================

class ViolationAction(str, Enum):
    """Action taken for violation."""
    WARN = "warn"
    MUTE = "mute"
    BAN = "ban"
    KICK = "kick"


class RuleType(str, Enum):
    """Moderation rule type."""
    KEYWORD = "keyword"
    DOMAIN = "domain"
    FREQUENCY = "frequency"
    IMAGE = "image"


class ViolationLevel(str, Enum):
    """Violation severity level."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class VerificationType(str, Enum):
    """Verification type for group join."""
    CAPTCHA = "captcha"
    QUESTION = "question"
    NONE = "none"


class VerificationState(str, Enum):
    """Verification session state."""
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    EXPIRED = "expired"


class ManagedGroupBindingStatus(str, Enum):
    """Lifecycle status of a bot-managed group binding."""
    PENDING = "pending"
    ACTIVE = "active"
    DEGRADED = "degraded"
    INACTIVE = "inactive"


class ManagedGroupBotRole(str, Enum):
    """Role the guardian bot currently has in the group."""
    MEMBER = "member"
    ADMIN = "admin"
    OWNER = "owner"


class SensitiveKeywordSource(str, Enum):
    """Origin of a moderation sensitive keyword."""
    MANUAL = "manual"
    AI_SUGGESTION = "ai_suggestion"
    VIOLATION_FEEDBACK = "violation_feedback"


class CampaignScope(str, Enum):
    """Campaign scope."""
    GLOBAL = "global"
    MANAGED_GROUP = "managed_group"


class GroupCampaignTriggerEvent(str, Enum):
    """Trigger event for group governance campaigns."""
    USER_JOINED = "user_joined"
    VERIFICATION_PASSED = "verification_passed"
    NEW_MEMBER_DELAY = "new_member_delay"
    SCHEDULED = "scheduled"
    MANUAL_BROADCAST = "manual_broadcast"
    PERIODIC = "periodic"


# =============================================================================
# Core Models
# =============================================================================

class Violation(Base):
    """Violation record model."""
    
    __tablename__ = "violation"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False
    )
    group_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    
    rule_type: Mapped[str] = mapped_column(String(50), nullable=False, comment="违规类型")
    rule_pattern: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, comment="匹配规则")
    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="违规内容")
    
    action_taken: Mapped[ViolationAction] = mapped_column(
        SQLEnum(ViolationAction),
        nullable=False
    )
    action_duration: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="惩罚时长(秒)")
    
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    
    user = relationship("User", backref="violations")
    
    __table_args__ = (
        Index("idx_violation_user_group", "user_id", "group_id"),
        Index("idx_violation_created_at", "created_at"),
    )


class ModerationRule(Base):
    """Moderation rule model."""
    
    __tablename__ = "moderation_rule"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_type: Mapped[RuleType] = mapped_column(
        SQLEnum(RuleType),
        nullable=False
    )
    pattern: Mapped[str] = mapped_column(String(255), nullable=False, comment="规则模式")
    level: Mapped[ViolationLevel] = mapped_column(
        SQLEnum(ViolationLevel),
        default=ViolationLevel.MEDIUM
    )
    action: Mapped[ViolationAction] = mapped_column(
        SQLEnum(ViolationAction),
        default=ViolationAction.WARN
    )
    group_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, comment="NULL表示全局规则")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False
    )
    
    __table_args__ = (
        Index("idx_rule_type_enabled", "rule_type", "enabled"),
        Index("idx_rule_group", "group_id"),
    )


class Whitelist(Base):
    """Whitelist model for users and domains."""
    
    __tablename__ = "whitelist"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    whitelist_type: Mapped[str] = mapped_column(String(20), nullable=False, comment="类型: user/domain/path")
    value: Mapped[str] = mapped_column(String(255), nullable=False, comment="值")
    group_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    
    __table_args__ = (
        Index("idx_whitelist_type_value", "whitelist_type", "value"),
        Index("idx_whitelist_expires", "expires_at"),
    )


# =============================================================================
# Verification Models
# =============================================================================

class VerificationSession(Base):
    """Verification session for group join."""
    
    __tablename__ = "verification_session"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, comment="会话ID")
    
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="用户ID")
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="群组ID")
    
    verify_type: Mapped[VerificationType] = mapped_column(
        SQLEnum(VerificationType),
        nullable=False
    )
    state: Mapped[VerificationState] = mapped_column(
        SQLEnum(VerificationState),
        default=VerificationState.PENDING
    )
    
    # 问答配置
    question: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="问题")
    answer: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, comment="答案")
    
    # 验证码（用于captcha）
    captcha_code: Mapped[Optional[str]] = mapped_column(String(10), nullable=True, comment="验证码")
    
    # 重试计数
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, comment="尝试次数")
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, comment="最大尝试次数")
    
    # 时间戳
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    
    __table_args__ = (
        Index("idx_verify_user_chat", "user_id", "chat_id"),
        Index("idx_verify_session", "session_id"),
        Index("idx_verify_expires", "expires_at"),
        Index("idx_verify_state", "state"),
    )


class GroupVerificationConfig(Base):
    """Group verification configuration."""
    
    __tablename__ = "group_verification_config"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, comment="群组ID")
    
    enable_verification: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否启用验证")
    verification_type: Mapped[VerificationType] = mapped_column(
        SQLEnum(VerificationType),
        default=VerificationType.CAPTCHA,
        comment="验证类型"
    )
    
    # 问答配置（JSON格式存储）
    questions: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment='JSON: [{"question": "问题", "answer": "答案"}]'
    )
    
    # 欢迎消息
    welcome_message: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="欢迎消息模板"
    )
    
    # 超时配置
    timeout_minutes: Mapped[int] = mapped_column(Integer, default=5, comment="超时时间(分钟)")
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, comment="最大尝试次数")
    
    # 白名单跳过
    whitelist_bypass: Mapped[bool] = mapped_column(Boolean, default=True, comment="白名单跳过")
    auto_kick_unverified: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否自动踢出未验证用户")
    kick_after_minutes: Mapped[int] = mapped_column(Integer, default=10, comment="多少分钟后踢出")
    
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False
    )
    
    def get_questions(self) -> list[dict]:
        """解析问答配置JSON"""
        if not self.questions:
            return []
        try:
            return json.loads(self.questions)
        except (json.JSONDecodeError, TypeError):
            return []
    
    def set_questions(self, questions: list[dict]) -> None:
        """设置问答配置"""
        self.questions = json.dumps(questions, ensure_ascii=False)


# =============================================================================
# Moderation Review Models
# =============================================================================

class KeywordSuggestion(Base):
    """Keyword suggestion review item."""

    __tablename__ = "keyword_suggestion"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    keyword: Mapped[str] = mapped_column(String(255), nullable=False, comment="候选关键词")
    category: Mapped[str] = mapped_column(String(50), nullable=False, comment="分类")
    confidence: Mapped[float] = mapped_column(default=0.0, comment="置信度")
    source_sample: Mapped[str] = mapped_column(Text, nullable=False, comment="来源样本")
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False, comment="状态")
    reject_reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, comment="拒绝原因")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    __table_args__ = (
        Index("idx_keyword_suggestion_status", "status"),
        Index("idx_keyword_suggestion_category", "category"),
        Index("idx_keyword_suggestion_created_at", "created_at"),
    )


class ModerationSensitiveKeyword(Base):
    """Dedicated keyword library for guardian moderation."""

    __tablename__ = "moderation_sensitive_keyword"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    text: Mapped[str] = mapped_column(String(255), nullable=False, comment="敏感词")
    normalized_text: Mapped[str] = mapped_column(String(255), nullable=False, comment="标准化敏感词")
    category: Mapped[str] = mapped_column(String(50), default="sensitive", nullable=False, comment="分类")
    source: Mapped[SensitiveKeywordSource] = mapped_column(
        SQLEnum(SensitiveKeywordSource),
        default=SensitiveKeywordSource.MANUAL,
        nullable=False,
        comment="来源",
    )
    level: Mapped[ViolationLevel] = mapped_column(
        SQLEnum(ViolationLevel),
        default=ViolationLevel.MEDIUM,
        nullable=False,
        comment="严重等级",
    )
    action: Mapped[ViolationAction] = mapped_column(
        SQLEnum(ViolationAction),
        default=ViolationAction.WARN,
        nullable=False,
        comment="建议动作",
    )
    group_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, comment="群级敏感词")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, comment="是否启用")
    confidence: Mapped[float] = mapped_column(default=1.0, comment="置信度")
    source_sample: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="来源样本")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("normalized_text", "group_id", name="uq_moderation_sensitive_keyword_text_group"),
        Index("idx_moderation_sensitive_group_enabled", "group_id", "enabled"),
        Index("idx_moderation_sensitive_category", "category"),
    )


class ManagedGroupBinding(Base):
    """Primary guardian-bot binding for a managed group."""

    __tablename__ = "managed_group_binding"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("group.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        comment="内部群ID",
    )
    telegram_group_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="Telegram群组ID")
    bot_account_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_account.id", ondelete="CASCADE"),
        nullable=False,
        comment="Bot账号ID",
    )
    binding_status: Mapped[ManagedGroupBindingStatus] = mapped_column(
        SQLEnum(ManagedGroupBindingStatus),
        default=ManagedGroupBindingStatus.PENDING,
        nullable=False,
        comment="绑定状态",
    )
    bot_role: Mapped[ManagedGroupBotRole] = mapped_column(
        SQLEnum(ManagedGroupBotRole),
        default=ManagedGroupBotRole.MEMBER,
        nullable=False,
        comment="Bot角色",
    )
    permissions_snapshot: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="权限快照JSON")
    bound_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    group = relationship("Group", lazy="joined")
    bot_account = relationship("TelegramAccount", lazy="joined")

    __table_args__ = (
        Index("idx_managed_group_binding_bot", "bot_account_id"),
        Index("idx_managed_group_binding_status", "binding_status"),
    )


class GroupModerationPolicy(Base):
    """Group-level moderation policy for throttling and anti-spam."""

    __tablename__ = "group_moderation_policy"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[Optional[int]] = mapped_column(BigInteger, unique=True, nullable=True, comment="NULL表示全局默认")
    message_interval_seconds: Mapped[int] = mapped_column(Integer, default=10, comment="发言间隔")
    max_messages_per_minute: Mapped[int] = mapped_column(Integer, default=5, comment="每分钟最大消息")
    max_links_per_hour: Mapped[int] = mapped_column(Integer, default=3, comment="每小时最大链接数")
    new_member_silent_minutes: Mapped[int] = mapped_column(Integer, default=5, comment="新人静默分钟数")
    first_speak_delay_seconds: Mapped[int] = mapped_column(Integer, default=30, comment="首次发言延迟")
    media_policy: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="媒体策略JSON")
    link_policy: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="链接策略JSON")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    __table_args__ = (
        Index("idx_group_moderation_policy_group", "group_id"),
    )


class GroupPunishmentPolicy(Base):
    """Group-level punishment policy."""

    __tablename__ = "group_punishment_policy"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[Optional[int]] = mapped_column(BigInteger, unique=True, nullable=True, comment="NULL表示全局默认")
    warn_threshold: Mapped[int] = mapped_column(Integer, default=3, comment="警告阈值")
    mute_on_warn_threshold: Mapped[bool] = mapped_column(Boolean, default=True, comment="达到警告阈值后禁言")
    mute_duration_seconds: Mapped[int] = mapped_column(Integer, default=300, comment="禁言时长")
    ban_on_warn_threshold: Mapped[int] = mapped_column(Integer, default=5, comment="多少次后封禁")
    repeat_violation_window_hours: Mapped[int] = mapped_column(Integer, default=24, comment="重复违规时间窗")
    auto_reset_warning_days: Mapped[int] = mapped_column(Integer, default=7, comment="警告自动重置天数")
    severe_violation_direct_action: Mapped[ViolationAction] = mapped_column(
        SQLEnum(ViolationAction),
        default=ViolationAction.MUTE,
        nullable=False,
        comment="严重违规直接动作",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    __table_args__ = (
        Index("idx_group_punishment_policy_group", "group_id"),
    )


# =============================================================================
# Coupon Models
# =============================================================================

class CouponDistribution(Base):
    """Coupon distribution record."""
    
    __tablename__ = "coupon_distribution"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False
    )
    campaign_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("campaign.id", ondelete="SET NULL"),
        nullable=True
    )
    
    distribution_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        comment="类型: trial/discount/gift"
    )
    coupon_code: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
        comment="优惠码"
    )
    
    # 奖励详情
    trial_hours: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="试用时长(小时)")
    traffic_gb: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="流量(GB)")
    
    distributed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    
    user = relationship("User", backref="coupon_distributions")
    campaign = relationship("Campaign", backref="distributions")
    
    __table_args__ = (
        Index("idx_coupon_user", "user_id"),
        Index("idx_coupon_campaign", "campaign_id"),
    )
