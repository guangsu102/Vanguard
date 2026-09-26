"""Add durable join request reservations and the 30 request policy.

Revision ID: 043_durable_join_budget
Revises: 042_set_growth_ad_capacity
"""

import sqlalchemy as sa
from alembic import op

revision = "043_durable_join_budget"
down_revision = "042_set_growth_ad_capacity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "acquisition_auto_join_attempt",
        sa.Column(
            "request_state",
            sa.String(length=30),
            server_default=sa.text("'released'"),
            nullable=False,
        ),
    )
    op.add_column(
        "acquisition_auto_join_attempt",
        sa.Column("reservation_key", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "acquisition_auto_join_attempt",
        sa.Column("target_key", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "acquisition_auto_join_attempt",
        sa.Column(
            "require_auto_join_enabled",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
    )
    op.add_column(
        "acquisition_auto_join_attempt",
        sa.Column("reserved_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "acquisition_auto_join_attempt",
        sa.Column("reservation_expires_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "acquisition_auto_join_attempt",
        sa.Column("request_sent_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "acquisition_auto_join_attempt",
        sa.Column("reservation_released_at", sa.DateTime(), nullable=True),
    )
    op.execute(
        """
        UPDATE acquisition_auto_join_attempt
        SET request_state = CASE
                WHEN telegram_action_attempted THEN 'sent'
                ELSE 'released'
            END,
            request_sent_at = CASE
                WHEN telegram_action_attempted THEN attempted_at
                ELSE NULL
            END
        """
    )
    op.alter_column(
        "group_ad_handover",
        "join_interval_min_minutes",
        server_default=sa.text("48"),
    )
    op.alter_column(
        "group_ad_handover",
        "join_interval_max_minutes",
        server_default=sa.text("120"),
    )
    op.execute(
        """
        UPDATE group_ad_handover
        SET join_interval_min_minutes = GREATEST(join_interval_min_minutes, 48),
            join_interval_max_minutes = GREATEST(
                join_interval_max_minutes,
                join_interval_min_minutes,
                48
            )
        """
    )
    op.execute(
        """
        UPDATE system_setting
        SET value = jsonb_set(
                value::jsonb,
                '{enabled}',
                'false'::jsonb,
                TRUE
            )::text,
            updated_at = NOW()
        WHERE key = 'automation.auto_join_scheduler'
        """
    )
    op.create_unique_constraint(
        "uq_auto_join_reservation_key",
        "acquisition_auto_join_attempt",
        ["reservation_key"],
    )
    op.create_index(
        "idx_auto_join_account_request_state",
        "acquisition_auto_join_attempt",
        ["account_id", "request_state", "request_sent_at"],
    )
    op.create_index(
        "idx_auto_join_account_target_state",
        "acquisition_auto_join_attempt",
        ["account_id", "target_key", "request_state"],
    )

    op.add_column(
        "telegram_account_operation_config",
        sa.Column(
            "join_review_backlog_paused",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.add_column(
        "telegram_account_operation_config",
        sa.Column("join_review_backlog_reason", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "group_account_membership",
        sa.Column(
            "review_status",
            sa.String(length=40),
            server_default=sa.text("'initial_pending'"),
            nullable=False,
        ),
    )
    for name in (
        "review_started_at",
        "review_next_at",
        "review_deadline_at",
        "leave_requested_at",
        "leave_confirmed_at",
        "leave_retry_at",
    ):
        op.add_column(
            "group_account_membership",
            sa.Column(name, sa.DateTime(), nullable=True),
        )
    for name in ("review_attempts", "leave_attempts"):
        op.add_column(
            "group_account_membership",
            sa.Column(name, sa.Integer(), server_default=sa.text("0"), nullable=False),
        )
    op.add_column(
        "group_account_membership",
        sa.Column("leave_error", sa.Text(), nullable=True),
    )
    op.drop_constraint(
        "ck_group_membership_inactive_ad_blocked",
        "group_account_membership",
        type_="check",
    )
    op.create_check_constraint(
        "ck_group_membership_inactive_ad_blocked",
        "group_account_membership",
        "status NOT IN ('left', 'banned', 'rejected', 'leave_failed') "
        "OR ad_status = 'blocked'",
    )
    op.execute(
        """
        UPDATE group_account_membership
        SET review_started_at = COALESCE(review_started_at, joined_at, created_at),
            review_deadline_at = COALESCE(
                review_deadline_at,
                COALESCE(joined_at, created_at) + INTERVAL '24 hours'
            ),
            review_next_at = COALESCE(
                review_next_at,
                CASE
                    WHEN status = 'rejected' THEN NOW()
                    ELSE COALESCE(joined_at, created_at) + INTERVAL '2 hours'
                END
            ),
            review_status = CASE
                WHEN status = 'rejected' THEN 'leave_failed'
                WHEN status IN ('left', 'banned') THEN 'left'
                WHEN status = 'joined' AND ad_status = 'active' THEN 'approved'
                ELSE 'initial_pending'
            END
        """
    )
    op.create_index(
        "idx_group_membership_review_due",
        "group_account_membership",
        ["account_id", "review_status", "review_next_at"],
    )
    op.alter_column(
        "telegram_account_operation_config",
        "max_groups_per_day",
        server_default=sa.text("30"),
    )
    op.alter_column(
        "telegram_account_operation_config",
        "join_interval_min_seconds",
        server_default=sa.text("2880"),
    )
    op.alter_column(
        "telegram_account_operation_config",
        "join_interval_max_seconds",
        server_default=sa.text("7200"),
    )
    op.execute(
        """
        UPDATE telegram_account_operation_config
        SET max_groups_per_day = CASE
                WHEN max_groups_per_day = 0 THEN 0
                ELSE 30
            END,
            join_interval_min_seconds = GREATEST(join_interval_min_seconds, 2880),
            join_interval_max_seconds = GREATEST(
                join_interval_max_seconds,
                join_interval_min_seconds,
                2880
            ),
            updated_at = NOW()
        """
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_group_membership_inactive_ad_blocked",
        "group_account_membership",
        type_="check",
    )
    op.create_check_constraint(
        "ck_group_membership_inactive_ad_blocked",
        "group_account_membership",
        "status NOT IN ('left', 'banned', 'rejected') OR ad_status = 'blocked'",
    )
    op.alter_column(
        "group_ad_handover",
        "join_interval_max_minutes",
        server_default=sa.text("30"),
    )
    op.alter_column(
        "group_ad_handover",
        "join_interval_min_minutes",
        server_default=sa.text("1"),
    )
    op.drop_index(
        "idx_group_membership_review_due",
        table_name="group_account_membership",
    )
    for name in (
        "leave_error",
        "leave_attempts",
        "leave_retry_at",
        "leave_confirmed_at",
        "leave_requested_at",
        "review_attempts",
        "review_deadline_at",
        "review_next_at",
        "review_started_at",
        "review_status",
    ):
        op.drop_column("group_account_membership", name)
    op.alter_column(
        "telegram_account_operation_config",
        "join_interval_max_seconds",
        server_default=sa.text("900"),
    )
    op.alter_column(
        "telegram_account_operation_config",
        "join_interval_min_seconds",
        server_default=sa.text("60"),
    )
    op.alter_column(
        "telegram_account_operation_config",
        "max_groups_per_day",
        server_default=sa.text("10"),
    )
    op.drop_column(
        "telegram_account_operation_config", "join_review_backlog_reason"
    )
    op.drop_column(
        "telegram_account_operation_config", "join_review_backlog_paused"
    )
    op.drop_index(
        "idx_auto_join_account_request_state",
        table_name="acquisition_auto_join_attempt",
    )
    op.drop_index(
        "idx_auto_join_account_target_state",
        table_name="acquisition_auto_join_attempt",
    )
    op.drop_constraint(
        "uq_auto_join_reservation_key",
        "acquisition_auto_join_attempt",
        type_="unique",
    )
    op.drop_column("acquisition_auto_join_attempt", "reservation_released_at")
    op.drop_column("acquisition_auto_join_attempt", "request_sent_at")
    op.drop_column("acquisition_auto_join_attempt", "reservation_expires_at")
    op.drop_column("acquisition_auto_join_attempt", "reserved_at")
    op.drop_column("acquisition_auto_join_attempt", "reservation_key")
    op.drop_column("acquisition_auto_join_attempt", "require_auto_join_enabled")
    op.drop_column("acquisition_auto_join_attempt", "target_key")
    op.drop_column("acquisition_auto_join_attempt", "request_state")
