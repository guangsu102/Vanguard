"""Add account proxy policy

Revision ID: 003_add_account_proxy_policy
Revises: 002_add_campaign_execution
Create Date: 2026-06-29

"""

from alembic import op
import sqlalchemy as sa


revision = "003_add_account_proxy_policy"
down_revision = "002_add_campaign_execution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "telegram_account",
        sa.Column(
            "proxy_mode",
            sa.Enum("dynamic", "static", "none", name="proxymode", native_enum=False),
            nullable=False,
            server_default="dynamic",
            comment="代理模式: dynamic/static/none",
        ),
    )
    op.add_column(
        "telegram_account",
        sa.Column("static_proxy_id", sa.Integer(), nullable=True, comment="静态绑定代理ID"),
    )
    op.create_foreign_key(
        "fk_telegram_account_static_proxy_id_proxy",
        "telegram_account",
        "proxy",
        ["static_proxy_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("idx_account_static_proxy", "telegram_account", ["static_proxy_id"])


def downgrade() -> None:
    op.drop_index("idx_account_static_proxy", table_name="telegram_account")
    op.drop_constraint(
        "fk_telegram_account_static_proxy_id_proxy",
        "telegram_account",
        type_="foreignkey",
    )
    op.drop_column("telegram_account", "static_proxy_id")
    op.drop_column("telegram_account", "proxy_mode")
