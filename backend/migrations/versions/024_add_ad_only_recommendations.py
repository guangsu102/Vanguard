"""Add ad-only recommendation and recoverable handover workflow.

Revision ID: 024_add_ad_only_recommendations
Revises: 023_add_telegram_private_inbox
"""

import sqlalchemy as sa
from alembic import op

revision = "024_add_ad_only_recommendations"
down_revision = "023_add_telegram_private_inbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "group_ad_only_assessment",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("telegram_group_id", sa.BigInteger(), nullable=False),
        sa.Column("source_growth_account_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("rule_version", sa.String(length=32), nullable=False),
        sa.Column("completed_sample_count", sa.Integer(), nullable=False),
        sa.Column("consecutive_success_count", sa.Integer(), nullable=False),
        sa.Column("send_success_percent", sa.Integer(), nullable=False),
        sa.Column("survival_24h_percent", sa.Integer(), nullable=False),
        sa.Column("pending_sample_count", sa.Integer(), nullable=False),
        sa.Column("group_failure_count", sa.Integer(), nullable=False),
        sa.Column("deleted_sample_count", sa.Integer(), nullable=False),
        sa.Column("metrics_json", sa.Text(), nullable=False),
        sa.Column("blocking_reasons_json", sa.Text(), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=False),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("sample_window_started_at", sa.DateTime(), nullable=False),
        sa.Column("sample_window_ended_at", sa.DateTime(), nullable=False),
        sa.Column("valid_until", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["group_id"], ["group.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_growth_account_id"],
            ["telegram_account.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_group_ad_only_assessment_group",
        "group_ad_only_assessment",
        ["group_id", "created_at"],
    )
    op.create_index(
        "idx_group_ad_only_assessment_status",
        "group_ad_only_assessment",
        ["status", "valid_until"],
    )

    op.create_table(
        "group_ad_handover",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("assessment_id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("active_group_key", sa.Integer(), nullable=True),
        sa.Column("source_growth_account_id", sa.Integer(), nullable=False),
        sa.Column("target_ad_only_account_id", sa.Integer(), nullable=False),
        sa.Column("creative_id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=True),
        sa.Column("invite_link_encrypted", sa.Text(), nullable=True),
        sa.Column("invite_secret_expires_at", sa.DateTime(), nullable=True),
        sa.Column("send_mode", sa.String(length=30), nullable=False),
        sa.Column("interval_minutes", sa.Integer(), nullable=False),
        sa.Column("scheduled_times", sa.Text(), nullable=True),
        sa.Column("estimated_daily_sends", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("current_step", sa.String(length=50), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("requested_by_user_id", sa.Integer(), nullable=False),
        sa.Column("approved_by_user_id", sa.Integer(), nullable=False),
        sa.Column("approved_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("failed_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["assessment_id"],
            ["group_ad_only_assessment.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"], ["ad_campaign.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["creative_id"], ["ad_creative.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["group_id"], ["group.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_growth_account_id"],
            ["telegram_account.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_ad_only_account_id"],
            ["telegram_account.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "active_group_key", name="uq_group_ad_handover_active_group"
        ),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_group_ad_handover_idempotency"
        ),
    )
    op.create_index(
        "idx_group_ad_handover_status",
        "group_ad_handover",
        ["status", "updated_at"],
    )
    op.create_index(
        "idx_group_ad_handover_group",
        "group_ad_handover",
        ["group_id", "created_at"],
    )

    op.create_table(
        "group_ad_only_event",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("assessment_id", sa.Integer(), nullable=True),
        sa.Column("handover_id", sa.Integer(), nullable=True),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("step", sa.String(length=50), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=True),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("message", sa.String(length=500), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["assessment_id"],
            ["group_ad_only_assessment.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["group_id"], ["group.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["handover_id"], ["group_ad_handover.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_group_ad_only_event_group",
        "group_ad_only_event",
        ["group_id", "created_at"],
    )
    op.create_index(
        "idx_group_ad_only_event_assessment",
        "group_ad_only_event",
        ["assessment_id", "created_at"],
    )
    op.create_index(
        "idx_group_ad_only_event_handover",
        "group_ad_only_event",
        ["handover_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_group_ad_only_event_handover",
        table_name="group_ad_only_event",
    )
    op.drop_index(
        "idx_group_ad_only_event_assessment",
        table_name="group_ad_only_event",
    )
    op.drop_index(
        "idx_group_ad_only_event_group",
        table_name="group_ad_only_event",
    )
    op.drop_table("group_ad_only_event")

    op.drop_index(
        "idx_group_ad_handover_group",
        table_name="group_ad_handover",
    )
    op.drop_index(
        "idx_group_ad_handover_status",
        table_name="group_ad_handover",
    )
    op.drop_table("group_ad_handover")

    op.drop_index(
        "idx_group_ad_only_assessment_status",
        table_name="group_ad_only_assessment",
    )
    op.drop_index(
        "idx_group_ad_only_assessment_group",
        table_name="group_ad_only_assessment",
    )
    op.drop_table("group_ad_only_assessment")
