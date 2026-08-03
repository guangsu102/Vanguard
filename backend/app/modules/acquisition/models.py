"""
Acquisition Module - Models

Database models for acquisition tracking, message templates, and conversation flows.
"""

import json
from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    Enum as SQLEnum,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

# =============================================================================
# Enums
# =============================================================================

class TriggerType(str, Enum):
    """Trigger type for keyword matching."""
    KEYWORD = "keyword"
    COMMAND = "command"
    MEMBER_JOIN = "member_join"
    BOT_MENTION = "bot_mention"


class TriggerAction(str, Enum):
    """Action to take when trigger matches."""
    REPLY_TEMPLATE = "reply_template"
    REPLY_AI = "reply_ai"
    SEND_PRIVATE = "send_private"
    REACT = "react"
    PIN_MESSAGE = "pin_message"


class GuideState(str, Enum):
    """User guide flow state."""
    INIT = "init"
    AWAITING_REGISTRATION = "awaiting_registration"
    REGISTERED = "registered"
    CLOSED = "closed"


class MessageType(str, Enum):
    """Message type for auto发言."""
    INTERACTION = "interaction"   # 互动型
    AI_WARMUP = "ai_warmup"       # AI主动暖场
    SHARE = "share"              # 分享型
    GUIDE = "guide"              # 引导型
    QA = "qa"                    # 问答型


class AdCreativeType(str, Enum):
    """Advertisement creative type."""
    TEXT = "text"
    IMAGE = "image"
    MIXED = "mixed"


class AdSendMode(str, Enum):
    """How an advertisement campaign should be delivered."""
    AFTER_JOIN = "after_join"
    INTERVAL = "interval"
    SCHEDULED = "scheduled"


class DeliveryStatus(str, Enum):
    """Delivery lifecycle status for automation logs."""
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class AdSurvivalStatus(str, Enum):
    """Post-delivery message survival check status."""
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    SURVIVED = "survived"
    DELETED = "deleted"
    CHECK_FAILED = "check_failed"


class GroupAdTier(str, Enum):
    """Per-group soft-ad capacity tier."""
    BLOCKED = "blocked"
    OBSERVING = "observing"
    TRIAL = "trial"
    VALIDATED = "validated"
    STABLE = "stable"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    PREMIUM = "premium"


class GroupAdPolicyMode(str, Enum):
    """Evidence-backed group advertising permission."""
    FORBIDDEN = "forbidden"
    UNKNOWN = "unknown"
    APPROVAL_REQUIRED = "approval_required"
    SOFT_AD_TRIAL = "soft_ad_trial"
    SOFT_AD_ALLOWED = "soft_ad_allowed"
    HIGH_VOLUME_AD_ALLOWED = "high_volume_ad_allowed"


class SearchKeywordStatus(str, Enum):
    """Group-search keyword review status."""
    PENDING = "pending"
    APPROVED = "approved"
    DISCARDED = "discarded"


class SearchKeywordSource(str, Enum):
    """Origin of a group-search keyword."""
    MANUAL = "manual"
    AI = "ai"
    IMPORT = "import"
    AUTOMATION = "automation"


# =============================================================================
# Search & Tracking Models
# =============================================================================

class GroupSearchRecord(Base):
    """Group search record for tracking search operations."""

    __tablename__ = "acquisition_group_search"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    keyword: Mapped[str] = mapped_column(String(255), nullable=False, comment="搜索关键词")
    group_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="群组ID")
    group_title: Mapped[Optional[str]] = mapped_column(String(500), nullable=True, comment="群名称")
    member_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="成员数")
    found_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("idx_search_keyword", "keyword"),
        Index("idx_search_found_at", "found_at"),
    )


