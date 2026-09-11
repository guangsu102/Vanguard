"""Supplemental self-owned group models loaded with the domain package."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def _utc_now() -> datetime:
    return datetime.now(UTC)


class OwnedGroupMembership(Base):
    __tablename__ = "owned_group_memberships"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_asset_id: Mapped[int] = mapped_column(
        ForeignKey("owned_group_assets.id", ondelete="CASCADE"), nullable=False
    )
    resource_type: Mapped[str] = mapped_column(String(16), nullable=False)
    resource_id: Mapped[int] = mapped_column(Integer, nullable=False)
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="member_verified", nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    admin_title: Mapped[str | None] = mapped_column(String(64), nullable=True)
    permissions_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    joined_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    group_asset = relationship("OwnedGroupAsset", back_populates="memberships")

    __table_args__ = (
        UniqueConstraint(
            "group_asset_id",
            "resource_type",
            "resource_id",
            name="uq_owned_group_membership_resource",
        ),
        Index("idx_owned_group_memberships_resource", "resource_type", "resource_id"),
        Index("idx_owned_group_memberships_status", "status"),
    )


class OwnedGroupMemberObservation(Base):
    """Guardian-observed membership facts for one self-owned group asset."""

    __tablename__ = "owned_group_member_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_asset_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("owned_group_assets.id", ondelete="CASCADE"),
        nullable=False,
    )
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # This value must always come from Telegram's JSON boolean. In particular,
    # there is deliberately no default that could guess a missing identity type.
    is_bot: Mapped[bool] = mapped_column(Boolean, nullable=False)
    username_snapshot: Mapped[str | None] = mapped_column(String(120), nullable=True)
    display_name_snapshot: Mapped[str | None] = mapped_column(String(200), nullable=True)
    presence_status: Mapped[str] = mapped_column(
        String(16), default="present", server_default="present", nullable=False
    )
    last_event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_bot_account_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("telegram_account.id", ondelete="SET NULL"),
        nullable=True,
    )
    last_update_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    first_observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    joined_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    left_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        onupdate=_utc_now,
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "group_asset_id",
            "telegram_user_id",
            name="uq_owned_group_member_observations_asset_user",
        ),
        CheckConstraint(
            "presence_status IN ('present', 'left')",
            name="presence_status",
        ),
        CheckConstraint(
            "last_event_type IN ('message', 'new_chat_member', 'left_chat_member')",
            name="last_event_type",
        ),
        Index(
            "idx_owned_group_member_observations_presence",
            "group_asset_id",
            "presence_status",
            "last_observed_at",
        ),
        Index(
            "idx_owned_group_member_observations_last_observed",
            "group_asset_id",
            "last_observed_at",
        ),
        Index(
            "idx_owned_group_member_observations_telegram_user",
            "telegram_user_id",
        ),
        Index(
            "idx_owned_group_member_observations_source_bot",
            "source_bot_account_id",
        ),
    )


class OwnedGroupAdminAssignment(Base):
    __tablename__ = "owned_group_admin_assignments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_asset_id: Mapped[int] = mapped_column(
        ForeignKey("owned_group_assets.id", ondelete="CASCADE"), nullable=False
    )
    resource_type: Mapped[str] = mapped_column(String(16), nullable=False)
    resource_id: Mapped[int] = mapped_column(Integer, nullable=False)
    permissions_snapshot: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    admin_title: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "group_asset_id", "resource_type", "resource_id", name="uq_owned_group_admin_resource"
        ),
        Index("idx_owned_group_admin_status", "status"),
    )


class OwnedGroupInviteLink(Base):
    __tablename__ = "owned_group_invite_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_asset_id: Mapped[int] = mapped_column(
        ForeignKey("owned_group_assets.id", ondelete="CASCADE"), nullable=False
    )
    link_type: Mapped[str] = mapped_column(String(32), nullable=False)
    link_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_owned_group_invite_links_active", "group_asset_id", "is_active"),
        # The adapter/API also serialize rotations with an asset row lock.  This
        # partial unique index is the final database guard against two active
        # bearer links being advertised after a concurrent/retried request.
        Index(
            "uq_owned_group_invite_links_one_active",
            "group_asset_id",
            unique=True,
            postgresql_where=text("is_active = true"),
            sqlite_where=text("is_active = 1"),
        ),
    )


class OwnedBotProfile(Base):
    """Bot registered for use by the self-owned group module."""

    __tablename__ = "owned_bot_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_account_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_account.id", ondelete="RESTRICT"), nullable=False
    )
    account_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_account.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    bot_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    bot_username: Mapped[str | None] = mapped_column(String(120), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    token_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending_verification", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    owner_account = relationship("TelegramAccount", foreign_keys=[owner_account_id], lazy="joined")
    account = relationship("TelegramAccount", foreign_keys=[account_id], lazy="joined")

    __table_args__ = (
        Index("idx_owned_bot_profiles_owner", "owner_account_id"),
        Index("idx_owned_bot_profiles_status", "status"),
    )


class OwnedGroupAuditEvent(Base):
    __tablename__ = "owned_group_audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    group_asset_id: Mapped[int | None] = mapped_column(
        ForeignKey("owned_group_assets.id", ondelete="SET NULL"), nullable=True
    )
    operation_id: Mapped[int | None] = mapped_column(
        ForeignKey("owned_group_operations.id", ondelete="SET NULL"), nullable=True
    )
    operation_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("owned_group_operation_items.id", ondelete="SET NULL"), nullable=True
    )
    resource_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resource_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    actor_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    before_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[str] = mapped_column(String(32), default="success", nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("idx_owned_group_audit_group_time", "group_asset_id", "created_at"),
        Index("idx_owned_group_audit_operation_time", "operation_id", "created_at"),
        Index("idx_owned_group_audit_resource_time", "resource_type", "resource_id", "created_at"),
    )
