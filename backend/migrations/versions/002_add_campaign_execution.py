"""Add campaign_execution table

Revision ID: 002_add_campaign_execution
Revises: 001_add_session_string
Create Date: 2026-06-02

"""
from alembic import op
import sqlalchemy as sa


revision = "002_add_campaign_execution"
down_revision = "001_add_session_string"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "campaign_execution",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False, comment="活动ID"),
        sa.Column("user_id", sa.Integer(), nullable=True, comment="目标用户ID"),
        sa.Column("group_id", sa.BigInteger(), nullable=True, comment="目标群组ID"),
        sa.Column(
            "status",
            sa.Enum("pending", "running", "completed", "skipped", "failed", name="campaignexecutionstatus", native_enum=False),
            nullable=False,
            comment="执行状态",
        ),
        sa.Column("trigger_timing", sa.String(length=30), nullable=True, comment="触发时机"),
        sa.Column("trigger_event", sa.String(length=50), nullable=True, comment="触发事件"),
        sa.Column(
            "distribution_mode",
            sa.Enum("welcome", "delayed", "scheduled", "manual", "periodic", name="campaigndistributionmode", native_enum=False),
            nullable=True,
            comment="分发模式",
        ),
        sa.Column("scheduled_at", sa.DateTime(), nullable=True, comment="计划执行时间"),
        sa.Column("executed_at", sa.DateTime(), nullable=True, comment="实际执行时间"),
        sa.Column("last_run_at", sa.DateTime(), nullable=True, comment="活动最近运行时间"),
        sa.Column("delivered", sa.Boolean(), nullable=False, server_default=sa.false(), comment="是否发送消息"),
        sa.Column("reward_granted", sa.Boolean(), nullable=False, server_default=sa.false(), comment="是否发放奖励"),
        sa.Column("error", sa.String(length=1000), nullable=True, comment="错误信息"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaign.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_campaign_execution_campaign", "campaign_execution", ["campaign_id"])
    op.create_index("idx_campaign_execution_user", "campaign_execution", ["user_id"])
    op.create_index("idx_campaign_execution_status_scheduled", "campaign_execution", ["status", "scheduled_at"])
    op.create_index("idx_campaign_execution_last_run", "campaign_execution", ["campaign_id", "last_run_at"])


def downgrade() -> None:
    op.drop_index("idx_campaign_execution_last_run", table_name="campaign_execution")
    op.drop_index("idx_campaign_execution_status_scheduled", table_name="campaign_execution")
    op.drop_index("idx_campaign_execution_user", table_name="campaign_execution")
    op.drop_index("idx_campaign_execution_campaign", table_name="campaign_execution")
    op.drop_table("campaign_execution")