class GroupSearchKeyword(Base):
    """Dedicated keyword library for growth-side group search and auto join."""

    __tablename__ = "acquisition_group_search_keyword"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    text: Mapped[str] = mapped_column(String(255), nullable=False, comment="搜群关键词")
    normalized_text: Mapped[str] = mapped_column(
        String(255),
        default="",
        server_default="",
        nullable=False,
        comment="规范化搜群关键词，用于快速判重",
    )
    keyword_type: Mapped[str] = mapped_column(String(50), nullable=False, comment="关键词类型")
    status: Mapped[SearchKeywordStatus] = mapped_column(
        SQLEnum(SearchKeywordStatus),
        default=SearchKeywordStatus.PENDING,
        nullable=False,
        comment="审核状态",
    )
    source: Mapped[SearchKeywordSource] = mapped_column(
        SQLEnum(SearchKeywordSource),
        default=SearchKeywordSource.MANUAL,
        nullable=False,
        comment="来源",
    )
    match_mode: Mapped[str] = mapped_column(String(20), default="fuzzy", nullable=False, comment="匹配模式")
    trigger_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="命中次数")
    use_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="搜群使用次数")
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="首次用于搜群时间")
    requires_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, comment="是否需要审核")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, comment="是否启用")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("text", "keyword_type", name="uq_group_search_keyword_text_type"),
        Index("idx_group_search_keyword_status", "status"),
        Index("idx_group_search_keyword_type", "keyword_type"),
        Index("idx_group_search_keyword_used", "used_at"),
        Index("idx_group_search_keyword_normalized", "keyword_type", "normalized_text"),
        Index("idx_group_search_keyword_list", "updated_at", "id"),
        Index("idx_group_search_keyword_searchable", "keyword_type", "status", "enabled", "used_at"),
    )


class AutoJoinAttempt(Base):
    """Record of automatic group-join decisions and outcomes."""

    __tablename__ = "acquisition_auto_join_attempt"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_account.id", ondelete="CASCADE"),
        nullable=False,
        comment="账号ID",
    )
    group_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("group.id", ondelete="SET NULL"),
        nullable=True,
        comment="群组表ID",
    )
    telegram_group_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, comment="Telegram群组ID")
    group_username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, comment="群用户名")
    group_title: Mapped[Optional[str]] = mapped_column(String(500), nullable=True, comment="群名称")
    source_keyword: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, comment="来源关键词")
    status: Mapped[str] = mapped_column(
        String(30),
        default=DeliveryStatus.PENDING.value,
        nullable=False,
        comment="状态",
    )
    reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, comment="跳过/失败原因")
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="错误详情")
    attempted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    joined_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    account = relationship("TelegramAccount", lazy="joined")
    group = relationship("Group", lazy="joined")

    __table_args__ = (
        Index("idx_auto_join_account_status", "account_id", "status"),
        Index("idx_auto_join_attempted_at", "attempted_at"),
        Index("idx_auto_join_tg_group", "telegram_group_id"),
    )


class AcquisitionTracking(Base):
    """Tracking model for user acquisition from Telegram."""

    __tablename__ = "acquisition_tracking"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tracking_code: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, comment="追踪码")
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
        comment="用户ID"
    )

    # Source information
    source_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, comment="来源类型")
    campaign_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, comment="活动名称")
    group_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, comment="群组ID")
    keyword: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, comment="触发关键词")
    bot_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, comment="Bot账号ID")

    # Timestamps
    click_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="点击时间")
    registered_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="注册时间")
    converted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="转化时间")

    # Status
    converted: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否转化")
    coupon_status: Mapped[Optional[str]] = mapped_column(String(30), nullable=True, comment="优惠券状态")
    coupon_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, comment="优惠券码")
    trial_granted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, comment="是否发放试用")
    external_user_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, comment="XBoard用户ID或UUID")
    last_event_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="最近XBoard事件时间")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", backref="acquisition_tracking")

    __table_args__ = (
        Index("idx_tracking_code", "tracking_code"),
        Index("idx_tracking_user", "user_id"),
        Index("idx_tracking_source", "source_type"),
    )


# =============================================================================
# Message & Trigger Models
# =============================================================================

class AcquisitionMessage(Base):
    """Message record for auto发言 tracking."""

    __tablename__ = "acquisition_message"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_account.id", ondelete="CASCADE"),
        nullable=False
    )
    group_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="群组ID")
    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="消息内容")
    message_type: Mapped[MessageType] = mapped_column(
        String(50),
        nullable=False,
        comment="消息类型"
    )
    message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, comment="Telegram消息ID")
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("idx_msg_account_group", "account_id", "group_id"),
        Index("idx_msg_sent_at", "sent_at"),
    )


