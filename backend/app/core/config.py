"""
Vanguard Configuration Module

Application configuration using Pydantic Settings with environment variable support.
"""

from functools import lru_cache
from typing import Optional, Union

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    SECRET_KEY: str = Field(default="dev-secret-key-change-in-production", description="Secret key for JWT")
    JWT_SECRET: str = Field(default="dev-secret-key-change-in-production", description="JWT secret key")
    JWT_ALGORITHM: str = Field(default="HS256", description="JWT algorithm")
    JWT_EXPIRATION_HOURS: int = Field(default=24, description="JWT expiration in hours")
    
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
    REDIS_PASSWORD: Optional[str] = Field(default=None, description="Redis password")
    
    # Telegram
    TELEGRAM_API_ID: Optional[str] = Field(default=None, description="Telegram API ID")
    TELEGRAM_API_HASH: Optional[str] = Field(default=None, description="Telegram API hash")
    BOT_TOKEN: Optional[str] = Field(default=None, description="Telegram bot token")
    TELEGRAM_SESSION_DIR: str = Field(default="./sessions", description="Telegram session files directory")
    
    # Proxy Service
    PROXY_PROVIDER: str = Field(default="evomi", description="Proxy provider for promoter accounts: evomi/decodo")
    PROMOTER_PROXY_REQUIRED: bool = Field(default=True, description="Require proxy before promoter account connects")

    # Evomi Proxy Service
    EVOMI_API_KEY: Optional[str] = Field(default=None, description="Evomi API key for proxy services")
    EVOMI_PRODUCT_CODE: str = Field(default="rp", description="Evomi product code for generated proxies")
    EVOMI_PROTOCOL: str = Field(default="http", description="Evomi proxy protocol")
    EVOMI_SESSION_TYPE: str = Field(default="sticky", description="Evomi session type")
    EVOMI_SESSION_LIFETIME_MINUTES: int = Field(default=30, description="Evomi sticky session lifetime in minutes")
    EVOMI_SESSION_NAMESPACE: str = Field(default="vanguard", description="Evomi sticky session namespace")
    EVOMI_ADBLOCK: bool = Field(default=False, description="Enable Evomi adblock where supported")

    # Decodo Proxy Service (legacy fallback)
    DECODO_API_KEY: Optional[str] = Field(default=None, description="Decodo API key for proxy services")
    DECODO_SESSION_DURATION: int = Field(default=10, description="Default proxy session duration in minutes")
    
    # XBoard Database (for direct integration)
    XBOARD_DB_HOST: str = Field(default="localhost", description="XBoard database host")
    XBOARD_DB_PORT: int = Field(default=3306, description="XBoard database port")
    XBOARD_DB_NAME: str = Field(default="xboard", description="XBoard database name")
    XBOARD_DB_USER: str = Field(default="root", description="XBoard database user")
    XBOARD_DB_PASSWORD: str = Field(default="password", description="XBoard database password")
    XBOARD_API_URL: str = Field(default="", description="XBoard API base URL")
    XBOARD_API_KEY: Optional[str] = Field(default=None, description="XBoard API key")

    # Vanguard <-> XBoard API integration
    VANGUARD_INTEGRATION_ENABLED: bool = Field(default=True, description="Enable Vanguard XBoard integration")
    VANGUARD_APP_ID: str = Field(default="vanguard", description="Inbound XBoard app id")
    VANGUARD_SIGNING_SECRET: str = Field(default="replace-with-shared-secret", description="Inbound signing secret")
    VANGUARD_TIMESTAMP_TOLERANCE: int = Field(default=300, description="Timestamp tolerance in seconds")
    VANGUARD_CALLBACK_ENABLED: bool = Field(default=True, description="Enable Vanguard callback endpoint")
    VANGUARD_CALLBACK_APP_ID: str = Field(default="xboard", description="Callback app id")
    VANGUARD_CALLBACK_SIGNING_SECRET: str = Field(default="replace-with-callback-secret", description="Callback signing secret")
    VANGUARD_CALLBACK_TIMEOUT: int = Field(default=5, description="Callback timeout in seconds")
    VANGUARD_CALLBACK_QUEUE: str = Field(default="vanguard_callback", description="Callback queue name")

    # LLM/AI
    OPENAI_API_KEY: Optional[str] = Field(default=None, description="OpenAI API Key")
    ANTHROPIC_API_KEY: Optional[str] = Field(default=None, description="Anthropic API Key")
    LLM_PROVIDER: str = Field(default="openai", description="LLM provider: openai/anthropic/local")
    LLM_MODEL: str = Field(default="gpt-4o-mini", description="Default LLM model")
    
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
    ALERT_CHAT_ID: Optional[str] = Field(default=None, description="Telegram chat ID for alerts")
    
    # Logging
    LOG_LEVEL: str = Field(default="INFO", description="Logging level")
    SENTRY_DSN: Optional[str] = Field(default=None, description="Sentry DSN for error tracking")
    
    @property
    def is_production(self) -> bool:
        """Check if running in production environment."""
        return self.APP_ENV == "production"
    
    @property
    def is_development(self) -> bool:
        """Check if running in development environment."""
        return self.APP_ENV == "development"


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


# Global settings instance
settings = get_settings()
