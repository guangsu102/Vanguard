"""Database models for the independent self-owned group module."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
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


class OwnedGroupAsset(Base):
    __tablename__ = "owned_group_assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    internal_name: Mapped[str] = mapped_column(String(120), nullable=False)
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    about: Mapped[str | None] = mapped_column(Text, nullable=True)
    visibility: Mapped[str] = mapped_column(String(16), default="public", nullable=False)
    telegram_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    public_link: Mapped[str | None] = mapped_column(String(255), nullable=True)
    owner_account_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_account.id", ondelete="RESTRICT"), nullable=False
    )
    invite_mode: Mapped[str] = mapped_column(String(32), default="direct_invite", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    member_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    owner_account = relationship("TelegramAccount", foreign_keys=[owner_account_id], lazy="joined")
    operations = relationship(
        "OwnedGroupOperation", back_populates="group_asset", cascade="all, delete-orphan"
    )
    memberships = relationship(
        "OwnedGroupMembership", back_populates="group_asset", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_owned_group_assets_status", "status"),
        Index("idx_owned_group_assets_owner", "owner_account_id"),
        Index("idx_owned_group_assets_visibility", "visibility"),
    )


class OwnedGroupOperation(Base):
    __tablename__ = "owned_group_operations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_asset_id: Mapped[int] = mapped_column(
        ForeignKey("owned_group_assets.id", ondelete="CASCADE"), nullable=False
    )
    operation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    selection_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    selection_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    config_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    config_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    planned_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    stop_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    schedule_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    group_asset = relationship("OwnedGroupAsset", back_populates="operations")
    items = relationship(
        "OwnedGroupOperationItem", back_populates="operation", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_owned_group_operations_group_status", "group_asset_id", "status"),
        Index("idx_owned_group_operations_schedule", "status", "schedule_at"),
    )


class OwnedGroupOperationItem(Base):
    __tablename__ = "owned_group_operation_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operation_id: Mapped[int] = mapped_column(
        ForeignKey("owned_group_operations.id", ondelete="CASCADE"), nullable=False
    )
    resource_type: Mapped[str] = mapped_column(String(16), nullable=False)
    resource_id: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    invited_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    joined_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    admin_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    admin_permissions: Mapped[str | None] = mapped_column(Text, nullable=True)
    admin_title: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    operation = relationship("OwnedGroupOperation", back_populates="items")

    __table_args__ = (
        UniqueConstraint(
            "operation_id", "resource_type", "resource_id", name="uq_owned_group_operation_resource"
        ),
        Index("idx_owned_group_items_status_retry", "status", "next_retry_at"),
        Index("idx_owned_group_items_resource", "resource_type", "resource_id"),
    )
