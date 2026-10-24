"""Add P4 pacing decision, history governance and archive storage.

Revision ID: 051_p4_pacing_and_history
Revises: 050_qq_growth_automation
"""

import sqlalchemy as sa
from alembic import op

revision = "051_p4_pacing_and_history"
down_revision = "050_qq_growth_automation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "growth_pacing_decision",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("stage", sa.String(length=24), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("window_started_at", sa.DateTime(), nullable=False),
        sa.Column("window_seconds", sa.Integer(), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("gated_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("scheduled_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("selected_account_id", sa.Integer(), nullable=True),
        sa.Column("applied", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("gate_reasons_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("candidates_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("NOW()")),
        sa.CheckConstraint(
            "candidate_count >= 0 AND gated_count >= 0 AND scheduled_count >= 0",
            name="pacing_decision_counts",
        ),
        sa.ForeignKeyConstraint(
            ["selected_account_id"], ["telegram_account.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_growth_pacing_decision_recent", "growth_pacing_decision", ["created_at"]
    )
    op.create_index(
        "ix_growth_pacing_decision_stage", "growth_pacing_decision", ["stage", "created_at"]
    )

    op.create_table(
        "growth_pacing_daily_account_stat",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("stat_date", sa.DateTime(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("decision_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("candidate_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("gated_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("scheduled_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("avg_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("fair_share_avg", sa.Float(), nullable=False, server_default="0"),
        sa.Column("actual_share_avg", sa.Float(), nullable=False, server_default="0"),
        sa.Column("ad_success_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("NOW()")),
        sa.CheckConstraint(
            "candidate_count >= 0 AND gated_count >= 0 AND scheduled_count >= 0 "
            "AND decision_count >= 0 AND ad_success_count >= 0",
            name="pacing_daily_stat_counts",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["telegram_account.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stat_date", "account_id", name="uq_pacing_daily_account_stat"),
    )
    op.create_index(
        "ix_pacing_daily_account_stat_date",
        "growth_pacing_daily_account_stat",
        ["stat_date"],
    )

    op.create_table(
        "growth_history_governance_batch",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="running"),
        sa.Column("dry_run", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("cutoffs_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("candidates_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("archived_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deleted_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("checksum", sa.String(length=64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("NOW()")),
        sa.CheckConstraint("mode IN ('dry_run','archive','cleanup')", name="history_batch_mode"),
        sa.CheckConstraint(
            "status IN ('running','succeeded','failed')", name="history_batch_status"
        ),
        sa.CheckConstraint(
            "archived_count >= 0 AND deleted_count >= 0", name="history_batch_counts"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_history_governance_batch_created",
        "growth_history_governance_batch",
        ["created_at"],
    )
    op.create_index(
        "ix_history_governance_batch_mode",
        "growth_history_governance_batch",
        ["mode", "created_at"],
    )

    op.create_table(
        "growth_operation_archive",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("operation_id", sa.BigInteger(), nullable=False),
        sa.Column("batch_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=True),
        sa.Column("campaign_id", sa.Integer(), nullable=True),
        sa.Column("target_key", sa.String(length=500), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("snapshot_id", sa.String(length=128), nullable=False),
        sa.Column("group_version", sa.String(length=128), nullable=False),
        sa.Column("rule_version", sa.String(length=128), nullable=False),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("budget_reservation_id", sa.String(length=64), nullable=True),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column(
            "reconciliation_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("provider_request_id", sa.String(length=160), nullable=True),
        sa.Column("provider_receipt_json", sa.Text(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reserved_at", sa.DateTime(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("survival_due_at", sa.DateTime(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("outbox_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("attempts_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("events_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("archived_at", sa.DateTime(), nullable=False, server_default=sa.text("NOW()")),
        sa.CheckConstraint(
            "state IN ('resolved','failed','cancelled')", name="archive_terminal_state"
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"], ["growth_history_governance_batch.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("operation_id", name="uq_growth_operation_archive_operation"),
    )
    op.create_index(
        "ix_growth_operation_archive_archived_at",
        "growth_operation_archive",
        ["archived_at"],
    )
    op.create_index(
        "ix_growth_operation_archive_account",
        "growth_operation_archive",
        ["account_id", "archived_at"],
    )

    # Cleanup query support: partial indexes over the terminal/reconciliation
    # slices the governance passes scan.
    op.create_index(
        "ix_growth_operation_terminal_resolved",
        "growth_operation",
        ["state", "resolved_at"],
        postgresql_where=sa.text(
            "state IN ('resolved','failed','cancelled') AND reconciliation_required = false"
        ),
    )
    op.create_index(
        "ix_growth_operation_unresolved",
        "growth_operation",
        ["updated_at"],
        postgresql_where=sa.text(
            "reconciliation_required = true OR state IN "
            "('reserved','sent','receipt_pending','confirmed','survival_pending')"
        ),
    )
    op.create_index(
        "ix_growth_outbox_terminal_completed",
        "growth_outbox",
        ["state", "completed_at"],
        postgresql_where=sa.text("state IN ('succeeded','dead','cancelled')"),
    )
    op.create_index(
        "ix_growth_outbox_open_available",
        "growth_outbox",
        ["kind", "available_at"],
        postgresql_where=sa.text("state IN ('pending','retryable_failed')"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_growth_outbox_open_available",
        table_name="growth_outbox",
    )
    op.drop_index(
        "ix_growth_outbox_terminal_completed",
        table_name="growth_outbox",
    )
    op.drop_index(
        "ix_growth_operation_unresolved",
        table_name="growth_operation",
    )
    op.drop_index(
        "ix_growth_operation_terminal_resolved",
        table_name="growth_operation",
    )
    op.drop_index(
        "ix_growth_operation_archive_account",
        table_name="growth_operation_archive",
    )
    op.drop_index(
        "ix_growth_operation_archive_archived_at",
        table_name="growth_operation_archive",
    )
    op.drop_table("growth_operation_archive")
    op.drop_index(
        "ix_history_governance_batch_mode",
        table_name="growth_history_governance_batch",
    )
    op.drop_index(
        "ix_history_governance_batch_created",
        table_name="growth_history_governance_batch",
    )
    op.drop_table("growth_history_governance_batch")
    op.drop_index(
        "ix_pacing_daily_account_stat_date",
        table_name="growth_pacing_daily_account_stat",
    )
    op.drop_table("growth_pacing_daily_account_stat")
    op.drop_index(
        "ix_growth_pacing_decision_stage",
        table_name="growth_pacing_decision",
    )
    op.drop_index(
        "ix_growth_pacing_decision_recent",
        table_name="growth_pacing_decision",
    )
    op.drop_table("growth_pacing_decision")
