"""PostgreSQL 数据库连接与模型"""
from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncGenerator
from uuid import uuid4

from sqlalchemy import (
    Column, String, Integer, BigInteger, Boolean, DateTime,
    Text, Enum as SQLEnum, Index
)
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import declarative_base
from sqlalchemy.dialects.postgresql import UUID
from loguru import logger
import enum


Base = declarative_base()


class ViolationType(str, enum.Enum):
    COMPETITOR_AD = "competitor_ad"
    SPAM = "spam"
    OTHER = "other"


class BotMatrixDatabase:
    """Bot 矩阵数据库管理器"""

    def __init__(self, dsn: str, pool_size: int = 10, max_overflow: int = 20):
        self.dsn = dsn
        self.engine = create_async_engine(
            dsn,
            pool_size=pool_size,
            max_overflow=max_overflow,
            echo=False
        )
        self.async_session = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False
        )

    async def init_db(self):
        """初始化数据库表"""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("数据库表初始化完成")

    async def close(self):
        """关闭数据库连接"""
        await self.engine.dispose()
        logger.info("数据库连接已关闭")

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession, None]:
        """获取数据库会话"""
        async with self.async_session() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise


# ============ 数据模型 ============

class TGUser(Base):
    """Telegram 用户表"""
    __tablename__ = "tg_users"

    id = Column(BigInteger, primary_key=True)
    tg_uid = Column(BigInteger, unique=True, nullable=False, index=True)
    username = Column(String(255), nullable=True)
    first_name = Column(String(255), nullable=True)
    last_name = Column(String(255), nullable=True)
    xboard_user_id = Column(BigInteger, nullable=True)
    is_bound = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index("idx_tg_users_tg_uid", "tg_uid"),
        Index("idx_tg_users_xboard_user_id", "xboard_user_id"),
    )


class TrialAccount(Base):
    """试用账号记录表"""
    __tablename__ = "trial_accounts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    tg_uid = Column(BigInteger, nullable=False, index=True)
    xboard_user_id = Column(BigInteger, nullable=False)
    validity_hours = Column(Integer, nullable=False)
    traffic_gb = Column(Integer, nullable=False)
    status = Column(String(20), default="active")  # active, expired, used
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)

    __table_args__ = (
        Index("idx_trial_accounts_tg_uid_created", "tg_uid", "created_at"),
    )


class CheckinRecord(Base):
    """签到记录表"""
    __tablename__ = "checkin_records"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    tg_uid = Column(BigInteger, nullable=False, index=True)
    bonus_mb = Column(Integer, nullable=False)
    is_special = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_checkin_records_tg_uid_created", "tg_uid", "created_at"),
    )


class AffiliatePoster(Base):
    """推广海报记录表"""
    __tablename__ = "affiliate_posters"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    tg_uid = Column(BigInteger, nullable=False, index=True)
    poster_path = Column(String(500), nullable=False)
    aff_link = Column(String(500), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_affiliate_posters_tg_uid", "tg_uid"),
    )


class ViolationRecord(Base):
    """违规记录表"""
    __tablename__ = "violation_records"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    tg_uid = Column(BigInteger, nullable=False, index=True)
    chat_id = Column(BigInteger, nullable=False)
    keyword = Column(String(255), nullable=False)
    violation_type = Column(SQLEnum(ViolationType), default=ViolationType.COMPETITOR_AD)
    message_preview = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class BanRecord(Base):
    """封禁记录表"""
    __tablename__ = "ban_records"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    tg_uid = Column(BigInteger, nullable=False, index=True)
    chat_id = Column(BigInteger, nullable=False)
    reason = Column(Text, nullable=True)
    banned_at = Column(DateTime, default=datetime.utcnow)
    unbanned_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_ban_records_tg_uid_chat", "tg_uid", "chat_id"),
    )


# ============ 数据库操作类 ============

class Database:
    """简化版的数据库操作封装"""

    def __init__(self, dsn: str):
        self._db = BotMatrixDatabase(dsn)

    async def init(self):
        await self._db.init_db()

    async def close(self):
        await self._db.close()

    async def record_checkin(self, user_id: int, bonus_mb: int, is_special: bool = False):
        """记录签到"""
        async with self._db.session() as session:
            record = CheckinRecord(
                tg_uid=user_id,
                bonus_mb=bonus_mb,
                is_special=is_special
            )
            session.add(record)

    async def record_ban(self, user_id: int, chat_id: int, reason: str = None):
        """记录封禁"""
        async with self._db.session() as session:
            record = BanRecord(
                tg_uid=user_id,
                chat_id=chat_id,
                reason=reason
            )
            session.add(record)

    async def record_poster(self, user_id: int, poster_path: str, aff_link: str):
        """记录海报生成"""
        async with self._db.session() as session:
            record = AffiliatePoster(
                tg_uid=user_id,
                poster_path=poster_path,
                aff_link=aff_link
            )
            session.add(record)

    async def create_trial_account(
        self,
        tg_uid: int,
        xboard_user_id: int,
        validity_hours: int,
        traffic_gb: int,
        expires_at: datetime
    ):
        """创建试用账号记录"""
        async with self._db.session() as session:
            record = TrialAccount(
                tg_uid=tg_uid,
                xboard_user_id=xboard_user_id,
                validity_hours=validity_hours,
                traffic_gb=traffic_gb,
                expires_at=expires_at
            )
            session.add(record)

    async def get_or_create_user(self, tg_uid: int, **kwargs):
        """获取或创建用户"""
        async with self._db.session() as session:
            from sqlalchemy import select
            stmt = select(TGUser).where(TGUser.tg_uid == tg_uid)
            result = await session.execute(stmt)
            user = result.scalar_one_or_none()

            if not user:
                user = TGUser(tg_uid=tg_uid, **kwargs)
                session.add(user)

            return user