class KeywordTrigger(Base):
    """Keyword trigger configuration."""

    __tablename__ = "acquisition_keyword_trigger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    keyword_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("keyword.id", ondelete="SET NULL"),
        nullable=True
    )
    keyword_text: Mapped[str] = mapped_column(String(255), nullable=False, comment="关键词文本")

    trigger_type: Mapped[TriggerType] = mapped_column(
        SQLEnum(TriggerType),
        default=TriggerType.KEYWORD,
        nullable=False
    )
    action: Mapped[TriggerAction] = mapped_column(
        SQLEnum(TriggerAction),
        nullable=False,
        comment="触发动作"
    )

    # Reply configuration
    template_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("acquisition_message_template.id", ondelete="SET NULL"),
        nullable=True
    )
    use_ai_reply: Mapped[bool] = mapped_column(Boolean, default=False, comment="使用AI回复")

    # Limits
    cooldown_seconds: Mapped[int] = mapped_column(Integer, default=300, comment="冷却时间(秒)")
    max_triggers_per_user: Mapped[int] = mapped_column(Integer, default=5, comment="单用户最大触发次数")
    max_triggers_per_group: Mapped[int] = mapped_column(Integer, default=10, comment="单群最大触发次数")

    # Priority & Status
    priority: Mapped[int] = mapped_column(Integer, default=0, comment="优先级")
    requires_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, comment="是否需要人工审核")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, comment="是否启用")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    keyword = relationship("Keyword")
    template = relationship("MessageTemplate")

    __table_args__ = (
        Index("idx_trigger_keyword", "keyword_id"),
        Index("idx_trigger_enabled", "enabled"),
        Index("idx_trigger_requires_review", "requires_review"),
    )


class MessageTemplate(Base):
    """Message template for auto发言 and replies."""

    __tablename__ = "acquisition_message_template"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, comment="模板名称")
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="模板内容")

    # 支持的变量：{{user_name}}, {{group_name}}, {{bot_name}}, {{register_link}}
    template_variables: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="可用变量，逗号分隔"
    )

    message_type: Mapped[MessageType] = mapped_column(
        String(50),
        nullable=False,
        comment="消息类型"
    )

    # Usage constraints
    cooldown_seconds: Mapped[int] = mapped_column(Integer, default=300, comment="冷却时间")
    max_uses_per_day: Mapped[int] = mapped_column(Integer, default=100, comment="每日最大使用次数")

    # Status
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False
    )

    def get_variables(self) -> list[str]:
        """Get list of available variables."""
        if not self.template_variables:
            return []
        return [v.strip() for v in self.template_variables.split(",")]

    def render(self, **kwargs) -> str:
        """Render template with provided variables."""
        content = self.content
        for key, value in kwargs.items():
            placeholder = f"{{{{{key}}}}}"
            content = content.replace(placeholder, str(value))
        return content


class TriggerRecord(Base):
    """Record of trigger events for tracking and analytics."""

    __tablename__ = "acquisition_trigger_record"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    trigger_id: Mapped[int] = mapped_column(
        ForeignKey("acquisition_keyword_trigger.id", ondelete="CASCADE"),
        nullable=False
    )

    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="触发用户ID")
    group_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, comment="群组ID")
    message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, comment="消息ID")

    # Matched content
    matched_keyword: Mapped[str] = mapped_column(String(255), nullable=False, comment="匹配的关键词")
    user_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="用户消息")

    # Action taken
    action_taken: Mapped[TriggerAction] = mapped_column(
        SQLEnum(TriggerAction),
        nullable=False
    )
    reply_content: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="回复内容")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    trigger = relationship("KeywordTrigger")

    __table_args__ = (
        Index("idx_record_trigger", "trigger_id"),
        Index("idx_record_user", "user_id"),
        Index("idx_record_created", "created_at"),
    )


# =============================================================================
# Conversation & Guide Flow Models
# =============================================================================

