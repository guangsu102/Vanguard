"""Widen owned-group audit resource type for stage 2.

Revision ID: 035_owned_group_audit_resource_type
Revises: 034_owned_group_messaging
"""

import sqlalchemy as sa
from alembic import op

revision = "035_owned_group_audit_resource_type"
down_revision = "034_owned_group_messaging"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "owned_group_audit_events",
        "resource_type",
        existing_type=sa.String(length=16),
        type_=sa.String(length=32),
        existing_nullable=True,
    )
    op.alter_column(
        "owned_group_audit_events",
        "resource_id",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=True,
        postgresql_using="resource_id::bigint",
    )


def downgrade() -> None:
    op.alter_column(
        "owned_group_audit_events",
        "resource_id",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=True,
        postgresql_using="resource_id::integer",
    )
    op.alter_column(
        "owned_group_audit_events",
        "resource_type",
        existing_type=sa.String(length=32),
        type_=sa.String(length=16),
        existing_nullable=True,
    )
