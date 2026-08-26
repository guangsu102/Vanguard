"""Persistent Telegram private chat inbox models."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class PrivateChatConversation(Base):
    """One Telegram peer talking to one Vanguard account."""

    __tablename__ = "telegram_private_conversation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_account.id", ondelete="CASCADE"), nullable=False
    )
    peer_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    peer_username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    peer_display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="open", nullable=False)
    handling_mode: Mapped[str] = mapped_column(String(20), default="auto", nullable=False)
    assigned_admin_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unread_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    last_message_preview: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_message_direction: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_outbound_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    account = relationship("TelegramAccount", lazy="joined")
    messages = relationship(
        "PrivateChatMessage",
        back_populates="conversation",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "peer_telegram_id",
            name="uq_private_conversation_account_peer",
        ),
        Index("idx_private_conversation_last_message", "status", "last_message_at"),
        Index("idx_private_conversation_account_last", "account_id", "last_message_at"),
        Index("idx_private_conversation_unread", "unread_count", "last_message_at"),
    )


class PrivateChatMessage(Base):
    """An inbound or outbound message in a private conversation."""

    __tablename__ = "telegram_private_message"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_private_conversation.id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_account.id", ondelete="CASCADE"), nullable=False
    )
    peer_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reply_to_telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    direction: Mapped[str] = mapped_column(String(20), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    message_type: Mapped[str] = mapped_column(String(30), default="text", nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(20), nullable=False)
    operator_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    client_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    conversation = relationship("PrivateChatConversation", back_populates="messages")

    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "peer_telegram_id",
            "telegram_message_id",
            name="uq_private_message_account_peer_telegram_id",
        ),
        UniqueConstraint("client_request_id", name="uq_private_message_client_request"),
        Index("idx_private_message_conversation_time", "conversation_id", "occurred_at"),
        Index("idx_private_message_outbox", "direction", "status", "next_attempt_at"),
    )
