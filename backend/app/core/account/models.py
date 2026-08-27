"""
Telegram Account Models

Database models for Telegram account management.
"""

from datetime import date, datetime
from enum import Enum
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
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


class AccountStatus(str, Enum):
    """Telegram account status."""
    OFFLINE = "offline"
    ONLINE = "online"
    WORKING = "working"
    IDLE = "idle"
    ERROR = "error"
    BANNED = "banned"


class ProxyType(str, Enum):
    """Proxy type enumeration."""
    RESIDENTIAL = "residential"
    DATACENTER = "datacenter"
    MOBILE = "mobile"


class ProxyMode(str, Enum):
    """How a Telegram account chooses its proxy."""
    DYNAMIC = "dynamic"
    STATIC = "static"
    NONE = "none"


class SessionType(str, Enum):
    """Telegram session type."""
    STICKY = "sticky"
    RANDOM = "random"


class AccountType(str, Enum):
    """Telegram account role in Vanguard."""
    PROMOTER = "promoter"
    GUARDIAN_BOT = "guardian_bot"


class AccountAssetTier(str, Enum):
    """Static asset tier for promoter accounts, independent from runtime health."""
    UNKNOWN = "unknown"
    MONTH_1 = "month_1"
    MONTH_3_6 = "month_3_6"
    YEAR_1 = "year_1"
    YEAR_2 = "year_2"
    YEAR_3_PLUS = "year_3_plus"


class AccountRiskLevel(str, Enum):
    """Lifecycle level used by the account risk guard."""
    NORMAL = "normal"
    WATCH = "watch"
    LIMITED = "limited"
    FROZEN = "frozen"
    QUARANTINED = "quarantined"


class AccountBusinessStage(str, Enum):
    """Growth automation stage derived from risk, probes, and delivery health."""
    NEW = "new"
    NORMAL = "normal"
    HOT = "hot"
    COOLDOWN = "cooldown"


class AccountOperationMode(str, Enum):
    """Scope of automation actions allowed for a promoter account."""

    GROWTH = "growth"
    AD_ONLY = "ad_only"


class AccountWarmupStage(str, Enum):
    """Managed-account warmup stage after Vanguard starts operating an account."""
    OBSERVE = "observe"
    SEED = "seed"
    SOFT = "soft"
    RAMP = "ramp"
    NORMAL = "normal"
    COOLDOWN = "cooldown"


