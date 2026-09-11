"""Add scoped owned-group AI/template messaging.

Revision ID: 034_owned_group_messaging
Revises: 033_owned_group_governance
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "034_owned_group_messaging"
down_revision = "033_owned_group_governance"
branch_labels = None
depends_on = None


_TRIGGER_DEFAULT = (
    '{"version":1,"scheduled":{"enabled":false,"timezone":"Asia/Shanghai",'
    '"weekdays":[1,2,3,4,5,6,7],"times":[],"jitter_seconds":0,'
    '"content_category":"community"},"keyword":{"enabled":false,"trigger_ids":[],'
    '"reply_to_source":true,"content_category":"community"},"reply":{"enabled":false,'
    '"strategy":"directed","semantic_min_confidence":0.75,"context_messages":6,'
    '"content_category":"community"},"manual":{"enabled":true,'
    '"allowed_content_categories":["community","promotion"]},'
    '"dedupe_window_seconds":21600}'
)
_PROMOTION_DEFAULT = (
    '{"mode":"off","default_template_id":null,"destination_url":null,"cta_text":null}'
)
_JSON_DOCUMENT = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "group_account_message_policy",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("owned_group_asset_id", sa.Integer(), nullable=False),
        sa.Column("core_group_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(length=16), server_default="off", nullable=False),
        sa.Column("default_template_id", sa.Integer(), nullable=True),
        sa.Column(
            "trigger_config",
            _JSON_DOCUMENT,
            server_default=_TRIGGER_DEFAULT,
            nullable=False,
        ),
        sa.Column(
            "promotion_config",
            _JSON_DOCUMENT,
            server_default=_PROMOTION_DEFAULT,
            nullable=False,
        ),
        sa.Column("daily_limit", sa.Integer(), server_default="5", nullable=False),
        sa.Column("cooldown_seconds", sa.Integer(), server_default="3600", nullable=False),
        sa.Column("allowed_topics", _JSON_DOCUMENT, server_default="[]", nullable=False),
        sa.Column("require_review", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "mode IN ('ai', 'template', 'off')",
            name=op.f("ck_group_account_message_policy_message_policy_mode"),
        ),
        sa.CheckConstraint(
            "daily_limit BETWEEN 0 AND 100",
            name=op.f("ck_group_account_message_policy_message_policy_daily_limit"),
        ),
        sa.CheckConstraint(
            "cooldown_seconds BETWEEN 60 AND 86400",
            name=op.f("ck_group_account_message_policy_message_policy_cooldown_seconds"),
        ),
        sa.CheckConstraint(
            "revision >= 1",
            name=op.f("ck_group_account_message_policy_message_policy_revision"),
        ),
        sa.ForeignKeyConstraint(
            ["owned_group_asset_id"],
            ["owned_group_assets.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["core_group_id"], ["group.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["account_id"], ["telegram_account.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["default_template_id"],
            ["acquisition_message_template.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owned_group_asset_id",
            "account_id",
            name=op.f("uq_group_account_message_policy_asset_account"),
        ),
    )
    op.create_index(
        "idx_message_policy_group_enabled",
        "group_account_message_policy",
        ["core_group_id", "enabled"],
    )
    op.create_index(
        "idx_message_policy_account_enabled",
        "group_account_message_policy",
        ["account_id", "enabled"],
    )

    op.create_table(
        "group_account_message_execution",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("policy_id", sa.Integer(), nullable=False),
        sa.Column("owned_group_asset_id", sa.Integer(), nullable=False),
        sa.Column("core_group_id", sa.Integer(), nullable=False),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("trigger_type", sa.String(length=16), nullable=False),
        sa.Column("message_purpose", sa.String(length=32), nullable=False),
        sa.Column("content_category", sa.String(length=16), nullable=False),
        sa.Column("mode_snapshot", sa.String(length=16), nullable=False),
        sa.Column("policy_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="queued", nullable=False),
        sa.Column("source_message_id", sa.BigInteger(), nullable=True),
        sa.Column("reply_to_message_id", sa.BigInteger(), nullable=True),
        sa.Column("keyword_trigger_id", sa.Integer(), nullable=True),
        sa.Column("template_id", sa.Integer(), nullable=True),
        sa.Column("topic", sa.String(length=100), nullable=True),
        sa.Column("prompt_context", _JSON_DOCUMENT, nullable=True),
        sa.Column("promotion_config_snapshot", _JSON_DOCUMENT, nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("correlation_id", sa.String(length=128), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(), nullable=True),
        sa.Column("lease_id", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("write_started_at", sa.DateTime(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("requested_by", sa.Integer(), nullable=True),
        sa.Column("reviewer_id", sa.Integer(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "trigger_type IN ('scheduled', 'keyword', 'reply', 'manual')",
            name=op.f("ck_group_account_message_execution_message_execution_trigger_ty"),
        ),
        sa.CheckConstraint(
            "message_purpose IN ('community_ai', 'template')",
            name=op.f("ck_group_account_message_execution_message_execution_purpose"),
        ),
        sa.CheckConstraint(
            "content_category IN ('community', 'promotion')",
            name=op.f("ck_group_account_message_execution_message_execution_category"),
        ),
        sa.CheckConstraint(
            "mode_snapshot IN ('ai', 'template')",
            name=op.f("ck_group_account_message_execution_message_execution_mode"),
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'generating', 'pending_review', 'ready_to_send', "
            "'sending', 'sent', 'skipped', 'failed', 'rejected', 'expired', 'cancelled')",
            name=op.f("ck_group_account_message_execution_message_execution_status"),
        ),
        sa.CheckConstraint(
            "attempt_count BETWEEN 0 AND 3",
            name=op.f("ck_group_account_message_execution_message_execution_attempt_co"),
        ),
        sa.CheckConstraint(
            "revision >= 1",
            name=op.f("ck_group_account_message_execution_message_execution_revision"),
        ),
        sa.ForeignKeyConstraint(
            ["policy_id"], ["group_account_message_policy.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["keyword_trigger_id"],
            ["acquisition_keyword_trigger.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["acquisition_message_template.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key",
            name=op.f("uq_group_account_message_execution_idempotency_key"),
        ),
    )
    for name, columns in (
        ("idx_message_execution_dispatch", ["status", "scheduled_at", "next_retry_at"]),
        ("idx_message_execution_policy_created", ["policy_id", "created_at"]),
        ("idx_message_execution_group_sent", ["core_group_id", "sent_at"]),
        (
            "idx_message_execution_group_category_sent",
            ["core_group_id", "content_category", "sent_at"],
        ),
        ("idx_message_execution_account_sent", ["account_id", "sent_at"]),
        (
            "idx_message_execution_group_hash_created",
            ["core_group_id", "content_hash", "created_at"],
        ),
        (
            "idx_message_execution_asset_source",
            ["owned_group_asset_id", "source_message_id"],
        ),
    ):
        op.create_index(name, "group_account_message_execution", columns)

    op.add_column(
        "acquisition_message_template",
        sa.Column("scope", sa.String(length=16), server_default="acquisition", nullable=False),
    )
    op.add_column(
        "acquisition_message_template",
        sa.Column("owned_group_asset_id", sa.Integer(), nullable=True),
    )
    op.execute(
        "UPDATE acquisition_message_template SET scope = 'acquisition', owned_group_asset_id = NULL"
    )
    op.create_foreign_key(
        op.f("fk_acquisition_message_template_owned_group_asset_id_owned_grou"),
        "acquisition_message_template",
        "owned_group_assets",
        ["owned_group_asset_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint(
        op.f("ck_acquisition_message_template_message_template_scope"),
        "acquisition_message_template",
        "scope IN ('acquisition', 'owned_group')",
    )
    op.create_check_constraint(
        op.f("ck_acquisition_message_template_message_template_scope_asset"),
        "acquisition_message_template",
        "(scope = 'acquisition' AND owned_group_asset_id IS NULL) OR "
        "(scope = 'owned_group' AND owned_group_asset_id IS NOT NULL)",
    )
    op.create_index(
        "idx_message_template_scope_asset_enabled",
        "acquisition_message_template",
        ["scope", "owned_group_asset_id", "enabled"],
    )

    op.add_column(
        "acquisition_message",
        sa.Column("message_purpose", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "acquisition_message",
        sa.Column("content_category", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "acquisition_message",
        sa.Column("owned_group_execution_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "acquisition_message",
        sa.Column("core_group_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "acquisition_message",
        sa.Column("content_hash", sa.String(length=64), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_acquisition_message_owned_group_execution_id_group_account_m"),
        "acquisition_message",
        "group_account_message_execution",
        ["owned_group_execution_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        op.f("fk_acquisition_message_core_group_id_group"),
        "acquisition_message",
        "group",
        ["core_group_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        op.f("uq_acquisition_message_owned_group_execution_id"),
        "acquisition_message",
        ["owned_group_execution_id"],
    )
    op.create_index(
        "idx_msg_core_group_sent",
        "acquisition_message",
        ["core_group_id", "sent_at"],
    )
    op.create_index(
        "idx_msg_content_hash",
        "acquisition_message",
        ["core_group_id", "content_hash"],
    )


def downgrade() -> None:
    op.drop_index("idx_msg_content_hash", table_name="acquisition_message")
    op.drop_index("idx_msg_core_group_sent", table_name="acquisition_message")
    op.drop_constraint(
        op.f("uq_acquisition_message_owned_group_execution_id"),
        "acquisition_message",
        type_="unique",
    )
    op.drop_constraint(
        op.f("fk_acquisition_message_core_group_id_group"),
        "acquisition_message",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("fk_acquisition_message_owned_group_execution_id_group_account_m"),
        "acquisition_message",
        type_="foreignkey",
    )
    for column in (
        "content_hash",
        "core_group_id",
        "owned_group_execution_id",
        "content_category",
        "message_purpose",
    ):
        op.drop_column("acquisition_message", column)

    op.drop_index(
        "idx_message_template_scope_asset_enabled",
        table_name="acquisition_message_template",
    )
    op.drop_constraint(
        op.f("ck_acquisition_message_template_message_template_scope_asset"),
        "acquisition_message_template",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_acquisition_message_template_message_template_scope"),
        "acquisition_message_template",
        type_="check",
    )
    op.drop_constraint(
        op.f("fk_acquisition_message_template_owned_group_asset_id_owned_grou"),
        "acquisition_message_template",
        type_="foreignkey",
    )
    op.drop_column("acquisition_message_template", "owned_group_asset_id")
    op.drop_column("acquisition_message_template", "scope")

    op.drop_table("group_account_message_execution")
    op.drop_table("group_account_message_policy")