class GuideFlow(Base):
    """User guide flow tracking."""

    __tablename__ = "acquisition_guide_flow"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True, comment="用户ID")

    state: Mapped[GuideState] = mapped_column(
        SQLEnum(GuideState),
        default=GuideState.INIT,
        nullable=False
    )
    current_step: Mapped[int] = mapped_column(Integer, default=0, comment="当前步骤")

    # Source information
    source_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, comment="来源类型")
    source_group_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, comment="来源群组")
    source_keyword: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, comment="触发关键词")
    tracking_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, comment="追踪码")

    # Step tracking (JSON)
    steps_completed: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="已完成的步骤JSON"
    )

    # Timestamps
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    last_message_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    def get_completed_steps(self) -> list[str]:
        """Get list of completed steps."""
        if not self.steps_completed:
            return []
        try:
            return json.loads(self.steps_completed)
        except (json.JSONDecodeError, TypeError):
            return []

    def set_completed_steps(self, steps: list[str]) -> None:
        """Set completed steps."""
        self.steps_completed = json.dumps(steps, ensure_ascii=False)

    def add_completed_step(self, step: str) -> None:
        """Add a completed step."""
        steps = self.get_completed_steps()
        if step not in steps:
            steps.append(step)
            self.set_completed_steps(steps)


class ConversationContext(Base):
    """Conversation context for multi-message dialogs."""

    __tablename__ = "acquisition_conversation_context"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True, comment="用户ID")

    # Context data (JSON)
    context_data: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="上下文数据JSON")

    # Message history (last N messages)
    message_history: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="消息历史JSON"
    )

    # Expiration
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False
    )

    def get_context(self) -> dict:
        """Get context data as dict."""
        if not self.context_data:
            return {}
        try:
            return json.loads(self.context_data)
        except (json.JSONDecodeError, TypeError):
            return {}

    def set_context(self, data: dict) -> None:
        """Set context data."""
        self.context_data = json.dumps(data, ensure_ascii=False)

    def update_context(self, updates: dict) -> None:
        """Update context with new data."""
        context = self.get_context()
        context.update(updates)
        self.set_context(context)

    def get_history(self) -> list[dict]:
        """Get message history."""
        if not self.message_history:
            return []
        try:
            return json.loads(self.message_history)
        except (json.JSONDecodeError, TypeError):
            return []

    def add_message(self, role: str, content: str) -> None:
        """Add message to history."""
        history = self.get_history()
        history.append({"role": role, "content": content, "timestamp": datetime.utcnow().isoformat()})
        # Keep only last 10 messages
        if len(history) > 10:
            history = history[-10:]
        self.message_history = json.dumps(history, ensure_ascii=False)


# =============================================================================
# Campaign & Activity Models
# =============================================================================

class AcquisitionCampaign(Base):
    """Acquisition campaign configuration."""

    __tablename__ = "acquisition_campaign"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, comment="活动名称")

    # Target configuration
    target_keywords: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="目标关键词JSON数组"
    )
    target_groups: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="目标群组JSON数组"
    )

    # Active templates
    active_templates: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="启用的模板ID JSON数组"
    )

    # Schedule
    start_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="开始日期")
    end_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="结束日期")

    # Status
    status: Mapped[str] = mapped_column(String(50), default="pending", comment="状态")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否启用")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False
    )

    def get_keywords(self) -> list[str]:
        """Get target keywords list."""
        if not self.target_keywords:
            return []
        try:
            return json.loads(self.target_keywords)
        except (json.JSONDecodeError, TypeError):
            return []

    def get_groups(self) -> list[int]:
        """Get target group IDs list."""
        if not self.target_groups:
            return []
        try:
            return json.loads(self.target_groups)
        except (json.JSONDecodeError, TypeError):
            return []

    def get_templates(self) -> list[int]:
        """Get active template IDs list."""
        if not self.active_templates:
            return []
        try:
            return json.loads(self.active_templates)
        except (json.JSONDecodeError, TypeError):
            return []


# =============================================================================
# Advertisement Models
# =============================================================================

class AdCreative(Base):
    """Managed advertisement creative."""

    __tablename__ = "ad_creative"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, comment="广告名称")
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="广告正文")
    creative_type: Mapped[str] = mapped_column(
        String(30),
        default=AdCreativeType.TEXT.value,
        nullable=False,
        comment="素材类型",
    )
    media_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="媒体URL或file_id")
    link_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="落地页链接")
    weight: Mapped[int] = mapped_column(Integer, default=100, nullable=False, comment="轮播权重")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, comment="是否启用")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    __table_args__ = (
        Index("idx_ad_creative_enabled", "enabled"),
    )


