"""Persistence models for owned-group account messaging policies and runs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.schema import conv

from app.core.database import Base
from app.modules.owned_group.messaging_contracts import (
    MessageExecutionStatus,
    MessageMode,
    default_promotion_config,
    default_trigger_config,
)

_JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")
_NULLABLE_JSON_DOCUMENT = JSON(none_as_null=True).with_variant(
    JSONB(none_as_null=True),
    "postgresql",
)
_TRIGGER_DEFAULT = '{"version":1,"scheduled":{"enabled":false,"timezone":"Asia/Shanghai","weekdays":[1,2,3,4,5,6,7],"times":[],"jitter_seconds":0,"content_category":"community"},"keyword":{"enabled":false,"trigger_ids":[],"reply_to_source":true,"content_category":"community"},"reply":{"enabled":false,"strategy":"directed","semantic_min_confidence":0.75,"context_messages":6,"content_category":"community"},"manual":{"enabled":true,"allowed_content_categories":["community","promotion"]},"dedupe_window_seconds":21600}'
_PROMOTION_DEFAULT = '{"mode":"off","default_template_id":null,"destination_url":null,"cta_text":null}'


class GroupAccountMessagePolicy(Base):
    """One immutable-account messaging configuration for one owned group."""

    __tablename__ = "group_account_message_policy"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owned_group_asset_id: Mapped[int] = mapped_column(
        ForeignKey("owned_group_assets.id", ondelete="CASCADE"), nullable=False
    )
    core_group_id: Mapped[int] = mapped_column(
        ForeignKey("group.id", ondelete="RESTRICT"), nullable=False
    )
    account_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_account.id", ondelete="RESTRICT"), nullable=False
    )
    mode: Mapped[str] = mapped_column(
        String(16),
        default=MessageMode.OFF.value,
        server_default=MessageMode.OFF.value,
        nullable=False,
    )
    default_template_id: Mapped[int | None] = mapped_column(
        ForeignKey("acquisition_message_template.id", ondelete="RESTRICT"), nullable=True
    )
    trigger_config: Mapped[dict[str, Any]] = mapped_column(
        _JSON_DOCUMENT,
        default=default_trigger_config,
        server_default=_TRIGGER_DEFAULT,
        nullable=False,
    )
    promotion_config: Mapped[dict[str, Any]] = mapped_column(
        _JSON_DOCUMENT,
        default=default_promotion_config,
        server_default=_PROMOTION_DEFAULT,
        nullable=False,
    )
    daily_limit: Mapped[int] = mapped_column(Integer, default=5, server_default="5", nullable=False)
    cooldown_seconds: Mapped[int] = mapped_column(
        Integer, default=3600, server_default="3600", nullable=False
    )
    allowed_topics: Mapped[list[str]] = mapped_column(
        _JSON_DOCUMENT, default=list, server_default="[]", nullable=False
    )
    require_review: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    asset = relationship("OwnedGroupAsset", lazy="joined")
    group = relationship("Group", lazy="joined")
    account = relationship("TelegramAccount", lazy="joined")
    default_template = relationship(
        "MessageTemplate", foreign_keys=[default_template_id], lazy="joined"
    )

    __table_args__ = (
        UniqueConstraint(
            "owned_group_asset_id",
            "account_id",
            name="uq_group_account_message_policy_asset_account",
        ),
        CheckConstraint("mode IN ('ai', 'template', 'off')", name="message_policy_mode"),
        CheckConstraint("daily_limit BETWEEN 0 AND 100", name="message_policy_daily_limit"),
        CheckConstraint(
            "cooldown_seconds BETWEEN 60 AND 86400",
            name="message_policy_cooldown_seconds",
        ),
        CheckConstraint("revision >= 1", name="message_policy_revision"),
        Index("idx_message_policy_group_enabled", "core_group_id", "enabled"),
        Index("idx_message_policy_account_enabled", "account_id", "enabled"),
    )


class GroupAccountMessageExecution(Base):
    """Frozen execution snapshot used by review and Telegram delivery workers."""

    __tablename__ = "group_account_message_execution"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    policy_id: Mapped[int] = mapped_column(
        ForeignKey("group_account_message_policy.id", ondelete="RESTRICT"), nullable=False
    )
    owned_group_asset_id: Mapped[int] = mapped_column(Integer, nullable=False)
    core_group_id: Mapped[int] = mapped_column(Integer, nullable=False)
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    account_id: Mapped[int] = mapped_column(Integer, nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(16), nullable=False)
    message_purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    content_category: Mapped[str] = mapped_column(String(16), nullable=False)
    mode_snapshot: Mapped[str] = mapped_column(String(16), nullable=False)
    policy_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    persona_revision_snapshot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    persona_source_snapshot: Mapped[str | None] = mapped_column(String(32), nullable=True)
    persona_snapshot: Mapped[dict[str, Any] | None] = mapped_column(
        _NULLABLE_JSON_DOCUMENT,
        nullable=True,
    )
    persona_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_template_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    prompt_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    governance_rules_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32),
        default=MessageExecutionStatus.QUEUED.value,
        server_default=MessageExecutionStatus.QUEUED.value,
        nullable=False,
    )
    source_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reply_to_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    keyword_trigger_id: Mapped[int | None] = mapped_column(
        ForeignKey("acquisition_keyword_trigger.id", ondelete="SET NULL"), nullable=True
    )
    template_id: Mapped[int | None] = mapped_column(
        ForeignKey("acquisition_message_template.id", ondelete="SET NULL"), nullable=True
    )
    topic: Mapped[str | None] = mapped_column(String(100), nullable=True)
    prompt_context: Mapped[dict[str, Any] | None] = mapped_column(
        _JSON_DOCUMENT, nullable=True
    )
    promotion_config_snapshot: Mapped[dict[str, Any] | None] = mapped_column(
        _JSON_DOCUMENT, nullable=True
    )
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lease_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    write_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    attempt_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    requested_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reviewer_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, server_default=text("CURRENT_TIMESTAMP"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    policy = relationship("GroupAccountMessagePolicy", lazy="joined")
    keyword_trigger = relationship("KeywordTrigger", lazy="joined")
    template = relationship("MessageTemplate", foreign_keys=[template_id], lazy="joined")

    __table_args__ = (
        CheckConstraint(
            "trigger_type IN ('scheduled', 'keyword', 'reply', 'manual')",
            name=conv("ck_group_account_message_execution_message_execution_trigger_ty"),
        ),
        CheckConstraint(
            "message_purpose IN ('community_ai', 'template')",
            name="message_execution_purpose",
        ),
        CheckConstraint(
            "content_category IN ('community', 'promotion')",
            name="message_execution_category",
        ),
        CheckConstraint(
            "mode_snapshot IN ('ai', 'template')",
            name="message_execution_mode",
        ),
        CheckConstraint(
            "persona_source_snapshot IS NULL OR persona_source_snapshot IN "
            "('configured', 'neutral_default', 'feature_disabled_default', 'legacy_default')",
            name="persona_source",
        ),
        CheckConstraint(
            "persona_revision_snapshot IS NULL OR persona_revision_snapshot >= 0",
            name="persona_revision",
        ),
        CheckConstraint(
            "mode_snapshot <> 'template' OR ("
            "persona_revision_snapshot IS NULL AND persona_source_snapshot IS NULL AND "
            "persona_snapshot IS NULL AND persona_hash IS NULL AND "
            "prompt_template_version IS NULL AND prompt_hash IS NULL AND "
            "governance_rules_hash IS NULL)",
            name="template_persona_null",
        ),
        CheckConstraint(
            "mode_snapshot <> 'ai' OR status NOT IN ('queued', 'generating') OR ("
            "persona_revision_snapshot IS NOT NULL AND persona_source_snapshot IS NOT NULL AND "
            "persona_snapshot IS NOT NULL AND persona_hash IS NOT NULL AND "
            "prompt_template_version IS NOT NULL)",
            name="ai_persona_required",
        ),
        CheckConstraint(
            "persona_source_snapshot IS NOT NULL OR ("
            "persona_revision_snapshot IS NULL AND persona_snapshot IS NULL AND "
            "persona_hash IS NULL AND prompt_template_version IS NULL)",
            name="persona_consistency",
        ),
        CheckConstraint(
            "persona_hash IS NULL OR persona_hash ~ '^[0-9a-f]{64}$'",
            name="persona_hash_pg",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "persona_hash IS NULL OR (length(persona_hash) = 64 AND "
            "lower(persona_hash) = persona_hash AND "
            "persona_hash NOT GLOB '*[^0-9a-f]*')",
            name="persona_hash_sqlite",
        ).ddl_if(dialect="sqlite"),
        CheckConstraint(
            "prompt_hash IS NULL OR prompt_hash ~ '^[0-9a-f]{64}$'",
            name="prompt_hash_pg",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "prompt_hash IS NULL OR (length(prompt_hash) = 64 AND "
            "lower(prompt_hash) = prompt_hash AND "
            "prompt_hash NOT GLOB '*[^0-9a-f]*')",
            name="prompt_hash_sqlite",
        ).ddl_if(dialect="sqlite"),
        CheckConstraint(
            "governance_rules_hash IS NULL OR "
            "governance_rules_hash ~ '^[0-9a-f]{64}$'",
            name="governance_hash_pg",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "governance_rules_hash IS NULL OR ("
            "length(governance_rules_hash) = 64 AND "
            "lower(governance_rules_hash) = governance_rules_hash AND "
            "governance_rules_hash NOT GLOB '*[^0-9a-f]*')",
            name="governance_hash_sqlite",
        ).ddl_if(dialect="sqlite"),
        CheckConstraint(
            "status IN ('queued', 'generating', 'pending_review', 'ready_to_send', "
            "'sending', 'sent', 'skipped', 'failed', 'rejected', 'expired', 'cancelled')",
            name="message_execution_status",
        ),
        CheckConstraint(
            "attempt_count BETWEEN 0 AND 3",
            name=conv("ck_group_account_message_execution_message_execution_attempt_co"),
        ),
        CheckConstraint("revision >= 1", name="message_execution_revision"),
        Index(
            "idx_message_execution_dispatch",
            "status",
            "scheduled_at",
            "next_retry_at",
        ),
        Index("idx_message_execution_policy_created", "policy_id", "created_at"),
        Index("idx_message_execution_group_sent", "core_group_id", "sent_at"),
        Index(
            "idx_message_execution_group_category_sent",
            "core_group_id",
            "content_category",
            "sent_at",
        ),
        Index("idx_message_execution_account_sent", "account_id", "sent_at"),
        Index(
            "idx_message_execution_group_hash_created",
            "core_group_id",
            "content_hash",
            "created_at",
        ),
        Index(
            "idx_message_execution_asset_source",
            "owned_group_asset_id",
            "source_message_id",
        ),
    )
