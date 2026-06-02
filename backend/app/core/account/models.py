"""
Telegram Account Models

Database models for Telegram account management.
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
    Text,
    UniqueConstraint,
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


class SessionType(str, Enum):
    """Telegram session type."""
    STICKY = "sticky"
    RANDOM = "random"


class AccountType(str, Enum):
    """Telegram account role in Vanguard."""
    PROMOTER = "promoter"
    GUARDIAN_BOT = "guardian_bot"


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
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        comment="是否启用"
    )
    
    __table_args__ = (
        Index("idx_status", "status"),
        Index("idx_country", "country_code"),
        Index("idx_api_config", "api_config_name"),
        Index("idx_account_type", "account_type"),
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

    max_groups_per_day: Mapped[int] = mapped_column(Integer, default=5, nullable=False, comment="每日最大加群数")
    max_groups_total: Mapped[int] = mapped_column(Integer, default=100, nullable=False, comment="账号总群数上限")
    join_interval_min_seconds: Mapped[int] = mapped_column(Integer, default=1800, nullable=False, comment="加群最小间隔")
    join_interval_max_seconds: Mapped[int] = mapped_column(Integer, default=7200, nullable=False, comment="加群最大间隔")
    next_join_after: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="下次允许自动加群时间")

    max_messages_per_day: Mapped[int] = mapped_column(Integer, default=30, nullable=False, comment="每日最大消息数")
    message_interval_seconds: Mapped[int] = mapped_column(Integer, default=300, nullable=False, comment="消息发送间隔")
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
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, comment="配置是否启用")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    account = relationship("TelegramAccount", lazy="joined")

    __table_args__ = (
        UniqueConstraint("account_id", name="uq_account_operation_config_account"),
        Index("idx_account_operation_auto_join", "auto_join_enabled"),
        Index("idx_account_operation_enabled", "enabled"),
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