class AdCampaign(Base):
    """Advertisement delivery campaign."""

    __tablename__ = "ad_campaign"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, comment="广告计划名称")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, comment="是否启用")
    status: Mapped[str] = mapped_column(String(30), default="draft", nullable=False, comment="状态")
    send_mode: Mapped[str] = mapped_column(
        String(30),
        default=AdSendMode.AFTER_JOIN.value,
        nullable=False,
        comment="发送模式",
    )
    target_group_levels: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="目标群等级JSON数组，如[A,B]",
    )
    target_group_ids: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="指定目标群数据库ID JSON数组；为空时按群等级投放",
    )
    start_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="开始时间")
    end_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="结束时间")
    min_wait_after_join_minutes: Mapped[int] = mapped_column(Integer, default=60, nullable=False, comment="入群后等待分钟")
    interval_minutes: Mapped[int] = mapped_column(Integer, default=1440, nullable=False, comment="间隔发送分钟")
    scheduled_times: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="每日定时点JSON数组 HH:MM")
    max_sends_per_group_per_day: Mapped[int] = mapped_column(Integer, default=1, nullable=False, comment="单群每日上限")
    max_sends_per_account_per_day: Mapped[int] = mapped_column(Integer, default=3, nullable=False, comment="单账号每日上限")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    bindings = relationship("AccountAdBinding", back_populates="campaign", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_ad_campaign_enabled", "enabled"),
        Index("idx_ad_campaign_mode", "send_mode"),
    )

    def get_target_levels(self) -> list[str]:
        """Return target group levels. Defaults to A-only for safety."""
        if not self.target_group_levels:
            return ["A"]
        try:
            levels = json.loads(self.target_group_levels)
            return [str(level) for level in levels if str(level)]
        except (json.JSONDecodeError, TypeError):
            return ["A"]

    def get_target_group_ids(self) -> list[int]:
        """Return explicitly targeted group database IDs."""
        if not self.target_group_ids:
            return []
        try:
            values = json.loads(self.target_group_ids)
        except (json.JSONDecodeError, TypeError):
            return []

        result: list[int] = []
        for value in values if isinstance(values, list) else []:
            try:
                group_id = int(value)
            except (TypeError, ValueError):
                continue
            if group_id > 0 and group_id not in result:
                result.append(group_id)
        return result

    def get_scheduled_times(self) -> list[str]:
        """Return configured daily send times."""
        if not self.scheduled_times:
            return []
        try:
            times = json.loads(self.scheduled_times)
            return [str(item) for item in times if str(item)]
        except (json.JSONDecodeError, TypeError):
            return []


class AccountAdBinding(Base):
    """Bind an account to an advertisement campaign and optional creative."""

    __tablename__ = "account_ad_binding"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_account.id", ondelete="CASCADE"),
        nullable=False,
        comment="账号ID",
    )
    ad_campaign_id: Mapped[int] = mapped_column(
        ForeignKey("ad_campaign.id", ondelete="CASCADE"),
        nullable=False,
        comment="广告计划ID",
    )
    creative_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("ad_creative.id", ondelete="SET NULL"),
        nullable=True,
        comment="指定素材ID",
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, comment="是否启用")
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="优先级")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    account = relationship("TelegramAccount", lazy="joined")
    campaign = relationship("AdCampaign", back_populates="bindings")
    creative = relationship("AdCreative", lazy="joined")

    __table_args__ = (
        UniqueConstraint("account_id", "ad_campaign_id", "creative_id", name="uq_account_ad_binding"),
        Index("idx_account_ad_binding_account", "account_id"),
        Index("idx_account_ad_binding_campaign", "ad_campaign_id"),
        Index("idx_account_ad_binding_enabled", "enabled"),
    )


