"""Add the P5 release control plane tables.

Revision ID: 052_p5_release_control
Revises: 051_p4_pacing_and_history
"""

import sqlalchemy as sa
from alembic import op

revision = "052_p5_release_control"
down_revision = "051_p4_pacing_and_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "p5_release_record",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("release_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="frozen"),
        sa.Column("image_digest", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("migration_revision", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("mode", sa.String(length=16), nullable=False, server_default="off"),
        sa.Column("cohort_percent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("canary_account_ids_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("cohort_account_ids_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("stage_history_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("enabled_flags_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("config_snapshot_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("selected_canary_account_id", sa.Integer(), nullable=True),
        sa.Column("selected_canary_report_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("baseline_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("gate_result", sa.String(length=8), nullable=True),
        sa.Column("gate_reasons_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("rollback_at", sa.DateTime(), nullable=True),
        sa.Column("rollback_reason", sa.Text(), nullable=True),
        sa.Column("rollback_metrics_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("affected_cohort_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("recovered_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("NOW()")),
        sa.CheckConstraint(
            "status IN ('frozen','active','completed','rolled_back')",
            name="p5_release_status",
        ),
        sa.CheckConstraint(
            "cohort_percent >= 0 AND cohort_percent <= 100",
            name="p5_release_percent",
        ),
        sa.UniqueConstraint("release_id", name="uq_p5_release_record_release_id"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_p5_release_record_created", "p5_release_record", ["created_at"], unique=False
    )
    op.create_table(
        "p5_gate_evaluation",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("release_id", sa.String(length=128), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("stage", sa.String(length=24), nullable=False, server_default=""),
        sa.Column("result", sa.String(length=8), nullable=False),
        sa.Column("reasons_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("metrics_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("cohort_account_ids_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("window_started_at", sa.DateTime(), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("NOW()")),
        sa.CheckConstraint("result IN ('pass','hold','stop')", name="p5_gate_result"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_p5_gate_evaluation_release_time",
        "p5_gate_evaluation",
        ["release_id", "evaluated_at"],
        unique=False,
    )
    op.create_index(
        "ix_p5_gate_evaluation_result_time",
        "p5_gate_evaluation",
        ["result", "evaluated_at"],
        unique=False,
    )


def downgrade() -> None:
    # P5 rollouts are forward-only by policy; the downgrade exists only for
    # disposable development clusters and never runs against production.
    op.drop_index("ix_p5_gate_evaluation_result_time", table_name="p5_gate_evaluation")
    op.drop_index("ix_p5_gate_evaluation_release_time", table_name="p5_gate_evaluation")
    op.drop_table("p5_gate_evaluation")
    op.drop_index("ix_p5_release_record_created", table_name="p5_release_record")
    op.drop_table("p5_release_record")
