"""
Keyword Models

Database models for keyword management.
"""

from datetime import datetime
from enum import Enum

from sqlalchemy import (
    DateTime,
    Enum as SQLEnum,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class KeywordType(str, Enum):
    """Keyword type enumeration."""
    DEMAND = "demand"      # 需求类
    INQUIRY = "inquiry"    # 咨询类
    PRICE = "price"        # 价格类
    COMPETITOR = "competitor"  # 竞品类


class KeywordStatus(str, Enum):
    """Keyword status enumeration."""
    PENDING = "pending"     # 待处理
    APPROVED = "approved"   # 已添加
    EXECUTING = "executing" # 执行中
    COMPLETED = "completed" # 已完成
    DISCARDED = "discarded" # 废弃


class MatchMode(str, Enum):
    """Keyword match mode."""
    EXACT = "exact"       # 精确匹配
    FUZZY = "fuzzy"      # 模糊匹配
    REGEX = "regex"       # 正则匹配


class Keyword(Base):
    """Keyword model for message matching."""
    
    __tablename__ = "keyword"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    text: Mapped[str] = mapped_column(String(255), nullable=False, comment="关键词")
    type: Mapped[KeywordType] = mapped_column(
        SQLEnum(KeywordType),
        nullable=False,
        comment="关键词类型"
    )
    status: Mapped[KeywordStatus] = mapped_column(
        SQLEnum(KeywordStatus),
        default=KeywordStatus.PENDING,
        nullable=False,
        comment="状态"
    )
    match_mode: Mapped[MatchMode] = mapped_column(
        SQLEnum(MatchMode),
        default=MatchMode.FUZZY,
        nullable=False,
        comment="匹配模式"
    )
    trigger_count: Mapped[int] = mapped_column(Integer, default=0, comment="触发次数")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False
    )
    
    __table_args__ = (
        Index("idx_type_status", "type", "status"),
        Index("idx_trigger_count", "trigger_count"),
    )