class TelegramAccount(Base):
    """
    Telegram user account model.
    
    Stores account credentials and metadata. Proxy is dynamically obtained
    based on the account's country_code.
    """
    
    __tablename__ = "telegram_account"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    phone: Mapped[Optional[str]] = mapped_column(String(20), unique=True, nullable=True, comment="手机号")
    account_type: Mapped[AccountType] = mapped_column(
        SQLEnum(AccountType),
        default=AccountType.PROMOTER,
        nullable=False,
        comment="账号类型",
    )
    identifier: Mapped[str] = mapped_column(
        String(120),
        unique=True,
        nullable=False,
        comment="统一账号标识(手机号或Bot用户名)",
    )
    display_name: Mapped[Optional[str]] = mapped_column(
        String(120),
        nullable=True,
        comment="展示名称",
    )
    profile_bio: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        comment="Telegram账号公开简介",
    )
    profile_bio_synced_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
        comment="公开简介最近同步到Telegram时间",
    )
    asset_tier: Mapped[str] = mapped_column(
        String(30),
        default=AccountAssetTier.UNKNOWN.value,
        nullable=False,
        comment="账号资产等级: unknown/month_1/month_3_6/year_1/year_2/year_3_plus",
    )
    registered_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
        comment="Telegram账号注册时间(运营标注)",
    )
    asset_verified_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
        comment="账号资产等级最近确认时间",
    )
    asset_note: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        comment="账号资产备注/采购批次",
    )
    managed_started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
        comment="账号开始由系统托管运营的时间",
    )
    warmup_stage: Mapped[str] = mapped_column(
        String(20),
        default=AccountWarmupStage.OBSERVE.value,
        nullable=False,
        comment="账号托管暖号阶段: observe/seed/soft/ramp/normal/cooldown",
    )
    warmup_stage_updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
        comment="暖号阶段最近更新时间",
    )
    warmup_hold_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
        comment="人工延长暖号/暂停晋级截止时间",
    )
    warmup_note: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        comment="账号托管暖号备注",
    )
    
    # API configuration binding (supports multiple configs)
    api_config_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("telegram_api_config.id", ondelete="SET NULL"),
        nullable=True,
        comment="API配置ID"
    )
    api_config_name: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="default",
        comment="API配置名称"
    )
    
    # Device fingerprint binding
    fingerprint_id: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
        comment="指纹ID"
    )
    
    # Account country for proxy matching
    country_code: Mapped[str] = mapped_column(
        String(2),
        nullable=False,
        default="US",
        comment="国家代码(ISO 3166-1 alpha-2)"
    )
    country_name: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
        comment="国家名称"
    )
    
    # Country matching for proxy selection
    country_match_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        comment="是否启用国家匹配"
    )
    preferred_country: Mapped[Optional[str]] = mapped_column(
        String(2),
        nullable=True,
        comment="优选国家代码(覆盖手机号推断)"
    )

    # Proxy policy
    proxy_mode: Mapped[ProxyMode] = mapped_column(
        SQLEnum(
            ProxyMode,
            values_callable=lambda values: [item.value for item in values],
            native_enum=False,
        ),
        default=ProxyMode.DYNAMIC,
        nullable=False,
        comment="代理模式: dynamic/static/none",
    )
    static_proxy_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("proxy.id", ondelete="SET NULL"),
        nullable=True,
        comment="静态绑定代理ID",
    )
    
    # Session persistence info
    session_name: Mapped[str] = mapped_column(
        String(100),
        unique=True,
        nullable=False,
        comment="会话名称"
    )
    session_string: Mapped[Optional[Text]] = mapped_column(
        Text,
        nullable=True,
        comment="Telethon session string (用于快速恢复登录)"
    )
    session_hash: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
        comment="会话哈希(用于验证session文件)"
    )
    auth_key_base64: Mapped[Optional[Text]] = mapped_column(
        Text,
        nullable=True,
        comment="认证密钥(加密存储)"
    )
    
    # Status
    status: Mapped[AccountStatus] = mapped_column(
        SQLEnum(AccountStatus),
        default=AccountStatus.OFFLINE,
        nullable=False,
        comment="账号状态"
    )
    
    # Device info for re-authentication
    device_model: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
        comment="设备型号"
    )
    system_version: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
        comment="系统版本"
    )
    app_version: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
        comment="APP版本"
    )
    
    # Timestamps
    last_active_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
        comment="最后活跃时间"
    )
    last_connected_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
        comment="最后连接时间"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False
    )
    
    # Health metrics
    connection_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        comment="连接次数"
    )
    error_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        comment="错误次数"
    )
    risk_score: Mapped[float] = mapped_column(
        Float,
        default=0.0,
        nullable=False,
        comment="账号风控分"
    )
    risk_level: Mapped[str] = mapped_column(
        String(20),
        default=AccountRiskLevel.NORMAL.value,
        nullable=False,
        comment="账号风控等级: normal/watch/limited/frozen/quarantined"
    )
    risk_pause_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
        comment="风控暂停到期时间"
    )
    risk_recovery_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
        comment="风控恢复观察期结束时间"
    )
    last_risk_decay_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
        comment="最近风险分衰减时间"
    )
    risk_reason: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        comment="最近风控原因"
    )
    last_risk_event_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
        comment="最近风控事件时间"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        comment="是否启用"
    )
    static_proxy: Mapped[Optional["Proxy"]] = relationship(
        "Proxy",
        foreign_keys=[static_proxy_id],
        lazy="selectin",
    )
    operation_config: Mapped[Optional["AccountOperationConfig"]] = relationship(
        "AccountOperationConfig",
        back_populates="account",
        uselist=False,
        lazy="selectin",
        cascade="all, delete-orphan",
        single_parent=True,
    )
    
    __table_args__ = (
        Index("idx_status", "status"),
        Index("idx_country", "country_code"),
        Index("idx_api_config", "api_config_name"),
        Index("idx_account_type", "account_type"),
        Index("idx_account_asset_tier", "asset_tier"),
        Index("idx_account_registered_at", "registered_at"),
        Index("idx_account_managed_started_at", "managed_started_at"),
        Index("idx_account_warmup_stage", "warmup_stage"),
        Index("idx_account_static_proxy", "static_proxy_id"),
        Index("idx_account_risk_pause_until", "risk_pause_until"),
        Index("idx_account_risk_level", "risk_level"),
        Index("idx_account_risk_recovery_until", "risk_recovery_until"),
    )


class TelegramAPIConfig(Base):
    """
    Telegram API configuration model.
    
    Supports multiple API configurations for different accounts.
    """
    
    __tablename__ = "telegram_api_config"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(
        String(50),
        unique=True,
        nullable=False,
        comment="配置名称"
    )
    api_id: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="API ID"
    )
    api_hash: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="API Hash"
    )
    
    # Optional description
    description: Mapped[Optional[str]] = mapped_column(
        String(200),
        nullable=True,
        comment="配置描述"
    )
    
    # Usage tracking
    account_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        comment="使用此配置的账号数"
    )
    
    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False
    )
    
    # Relationship
    accounts: Mapped[list["TelegramAccount"]] = relationship(
        "TelegramAccount",
        back_populates="api_config",
        lazy="selectin"
    )


