"""Add account-level AI Persona.

Revision ID: 036_add_account_ai_persona
Revises: 035_owned_group_audit_resource_type
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "036_add_account_ai_persona"
down_revision = "035_owned_group_audit_resource_type"
branch_labels = None
depends_on = None


_JSON_DOCUMENT = sa.JSON(none_as_null=True).with_variant(
    postgresql.JSONB(none_as_null=True),
    "postgresql",
)
_REVISION_CONSTRAINT = "ck_telegram_account_account_persona_revision_non_negative"
_POSTGRES_CONSISTENCY_CONSTRAINT = "ck_telegram_account_account_persona_consistency_postgresql"
_SQLITE_CONSISTENCY_CONSTRAINT = "ck_telegram_account_account_persona_consistency_sqlite"
_POSTGRES_CONSISTENCY_SQL = (
    "(ai_persona IS NULL AND ai_persona_hash IS NULL) OR "
    "(ai_persona IS NOT NULL AND ai_persona_revision >= 1 AND "
    "ai_persona_hash ~ '^[0-9a-f]{64}$')"
)
_SQLITE_CONSISTENCY_SQL = (
    "(ai_persona IS NULL AND ai_persona_hash IS NULL) OR "
    "(ai_persona IS NOT NULL AND ai_persona_revision >= 1 AND "
    "length(ai_persona_hash) = 64 AND "
    "lower(ai_persona_hash) = ai_persona_hash AND "
    "ai_persona_hash NOT GLOB '*[^0-9a-f]*')"
)
_PERSONA_RUNTIME_DEFAULT = (
    '{"ownedGroupAiPersona":{"enabled":false,"revision":0,"updatedAt":null,"updatedBy":null}}'
)


def _dialect_name() -> str:
    return op.get_bind().dialect.name


def upgrade() -> None:
    columns = (
        sa.Column("ai_persona", _JSON_DOCUMENT, nullable=True),
        sa.Column(
            "ai_persona_revision",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("ai_persona_hash", sa.String(length=64), nullable=True),
        sa.Column("ai_persona_updated_at", sa.DateTime(), nullable=True),
        sa.Column("ai_persona_updated_by", sa.Integer(), nullable=True),
    )
    dialect = _dialect_name()

    if dialect == "sqlite":
        with op.batch_alter_table("telegram_account") as batch_op:
            for column in columns:
                batch_op.add_column(column)
            batch_op.create_check_constraint(
                op.f(_REVISION_CONSTRAINT),
                "ai_persona_revision >= 0",
            )
            batch_op.create_check_constraint(
                op.f(_SQLITE_CONSISTENCY_CONSTRAINT),
                _SQLITE_CONSISTENCY_SQL,
            )
    else:
        for column in columns:
            op.add_column("telegram_account", column)
        op.create_check_constraint(
            op.f(_REVISION_CONSTRAINT),
            "telegram_account",
            "ai_persona_revision >= 0",
        )
        op.create_check_constraint(
            op.f(_POSTGRES_CONSISTENCY_CONSTRAINT),
            "telegram_account",
            _POSTGRES_CONSISTENCY_SQL,
        )

    op.execute(
        sa.text(
            """
            INSERT INTO system_setting (key, value, description)
            VALUES (
                'app.runtime_settings',
                :value,
                'Admin-managed runtime application settings'
            )
            ON CONFLICT (key) DO NOTHING
            """
        ).bindparams(value=_PERSONA_RUNTIME_DEFAULT)
    )


def downgrade() -> None:
    dialect = _dialect_name()
    if dialect == "postgresql":
        op.execute(
            """
            UPDATE system_setting
            SET value = (
                    COALESCE(NULLIF(BTRIM(value), ''), '{}')::jsonb
                    - 'ownedGroupAiPersona'
                )::text,
                updated_at = NOW()
            WHERE key = 'app.runtime_settings'
            """
        )
    elif dialect == "sqlite":
        op.execute(
            """
            UPDATE system_setting
            SET value = json_remove(value, '$.ownedGroupAiPersona'),
                updated_at = CURRENT_TIMESTAMP
            WHERE key = 'app.runtime_settings'
              AND json_valid(value)
            """
        )

    if dialect == "sqlite":
        with op.batch_alter_table("telegram_account") as batch_op:
            batch_op.drop_constraint(
                op.f(_SQLITE_CONSISTENCY_CONSTRAINT),
                type_="check",
            )
            batch_op.drop_constraint(op.f(_REVISION_CONSTRAINT), type_="check")
            for column_name in (
                "ai_persona_updated_by",
                "ai_persona_updated_at",
                "ai_persona_hash",
                "ai_persona_revision",
                "ai_persona",
            ):
                batch_op.drop_column(column_name)
    else:
        op.drop_constraint(
            op.f(_POSTGRES_CONSISTENCY_CONSTRAINT),
            "telegram_account",
            type_="check",
        )
        op.drop_constraint(
            op.f(_REVISION_CONSTRAINT),
            "telegram_account",
            type_="check",
        )
        for column_name in (
            "ai_persona_updated_by",
            "ai_persona_updated_at",
            "ai_persona_hash",
            "ai_persona_revision",
            "ai_persona",
        ):
            op.drop_column("telegram_account", column_name)