class GroupAdProfile(Base):
    """Per-group soft advertisement capacity and survival profile."""

    __tablename__ = "group_ad_profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("group.id", ondelete="CASCADE"),
        nullable=False,
        comment="群组表ID",
    )
    telegram_group_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="Telegram群组ID")
    ad_policy_mode: Mapped[str] = mapped_column(
        String(40),
        default=GroupAdPolicyMode.UNKNOWN.value,
        nullable=False,
        comment="广告许可: forbidden/unknown/approval_required/soft_ad_trial/soft_ad_allowed/high_volume_ad_allowed",
    )
    ad_policy_confidence: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="广告许可置信度0-100")
    ad_policy_source: Mapped[Optional[str]] = mapped_column(String(80), nullable=True, comment="广告许可证据来源")
    ad_policy_verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="广告许可确认时间")
    ad_policy_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="广告许可失效时间")
    ad_tier: Mapped[str] = mapped_column(
        String(30),
        default=GroupAdTier.OBSERVING.value,
        nullable=False,
        comment="软广承载等级: blocked/observing/trial/validated/stable/high/premium",
    )
    daily_capacity: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="群每日软广承载上限")
    score: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="软广承载评分")
    survival_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="广告存活次数")
    deleted_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="广告删除次数")
    consecutive_survivals: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="连续存活次数")
    consecutive_deletions: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="连续删除次数")
    tier_changed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="广告档位最近变更时间")
    paused_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="群广告暂停截止时间")
    last_probe_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="最近探测时间")
    last_survived_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="最近广告存活时间")
    last_deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="最近广告删除时间")
    blocked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="软广阻断时间")
    blocked_reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, comment="软广阻断原因")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    group = relationship("Group", lazy="joined")

    __table_args__ = (
        UniqueConstraint("group_id", name="uq_group_ad_profile_group"),
        Index("idx_group_ad_profile_tg_group", "telegram_group_id"),
        Index("idx_group_ad_profile_tier", "ad_tier"),
        Index("idx_group_ad_profile_blocked", "blocked_at"),
    )


class GroupAdPolicyEvent(Base):
    """Immutable audit trail for automated and manual group ad-policy changes."""

    __tablename__ = "group_ad_policy_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("group.id", ondelete="CASCADE"), nullable=False)
    telegram_group_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    previous_mode: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    new_mode: Mapped[str] = mapped_column(String(40), nullable=False)
    confidence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    evidence: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    changed_by_user_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("idx_group_ad_policy_event_group", "group_id", "created_at"),
        Index("idx_group_ad_policy_event_mode", "new_mode", "created_at"),
    )


class AdDeliveryLog(Base):
    """Record advertisement delivery attempts."""

    __tablename__ = "ad_delivery_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_account.id", ondelete="CASCADE"),
        nullable=False,
        comment="账号ID",
    )
    group_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("group.id", ondelete="SET NULL"),
        nullable=True,
        comment="群组表ID",
    )
    telegram_group_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="Telegram群组ID")
    ad_campaign_id: Mapped[int] = mapped_column(
        ForeignKey("ad_campaign.id", ondelete="CASCADE"),
        nullable=False,
        comment="广告计划ID",
    )
    creative_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("ad_creative.id", ondelete="SET NULL"),
        nullable=True,
        comment="素材ID",
    )
    status: Mapped[str] = mapped_column(
        String(30),
        default=DeliveryStatus.PENDING.value,
        nullable=False,
        comment="发送状态",
    )
    telegram_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, comment="Telegram消息ID")
    survival_status: Mapped[str] = mapped_column(
        String(30),
        default=AdSurvivalStatus.NOT_REQUIRED.value,
        nullable=False,
        comment="广告2分钟存活检测状态",
    )
    survival_stage: Mapped[str] = mapped_column(
        String(30),
        default="two_minute",
        nullable=False,
        comment="下一存活检测阶段: two_minute/one_hour/twenty_four_hour/complete",
    )
    survival_check_due_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="广告存活检测时间")
    survival_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="广告存活实际检测时间")
    survived_two_minute_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="广告2分钟存活时间")
    survived_one_hour_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="广告1小时存活时间")
    survived_twenty_four_hour_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="广告24小时存活时间")
    survival_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="广告存活检测错误")
    survival_retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="存活检测重试次数")
    reservation_token: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, comment="广告发送预留幂等标识")
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="错误详情")
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="发送时间")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    account = relationship("TelegramAccount", lazy="joined")
    group = relationship("Group", lazy="joined")
    campaign = relationship("AdCampaign", lazy="joined")
    creative = relationship("AdCreative", lazy="joined")

    __table_args__ = (
        Index("idx_ad_delivery_account_sent", "account_id", "sent_at"),
        Index("idx_ad_delivery_group_sent", "telegram_group_id", "sent_at"),
        Index("idx_ad_delivery_campaign", "ad_campaign_id"),
        Index("idx_ad_delivery_status", "status"),
        Index("idx_ad_delivery_survival_due", "survival_status", "survival_check_due_at"),
        Index("idx_ad_delivery_reservation", "reservation_token", unique=True),
    )