# Add relationship to TelegramAccount
TelegramAccount.api_config = relationship(
    "TelegramAPIConfig",
    back_populates="accounts",
    foreign_keys=[TelegramAccount.api_config_id],
    lazy="joined"
)



class AccountRiskEvent(Base):
    """Audit log for account-level risk decisions and Telegram actions."""

    __tablename__ = "account_risk_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("telegram_account.id", ondelete="SET NULL"),
        nullable=True,
        comment="Telegram账号ID",
    )
    action: Mapped[str] = mapped_column(String(50), nullable=False, comment="动作类型")
    status: Mapped[str] = mapped_column(String(30), nullable=False, comment="allow/block/success/failure/freeze")
    reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, comment="原因")
    target_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, comment="目标类型")
    target_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, comment="目标ID")
    fingerprint_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, comment="指纹ID")
    proxy_mode: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, comment="代理模式")
    proxy_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="静态代理ID")
    proxy_country: Mapped[Optional[str]] = mapped_column(String(2), nullable=True, comment="代理国家")
    details: Mapped[Optional[Text]] = mapped_column(Text, nullable=True, comment="事件详情JSON")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    account = relationship("TelegramAccount", lazy="joined")

    __table_args__ = (
        Index("idx_account_risk_event_account_created", "account_id", "created_at"),
        Index("idx_account_risk_event_action_status", "action", "status"),
        Index("idx_account_risk_event_target", "target_type", "target_id"),
    )


class AccountRiskDailyStat(Base):
    """Daily aggregate for account risk events and action usage."""

    __tablename__ = "account_risk_daily_stat"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("telegram_account.id", ondelete="SET NULL"),
        nullable=True,
        comment="Telegram账号ID",
    )
    stat_date: Mapped[date] = mapped_column(Date, nullable=False, comment="统计日期")
    action: Mapped[str] = mapped_column(String(50), nullable=False, comment="动作类型")
    status: Mapped[str] = mapped_column(String(30), nullable=False, comment="事件状态")
    target_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, comment="目标类型")
    count: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="事件数量")
    last_reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, comment="最近原因")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    account = relationship("TelegramAccount", lazy="joined")

    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "stat_date",
            "action",
            "status",
            "target_type",
            name="uq_account_risk_daily_stat",
        ),
        Index("idx_account_risk_daily_stat_account_date", "account_id", "stat_date"),
        Index("idx_account_risk_daily_stat_date_status", "stat_date", "status"),
    )


class AccountEnvironmentEvent(Base):
    """Audit log for login/runtime proxy and fingerprint environment changes."""

    __tablename__ = "account_environment_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("telegram_account.id", ondelete="SET NULL"),
        nullable=True,
        comment="Telegram??ID",
    )
    event_type: Mapped[str] = mapped_column(String(50), nullable=False, comment="login/import/runtime/proxy_change")
    status: Mapped[str] = mapped_column(String(30), default="ok", nullable=False, comment="ok/warning/block")
    reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, comment="??")
    proxy_mode: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, comment="????")
    proxy_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="????ID")
    proxy_country: Mapped[Optional[str]] = mapped_column(String(2), nullable=True, comment="????")
    fingerprint_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, comment="??ID")
    device_model: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, comment="????")
    system_version: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, comment="????")
    app_version: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, comment="APP??")
    details: Mapped[Optional[Text]] = mapped_column(Text, nullable=True, comment="????JSON")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    account = relationship("TelegramAccount", lazy="joined")

    __table_args__ = (
        Index("idx_account_environment_event_account_created", "account_id", "created_at"),
        Index("idx_account_environment_event_type_status", "event_type", "status"),
    )

