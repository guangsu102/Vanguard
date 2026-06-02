"""
Message Models

Database models for message routing and handling.
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
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class MessageType(str, Enum):
    """Telegram message type."""
    GROUP_TEXT = "group_text"
    GROUP_PHOTO = "group_photo"
    GROUP_VIDEO = "group_video"
    GROUP_AUDIO = "group_audio"
    GROUP_DOCUMENT = "group_document"
    PRIVATE_TEXT = "private_text"
    CALLBACK_QUERY = "callback_query"
    COMMAND = "command"


class TelegramMessage(Base):
    """Telegram message record model."""
    
    __tablename__ = "telegram_message"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="消息ID")
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="群组ID")
    sender_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="发送者ID")
    sender_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, comment="发送者名称")
    
    message_type: Mapped[MessageType] = mapped_column(
        SQLEnum(MessageType),
        nullable=False,
        comment="消息类型"
    )
    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="消息内容")
    
    # Metadata
    reply_to_message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    
    __table_args__ = (
        Index("idx_chat_timestamp", "chat_id", "timestamp"),
        Index("idx_sender", "sender_id"),
    )
