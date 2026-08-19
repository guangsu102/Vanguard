"""
Vanguard Configuration Module

Application configuration using Pydantic Settings with environment variable support.
"""

from functools import lru_cache

from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_DEV_SECRET = "vanguard-development-secret-key-must-be-replaced-in-production-with-minimum-sixty-four-characters-long"
PLACEHOLDER_VANGUARD_SIGNING_SECRET = "replace-with-shared-secret"
PLACEHOLDER_VANGUARD_CALLBACK_SECRET = "replace-with-callback-secret"


class Settings(BaseSettings):
    """Application settings with environment variable support."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    APP_ENV: str = Field(default="development", description="Application environment")
    DEBUG: bool = Field(default=True, description="Debug mode")
    SECRET_KEY: str = Field(default=DEFAULT_DEV_SECRET, description="Secret key for JWT (minimum 64 characters)")
    JWT_SECRET: str = Field(default=DEFAULT_DEV_SECRET, description="JWT secret key (minimum 64 characters for HS256)")
    JWT_ALGORITHM: str = Field(default="HS256", description="JWT algorithm")
    JWT_EXPIRATION_HOURS: int = Field(default=24, description="JWT expiration in hours")

    @field_validator("JWT_SECRET", "SECRET_KEY")
    @classmethod
    def validate_secret_length(cls, v: str) -> str:
        if len(v) < 64:
            raise ValueError("JWT secret must be at least 64 characters long for HS256 security")
        return v

    @field_validator("SUB2API_ALERT_WEBHOOK_SECRET")
    @classmethod
    def validate_sub2api_alert_secret(cls, value: str | None) -> str | None:
        if value is not None and value.strip() and len(value.strip()) < 32:
            raise ValueError("SUB2API_ALERT_WEBHOOK_SECRET must contain at least 32 characters")
        return value

    # Server
    HOST: str = Field(default="0.0.0.0", description="Server host")
    PORT: int = Field(default=8000, description="Server port")

    # Database
    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://vanguard:change_me_in_production@postgres:5432/vanguard",
        description="Database connection URL"
    )
    DATABASE_POOL_SIZE: int = Field(default=10, description="Database pool size")
    DATABASE_MAX_OVERFLOW: int = Field(default=20, description="Database max overflow")

    # Redis
    REDIS_URL: str = Field(default="redis://localhost:6379/0", description="Redis connection URL")
    REDIS_PASSWORD: str | None = Field(default=None, description="Legacy fallback; prefer the password embedded in REDIS_URL")

    # Telegram
    TELEGRAM_API_ID: str | None = Field(default=None, description="Telegram API ID")
    TELEGRAM_API_HASH: str | None = Field(default=None, description="Telegram API hash")
    BOT_TOKEN: str | None = Field(default=None, description="Telegram bot token")
    TELEGRAM_SESSION_DIR: str = Field(default="./sessions", description="Telegram session files directory")
    TELEGRAM_SESSION_ENCRYPTION_KEY: str | None = Field(default=None, description="Encryption key for Telegram StringSession values")

    # QQ through NapCatQQ OneBot 11
    QQ_ONEBOT_ENABLED: bool = Field(default=False, description="Enable NapCatQQ OneBot 11")
    QQ_ONEBOT_ACCOUNT_ID: str | None = Field(default=None, description="QQ account number logged in to NapCat")
    QQ_ONEBOT_HTTP_URL: str = Field(
        default="http://napcat:3000",
        description="NapCat OneBot 11 HTTP API URL",
    )
    QQ_ONEBOT_WS_URL: str = Field(
        default="ws://napcat:3001",
        description="NapCat OneBot 11 WebSocket server URL",
    )
    QQ_ONEBOT_ACCESS_TOKEN: str | None = Field(
        default=None,
        description="Shared access token configured in NapCat OneBot network settings",
    )
    QQ_ONEBOT_REQUEST_TIMEOUT_SECONDS: float = Field(
        default=10.0,
        ge=2.0,
        le=60.0,
        description="NapCat OneBot HTTP timeout",
    )
    QQ_ONEBOT_MESSAGE_RETENTION_DAYS: int = Field(
        default=30,
        ge=1,
        le=365,
        description="QQ monitored message retention in days",
    )

    # Proxy Service
    PROXY_PROVIDER: str = Field(default="evomi", description="Proxy provider for promoter accounts: evomi/decodo")
    PROMOTER_PROXY_REQUIRED: bool = Field(default=True, description="Require proxy before promoter account connects")
    RISK_GUARD_FAIL_CLOSED: bool = Field(default=False, description="Block high-risk account actions when Redis risk budget is unavailable")

    # Evomi Proxy Service
    EVOMI_API_KEY: str | None = Field(default=None, description="Evomi API key for proxy services")
    EVOMI_PROXY_HOST: str | None = Field(default=None, description="Static Evomi proxy host")
    EVOMI_PROXY_PORT: int | None = Field(default=None, description="Static Evomi proxy port")
    EVOMI_PROXY_USERNAME: str | None = Field(default=None, description="Static Evomi proxy username")
    EVOMI_PROXY_PASSWORD: str | None = Field(default=None, description="Static Evomi proxy password")
    EVOMI_PRODUCT_CODE: str = Field(default="rp", description="Evomi product code for generated proxies")
    EVOMI_PROTOCOL: str = Field(default="http", description="Evomi proxy protocol")
    EVOMI_SESSION_TYPE: str = Field(default="sticky", description="Evomi session type")
    EVOMI_SESSION_LIFETIME_MINUTES: int = Field(default=30, description="Evomi sticky session lifetime in minutes")
    EVOMI_SESSION_NAMESPACE: str = Field(default="vanguard", description="Evomi sticky session namespace")
    EVOMI_ADBLOCK: bool = Field(default=False, description="Enable Evomi adblock where supported")
    EVOMI_COUNTRY_VERIFY_ENABLED: bool = Field(default=False, description="Verify proxy exit country before use")
    EVOMI_COUNTRY_VERIFY_ATTEMPTS: int = Field(default=5, description="Maximum country verification attempts")
    EVOMI_COUNTRY_VERIFY_TIMEOUT_SECONDS: int = Field(default=8, description="Proxy country verification timeout")
    EVOMI_COUNTRY_VERIFY_URL: str = Field(
        default="http://ip-api.com/json/?fields=status,countryCode,query",
        description="Proxy country verification endpoint",
    )

    # Decodo Proxy Service (legacy fallback)
    DECODO_API_KEY: str | None = Field(default=None, description="Decodo API key for proxy services")
    DECODO_SESSION_DURATION: int = Field(default=10, description="Default proxy session duration in minutes")

    # XBoard direct database fields are retained for compatibility only; Vanguard does not read them.
    XBOARD_DB_HOST: str = Field(default="localhost", description="Deprecated XBoard database host")
    XBOARD_DB_PORT: int = Field(default=3306, description="Deprecated XBoard database port")
    XBOARD_DB_NAME: str = Field(default="xboard", description="Deprecated XBoard database name")
    XBOARD_DB_USER: str = Field(default="root", description="Deprecated XBoard database user")
    XBOARD_DB_PASSWORD: str = Field(default="password", description="Deprecated XBoard database password")
    XBOARD_API_URL: str = Field(default="", description="Deprecated outbound XBoard API URL; signed HMAC integration is authoritative")
    XBOARD_API_KEY: str | None = Field(default=None, description="Deprecated XBoard API key; signed HMAC integration is authoritative")

    # Vanguard <-> XBoard API integration
    VANGUARD_INTEGRATION_ENABLED: bool = Field(default=True, description="Enable Vanguard XBoard integration")
    VANGUARD_APP_ID: str = Field(default="vanguard", description="Inbound XBoard app id")
    VANGUARD_SIGNING_SECRET: str = Field(default=PLACEHOLDER_VANGUARD_SIGNING_SECRET, description="Inbound signing secret")
    VANGUARD_TIMESTAMP_TOLERANCE: int = Field(default=300, description="Timestamp tolerance in seconds")
    VANGUARD_CALLBACK_ENABLED: bool = Field(default=True, description="Enable Vanguard callback endpoint")
    VANGUARD_CALLBACK_APP_ID: str = Field(default="xboard", description="Callback app id")
    VANGUARD_CALLBACK_SIGNING_SECRET: str = Field(default=PLACEHOLDER_VANGUARD_CALLBACK_SECRET, description="Callback signing secret")
    VANGUARD_CALLBACK_TIMEOUT: int = Field(default=5, description="Callback timeout in seconds")
    VANGUARD_CALLBACK_QUEUE: str = Field(default="vanguard_callback", description="Callback queue name")

    # Sub2API Admin integration for campaign redeem-code coupons
    SUB2API_ENABLED: bool = Field(default=False, description="Enable Sub2API redeem-code coupon generation")
    SUB2API_BASE_URL: str = Field(default="", description="Sub2API base URL")
    SUB2API_ADMIN_API_KEY: str | None = Field(default=None, description="Sub2API admin API key")
    SUB2API_TIMEOUT: int = Field(default=5, description="Sub2API request timeout in seconds")
    SUB2API_ALERT_WEBHOOK_SECRET: str | None = Field(
        default=None,
        description="Shared HMAC secret for inbound Sub2API alert and announcement webhooks",
    )
    SUB2API_ALERT_TIMESTAMP_TOLERANCE: int = Field(
        default=300,
        ge=30,
        le=3600,
        description="Maximum accepted Sub2API alert webhook clock skew in seconds",
    )
    SUB2API_ALERT_IDEMPOTENCY_TTL_SECONDS: int = Field(
        default=2592000,
        ge=3600,
        le=7776000,
        description="Retention for per-channel Sub2API alert idempotency keys",
    )

    # LLM/AI
    OPENAI_API_KEY: str | None = Field(default=None, description="OpenAI API Key")
    OPENAI_BASE_URL: str | None = Field(default=None, description="OpenAI-compatible API base URL")
    ANTHROPIC_API_KEY: str | None = Field(default=None, description="Anthropic API Key")
    LLM_PROVIDER: str = Field(default="openai", description="LLM provider: openai/anthropic/local")
    LLM_MODEL: str = Field(default="gpt-5.6-terra", description="Default LLM model")
    LLM_FAST_MODEL: str = Field(default="", description="Low-latency LLM model; falls back to LLM_MODEL when empty")

    # CORS
    CORS_ORIGINS: str = Field(
        default="http://localhost:3000,http://localhost:8000",
        description="Allowed CORS origins (comma-separated)"
    )

    @property
    def cors_origins_list(self) -> list[str]:
        """Parse CORS_ORIGINS string to list."""
        if isinstance(self.CORS_ORIGINS, str):
            return [origin.strip() for origin in self.CORS_ORIGINS.split(',') if origin.strip()]
        return self.CORS_ORIGINS if isinstance(self.CORS_ORIGINS, list) else []

    # Celery
    CELERY_BROKER_URL: str = Field(default="redis://localhost:6379/1", description="Celery broker URL")
    CELERY_RESULT_BACKEND: str = Field(default="redis://localhost:6379/2", description="Celery result backend")

    # Alert
    ALERT_CHAT_ID: str | None = Field(default=None, description="Telegram chat ID for alerts")
    TELEGRAM_ALERT_CHAT_ID: str | None = Field(default=None, description="Deprecated alias for ALERT_CHAT_ID")

    # Logging
    LOG_LEVEL: str = Field(default="INFO", description="Logging level")
    SENTRY_DSN: str | None = Field(default=None, description="Sentry DSN for error tracking")

    @property
    def is_production(self) -> bool:
        """Check if running in production environment."""
        return self.APP_ENV.lower() in {"production", "prod"}

    @property
    def is_development(self) -> bool:
        """Check if running in development environment."""
        return self.APP_ENV.lower() in {"development", "dev", "local"}

    @model_validator(mode="after")
    def validate_production_defaults(self):
        """Fail fast if production is started with development secrets."""
        if not self.LLM_FAST_MODEL.strip():
            self.LLM_FAST_MODEL = self.LLM_MODEL

        if not self.ALERT_CHAT_ID and self.TELEGRAM_ALERT_CHAT_ID:
            self.ALERT_CHAT_ID = self.TELEGRAM_ALERT_CHAT_ID

        if self.SECRET_KEY == DEFAULT_DEV_SECRET and self.JWT_SECRET != DEFAULT_DEV_SECRET:
            self.SECRET_KEY = self.JWT_SECRET
        elif self.JWT_SECRET == DEFAULT_DEV_SECRET and self.SECRET_KEY != DEFAULT_DEV_SECRET:
            self.JWT_SECRET = self.SECRET_KEY

        if self.QQ_ONEBOT_ENABLED:
            account_id = (self.QQ_ONEBOT_ACCOUNT_ID or "").strip()
            if not account_id or not account_id.isdigit():
                raise ValueError(
                    "QQ_ONEBOT_ACCOUNT_ID must be a numeric QQ account when QQ_ONEBOT_ENABLED=true"
                )
            if not self.QQ_ONEBOT_ACCESS_TOKEN or len(self.QQ_ONEBOT_ACCESS_TOKEN) < 32:
                raise ValueError(
                    "QQ_ONEBOT_ACCESS_TOKEN must contain at least 32 characters when QQ_ONEBOT_ENABLED=true"
                )

        if self.APP_ENV.lower() not in {"production", "prod"}:
            return self

        errors: list[str] = []
        if self.SECRET_KEY == DEFAULT_DEV_SECRET:
            errors.append("SECRET_KEY must be set explicitly in production")
        if self.JWT_SECRET == DEFAULT_DEV_SECRET:
            errors.append("JWT_SECRET must be set explicitly in production")
        if "change_me_in_production" in self.DATABASE_URL:
            errors.append("DATABASE_URL must not use the development password in production")
        if self.VANGUARD_INTEGRATION_ENABLED and self.VANGUARD_SIGNING_SECRET == PLACEHOLDER_VANGUARD_SIGNING_SECRET:
            errors.append("VANGUARD_SIGNING_SECRET must be set explicitly in production")
        if self.VANGUARD_CALLBACK_ENABLED and self.VANGUARD_CALLBACK_SIGNING_SECRET == PLACEHOLDER_VANGUARD_CALLBACK_SECRET:
            errors.append("VANGUARD_CALLBACK_SIGNING_SECRET must be set explicitly in production")
        if errors:
            raise ValueError("; ".join(errors))
        return self

    @property
    def effective_redis_password(self) -> str | None:
        """Use the URL credential first; REDIS_PASSWORD remains a legacy fallback."""
        try:
            return None if urlsplit(self.REDIS_URL).password is not None else self.REDIS_PASSWORD
        except ValueError:
            return self.REDIS_PASSWORD


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


# Global settings instance
settings = get_settings()
