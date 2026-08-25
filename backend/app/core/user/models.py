"""
User Models

Database models for user management and state tracking.
"""

from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum as SQLEnum,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class UserState(str, Enum):
    """User lifecycle state."""
    NEW = "new"           # 新注册用户
    PENDING = "pending"   # 待转化（试用期）
    ACTIVE = "active"     # 活跃付费用户
    SILENT = "silent"     # 沉默用户
    CHURNED = "churned"   # 流失用户
    BLOCKED = "blocked"   # 被拉黑用户


class User(Base):
    """User model for tracking user lifecycle."""
    
    __tablename__ = "user"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, comment="Telegram用户ID")
    xboard_user_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="XBoard用户ID")
    username: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, comment="用户名")
    
    state: Mapped[UserState] = mapped_column(
        SQLEnum(UserState),
        default=UserState.NEW,
        nullable=False,
        comment="用户状态"
    )
    
    # Warning and mute tracking
    warning_count: Mapped[int] = mapped_column(Integer, default=0, comment="警告次数")
    muted_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="禁言截止时间")
    
    # Trial tracking
    trial_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="试用开始时间")
    trial_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, comment="试用过期时间")
    
    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False
    )
    
    __table_args__ = (
        Index("idx_state", "state"),
    )