class AccountOperationConfig(Base):
    """Per-account automation and risk-control settings."""

    __tablename__ = "telegram_account_operation_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_account.id", ondelete="CASCADE"),
        nullable=False,
        comment="Telegram账号ID",
    )

    auto_join_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, comment="是否自动加群")
    auto_ads_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, comment="是否允许自动广告")
    operation_mode: Mapped[str] = mapped_column(
        String(20),
        default=AccountOperationMode.GROWTH.value,
        server_default=AccountOperationMode.GROWTH.value,
        nullable=False,
        comment="自动化职责: growth/ad_only",
    )

    max_groups_per_day: Mapped[int] = mapped_column(Integer, default=100, nullable=False, comment="每日最大加群数")
    max_groups_total: Mapped[int] = mapped_column(Integer, default=100, nullable=False, comment="账号总群数上限")
    join_interval_min_seconds: Mapped[int] = mapped_column(Integer, default=60, nullable=False, comment="加群最小间隔")
    join_interval_max_seconds: Mapped[int] = mapped_column(Integer, default=900, nullable=False, comment="加群最大间隔")
    next_join_after: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="下次允许自动加群时间")
    last_group_cleanup_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="最近低价值群清理时间")

    max_messages_per_day: Mapped[Optional[int]] = mapped_column(
        Integer,
        default=None,
        nullable=True,
        comment="Per-account outbound message hard-cap override; null uses central default",
    )
    message_interval_seconds: Mapped[int] = mapped_column(Integer, default=3600, nullable=False, comment="消息发送间隔")
    quiet_hours_start: Mapped[Optional[str]] = mapped_column(String(5), nullable=True, comment="免打扰开始 HH:MM")
    quiet_hours_end: Mapped[Optional[str]] = mapped_column(String(5), nullable=True, comment="免打扰结束 HH:MM")

    keyword_types: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="允许用于搜群的关键词类型JSON数组",
    )
    keyword_auto_replenish_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        comment="关键词不足时是否自动补充",
    )
    keyword_replenish_requires_review: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        comment="自动补充关键词是否需要审核",
    )
    risk_level: Mapped[str] = mapped_column(String(20), default="normal", nullable=False, comment="风控等级")
    business_stage: Mapped[str] = mapped_column(
        String(20),
        default=AccountBusinessStage.NEW.value,
        nullable=False,
        comment="增长业务状态: new/normal/hot/cooldown",
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, comment="配置是否启用")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    account = relationship(
        "TelegramAccount",
        back_populates="operation_config",
        lazy="joined",
    )

    __table_args__ = (
        UniqueConstraint("account_id", name="uq_account_operation_config_account"),
        Index("idx_account_operation_auto_join", "auto_join_enabled"),
        Index("idx_account_operation_enabled", "enabled"),
        Index("idx_account_operation_mode", "operation_mode"),
        Index("idx_account_operation_business_stage", "business_stage"),
    )


class GuardianBotHealthStatus(str, Enum):
    """Guardian bot health status."""
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    OFFLINE = "offline"


class GuardianBotProfile(Base):
    """Bot-specific profile bound to a guardian bot account."""

    __tablename__ = "guardian_bot_profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_account.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        comment="基础账号ID",
    )
    bot_token: Mapped[str] = mapped_column(String(255), nullable=False, comment="Telegram Bot Token")
    bot_username: Mapped[Optional[str]] = mapped_column(String(120), nullable=True, comment="Bot用户名")
    bot_user_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, comment="Bot用户ID")
    health_status: Mapped[GuardianBotHealthStatus] = mapped_column(
        SQLEnum(GuardianBotHealthStatus),
        default=GuardianBotHealthStatus.UNKNOWN,
        nullable=False,
        comment="健康状态",
    )
    sync_status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False, comment="群同步状态")
    permissions_snapshot: Mapped[Optional[Text]] = mapped_column(Text, nullable=True, comment="权限快照JSON")
    last_heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="最近心跳")
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="最近同步")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, comment="是否启用")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    account = relationship("TelegramAccount", lazy="joined")

    __table_args__ = (
        Index("idx_guardian_bot_health_status", "health_status"),
        Index("idx_guardian_bot_sync_status", "sync_status"),
    )


class Proxy(Base):
    """
    Proxy model for storing proxy configurations.
    
    Proxies are matched to accounts based on country code.
    """
    
    __tablename__ = "proxy"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    proxy_type: Mapped[ProxyType] = mapped_column(
        SQLEnum(ProxyType),
        default=ProxyType.DATACENTER,
        nullable=False,
        comment="代理类型"
    )
    host: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="代理主机"
    )
    port: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="代理端口"
    )
    protocol: Mapped[str] = mapped_column(
        String(20),
        default="http",
        nullable=False,
        comment="协议: http, socks5"
    )
    username: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
        comment="认证用户名"
    )
    password: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
        comment="认证密码"
    )
    
    # Country info
    country: Mapped[str] = mapped_column(
        String(2),
        nullable=False,
        comment="国家代码(ISO 3166-1 alpha-2)"
    )
    country_name: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
        comment="国家名称"
    )
    
    # Health metrics
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        comment="是否启用"
    )
    success_rate: Mapped[float] = mapped_column(
        default=1.0,
        comment="成功率(0-1)"
    )
    avg_latency: Mapped[int] = mapped_column(
        Integer,
        default=0,
        comment="平均延迟(ms)"
    )
    last_checked: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
        comment="最后检查时间"
    )
    consecutive_failures: Mapped[int] = mapped_column(
        Integer,
        default=0,
        comment="连续失败次数"
    )
    
    # Provider info (for auto-discovered proxies)
    provider: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
        comment="来源Provider"
    )
    
    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False
    )
    
    __table_args__ = (
        Index("idx_proxy_country", "country"),
        Index("idx_proxy_active", "is_active"),
        Index("idx_proxy_type", "proxy_type"),
    )
