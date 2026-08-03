"""add account risk controls

Revision ID: 005_add_account_risk_controls
Revises: 004_add_system_setting
Create Date: 2026-06-30
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "005_add_account_risk_controls"
down_revision = "004_add_system_setting"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("telegram_account", sa.Column("risk_score", sa.Float(), nullable=False, server_default="0"))
    op.add_column("telegram_account", sa.Column("risk_level", sa.String(length=20), nullable=False, server_default="normal"))
    op.add_column("telegram_account", sa.Column("risk_pause_until", sa.DateTime(), nullable=True))
    op.add_column("telegram_account", sa.Column("risk_recovery_until", sa.DateTime(), nullable=True))
    op.add_column("telegram_account", sa.Column("last_risk_decay_at", sa.DateTime(), nullable=True))
    op.add_column("telegram_account", sa.Column("risk_reason", sa.String(length=255), nullable=True))
    op.add_column("telegram_account", sa.Column("last_risk_event_at", sa.DateTime(), nullable=True))
    op.create_index("idx_account_risk_pause_until", "telegram_account", ["risk_pause_until"], unique=False)
    op.create_index("idx_account_risk_level", "telegram_account", ["risk_level"], unique=False)
    op.create_index("idx_account_risk_recovery_until", "telegram_account", ["risk_recovery_until"], unique=False)

    op.create_table(
        "account_risk_event",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("target_type", sa.String(length=50), nullable=True),
        sa.Column("target_id", sa.String(length=100), nullable=True),
        sa.Column("fingerprint_id", sa.String(length=50), nullable=True),
        sa.Column("proxy_mode", sa.String(length=20), nullable=True),
        sa.Column("proxy_id", sa.Integer(), nullable=True),
        sa.Column("proxy_country", sa.String(length=2), nullable=True),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["account_id"], ["telegram_account.id"], name=op.f("fk_account_risk_event_account_id_telegram_account"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_account_risk_event")),
    )
    op.create_index("idx_account_risk_event_account_created", "account_risk_event", ["account_id", "created_at"], unique=False)
    op.create_index("idx_account_risk_event_action_status", "account_risk_event", ["action", "status"], unique=False)
    op.create_index("idx_account_risk_event_target", "account_risk_event", ["target_type", "target_id"], unique=False)

    op.create_table(
        "account_risk_daily_stat",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("stat_date", sa.Date(), nullable=False),
        sa.Column("action", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("target_type", sa.String(length=50), nullable=True),
        sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_reason", sa.String(length=255), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["account_id"], ["telegram_account.id"], name=op.f("fk_account_risk_daily_stat_account_id_telegram_account"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_account_risk_daily_stat")),
        sa.UniqueConstraint("account_id", "stat_date", "action", "status", "target_type", name="uq_account_risk_daily_stat"),
    )
    op.create_index("idx_account_risk_daily_stat_account_date", "account_risk_daily_stat", ["account_id", "stat_date"], unique=False)
    op.create_index("idx_account_risk_daily_stat_date_status", "account_risk_daily_stat", ["stat_date", "status"], unique=False)

    op.create_table(
        "account_environment_event",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="ok"),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("proxy_mode", sa.String(length=20), nullable=True),
        sa.Column("proxy_id", sa.Integer(), nullable=True),
        sa.Column("proxy_country", sa.String(length=2), nullable=True),
        sa.Column("fingerprint_id", sa.String(length=50), nullable=True),
        sa.Column("device_model", sa.String(length=100), nullable=True),
        sa.Column("system_version", sa.String(length=50), nullable=True),
        sa.Column("app_version", sa.String(length=50), nullable=True),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["account_id"], ["telegram_account.id"], name=op.f("fk_account_environment_event_account_id_telegram_account"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_account_environment_event")),
    )
    op.create_index("idx_account_environment_event_account_created", "account_environment_event", ["account_id", "created_at"], unique=False)
    op.create_index("idx_account_environment_event_type_status", "account_environment_event", ["event_type", "status"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_account_environment_event_type_status", table_name="account_environment_event")
    op.drop_index("idx_account_environment_event_account_created", table_name="account_environment_event")
    op.drop_table("account_environment_event")
    op.drop_index("idx_account_risk_daily_stat_date_status", table_name="account_risk_daily_stat")
    op.drop_index("idx_account_risk_daily_stat_account_date", table_name="account_risk_daily_stat")
    op.drop_table("account_risk_daily_stat")
    op.drop_index("idx_account_risk_event_target", table_name="account_risk_event")
    op.drop_index("idx_account_risk_event_action_status", table_name="account_risk_event")
    op.drop_index("idx_account_risk_event_account_created", table_name="account_risk_event")
    op.drop_table("account_risk_event")
    op.drop_index("idx_account_risk_recovery_until", table_name="telegram_account")
    op.drop_index("idx_account_risk_level", table_name="telegram_account")
    op.drop_index("idx_account_risk_pause_until", table_name="telegram_account")
    op.drop_column("telegram_account", "last_risk_event_at")
    op.drop_column("telegram_account", "risk_reason")
    op.drop_column("telegram_account", "last_risk_decay_at")
    op.drop_column("telegram_account", "risk_recovery_until")
    op.drop_column("telegram_account", "risk_pause_until")
    op.drop_column("telegram_account", "risk_level")
    op.drop_column("telegram_account", "risk_score")
