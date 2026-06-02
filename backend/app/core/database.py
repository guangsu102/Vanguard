"""
Database Connection and Session Management

SQLAlchemy 2.0 async support with connection pooling.
"""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


# Naming convention for database constraints
convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base class for all database models."""
    
    metadata = MetaData(naming_convention=convention)


# Global engine and session factory
engine: AsyncEngine | None = None
async_session_factory: async_sessionmaker[AsyncSession] | None = None


async def init_db(create_tables: bool = True) -> None:
    """Initialize database connection and optionally create tables."""
    global engine, async_session_factory
    
    engine = create_async_engine(
        settings.DATABASE_URL,
        pool_size=settings.DATABASE_POOL_SIZE,
        max_overflow=settings.DATABASE_MAX_OVERFLOW,
        pool_pre_ping=True,
        pool_recycle=3600,
        echo=settings.DEBUG,
    )
    
    async_session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )
    
    # Import models to register them with Base
    from app.core.account.models import TelegramAccount, AccountOperationConfig, GuardianBotProfile
    from app.core.group.models import Group, GroupAccountMembership, GroupLevelConfig
    from app.core.keyword.models import Keyword
    from app.core.user.models import User
    from app.core.campaign.models import Campaign, CampaignTracking
    from app.core.worker_status import TelegramWorkerStatus
    from app.api.broadcasts import BroadcastRecord
    from app.modules.guardian.models import (
        Violation,
        ModerationRule,
        Whitelist,
        GroupVerificationConfig,
        VerificationSession,
        KeywordSuggestion,
        ModerationSensitiveKeyword,
        ManagedGroupBinding,
        GroupModerationPolicy,
        GroupPunishmentPolicy,
        CouponDistribution,
    )
    from app.modules.acquisition.models import (
        GroupSearchRecord,
        GroupSearchKeyword,
        AutoJoinAttempt,
        AcquisitionTracking,
        AcquisitionMessage,
        KeywordTrigger,
        TriggerRecord,
        MessageTemplate,
        GuideFlow,
        ConversationContext,
        AdCreative,
        AdCampaign,
        AccountAdBinding,
        AdDeliveryLog,
    )
    from app.integrations.xboard.models import XBoardEvent, XBoardCallback
    
    if create_tables:
        # Create tables (use curated SQL migrations for production schema changes)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)


async def close_db() -> None:
    """Close database connection."""
    global engine, async_session_factory
    if engine:
        await engine.dispose()
        engine = None
    async_session_factory = None


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Get database session for dependency injection."""
    if async_session_factory is None:
        raise RuntimeError("Database not initialized")
    
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


@asynccontextmanager
async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Get database session as context manager."""
    if async_session_factory is None:
        raise RuntimeError("Database not initialized")
    
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
