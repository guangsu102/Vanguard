"""Add frozen Persona metadata to owned-group message executions.

Revision ID: 037_message_persona_snapshot
Revises: 036_add_account_ai_persona
"""

from __future__ import annotations

import json
import unicodedata
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "037_message_persona_snapshot"
down_revision = "036_add_account_ai_persona"
branch_labels = None
depends_on = None


_TABLE = "group_account_message_execution"
_JSON_DOCUMENT = sa.JSON(none_as_null=True).with_variant(
    postgresql.JSONB(none_as_null=True),
    "postgresql",
)
_NEUTRAL_PERSONA: dict[str, Any] = {
    "ad_style": "neutral",
    "catchphrases": [],
    "expertise": [],
    "forbidden_topics": [],
    "interests": [],
    "language_style": "auto",
    "name": "中性群友",
    "preferred_topics": [],
    "reply_length": "short",
    "schema_version": 1,
    "system_prompt": "",
    "tone": "自然、克制、简短",
}
_NEUTRAL_PERSONA_JSON = json.dumps(
    _NEUTRAL_PERSONA,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
)
_NEUTRAL_PERSONA_HASH = (
    "b8012c2a5d57294fbbe2041bb9a22dfa491146653b11e6c45a04b08c965db174"
)
_LEGACY_PROMPT_VERSION = "owned-group-neutral-legacy-v1"

_COMMON_CONSTRAINTS = (
    (
        "ck_group_account_message_execution_persona_source",
        "persona_source_snapshot IS NULL OR persona_source_snapshot IN "
        "('configured', 'neutral_default', 'feature_disabled_default', 'legacy_default')",
    ),
    (
        "ck_group_account_message_execution_persona_revision",
        "persona_revision_snapshot IS NULL OR persona_revision_snapshot >= 0",
    ),
    (
        "ck_group_account_message_execution_template_persona_null",
        "mode_snapshot <> 'template' OR ("
        "persona_revision_snapshot IS NULL AND persona_source_snapshot IS NULL AND "
        "persona_snapshot IS NULL AND persona_hash IS NULL AND "
        "prompt_template_version IS NULL AND prompt_hash IS NULL AND "
        "governance_rules_hash IS NULL)",
    ),
    (
        "ck_group_account_message_execution_ai_persona_required",
        "mode_snapshot <> 'ai' OR status NOT IN ('queued', 'generating') OR ("
        "persona_revision_snapshot IS NOT NULL AND persona_source_snapshot IS NOT NULL AND "
        "persona_snapshot IS NOT NULL AND persona_hash IS NOT NULL AND "
        "prompt_template_version IS NOT NULL)",
    ),
    (
        "ck_group_account_message_execution_persona_consistency",
        "persona_source_snapshot IS NOT NULL OR ("
        "persona_revision_snapshot IS NULL AND persona_snapshot IS NULL AND "
        "persona_hash IS NULL AND prompt_template_version IS NULL)",
    ),
)
_POSTGRES_HASH_CONSTRAINTS = (
    (
        "ck_group_account_message_execution_persona_hash_pg",
        "persona_hash IS NULL OR persona_hash ~ '^[0-9a-f]{64}$'",
    ),
    (
        "ck_group_account_message_execution_prompt_hash_pg",
        "prompt_hash IS NULL OR prompt_hash ~ '^[0-9a-f]{64}$'",
    ),
    (
        "ck_group_account_message_execution_governance_hash_pg",
        "governance_rules_hash IS NULL OR governance_rules_hash ~ '^[0-9a-f]{64}$'",
    ),
)
_SQLITE_HASH_CONSTRAINTS = (
    (
        "ck_group_account_message_execution_persona_hash_sqlite",
        "persona_hash IS NULL OR (length(persona_hash) = 64 AND "
        "lower(persona_hash) = persona_hash AND "
        "persona_hash NOT GLOB '*[^0-9a-f]*')",
    ),
    (
        "ck_group_account_message_execution_prompt_hash_sqlite",
        "prompt_hash IS NULL OR (length(prompt_hash) = 64 AND "
        "lower(prompt_hash) = prompt_hash AND "
        "prompt_hash NOT GLOB '*[^0-9a-f]*')",
    ),
    (
        "ck_group_account_message_execution_governance_hash_sqlite",
        "governance_rules_hash IS NULL OR (length(governance_rules_hash) = 64 AND "
        "lower(governance_rules_hash) = governance_rules_hash AND "
        "governance_rules_hash NOT GLOB '*[^0-9a-f]*')",
    ),
)


def _dialect_name() -> str:
    return op.get_bind().dialect.name


def _columns() -> tuple[sa.Column[Any], ...]:
    return (
        sa.Column("persona_revision_snapshot", sa.Integer(), nullable=True),
        sa.Column("persona_source_snapshot", sa.String(length=32), nullable=True),
        sa.Column("persona_snapshot", _JSON_DOCUMENT, nullable=True),
        sa.Column("persona_hash", sa.String(length=64), nullable=True),
        sa.Column("prompt_template_version", sa.String(length=32), nullable=True),
        sa.Column("prompt_hash", sa.String(length=64), nullable=True),
        sa.Column("governance_rules_hash", sa.String(length=64), nullable=True),
    )


def _postgresql_precheck_and_backfill() -> None:
    op.execute("LOCK TABLE group_account_message_execution IN SHARE ROW EXCLUSIVE MODE")
    op.execute(
        """
        DO $$
        DECLARE active_ids TEXT;
        BEGIN
            SELECT string_agg(id::text, ',' ORDER BY id) INTO active_ids
            FROM (
                SELECT id FROM group_account_message_execution
                WHERE status IN ('generating', 'sending') ORDER BY id LIMIT 50
            ) AS active;
            IF active_ids IS NOT NULL THEN
                RAISE EXCEPTION
                    'stage3 persona migration requires generating/sending to be empty; execution_ids=%',
                    active_ids;
            END IF;
        END
        $$
        """
    )
    op.execute(
        """
        DO $$
        DECLARE invalid_ids TEXT;
        BEGIN
            SELECT string_agg(id::text, ',' ORDER BY id) INTO invalid_ids
            FROM (
                SELECT execution.id
                FROM group_account_message_execution AS execution
                LEFT JOIN group_account_message_policy AS policy
                    ON policy.id = execution.policy_id
                LEFT JOIN owned_group_assets AS asset
                    ON asset.id = execution.owned_group_asset_id
                LEFT JOIN "group" AS core_group
                    ON core_group.id = execution.core_group_id
                WHERE execution.mode_snapshot = 'ai'
                  AND execution.status = 'queued'
                  AND (
                      policy.id IS NULL OR asset.id IS NULL OR core_group.id IS NULL
                      OR execution.owned_group_asset_id <> policy.owned_group_asset_id
                      OR policy.owned_group_asset_id <> asset.id
                      OR execution.account_id <> policy.account_id
                      OR execution.core_group_id <> policy.core_group_id
                      OR policy.core_group_id <> asset.core_group_id
                      OR asset.core_group_id <> core_group.id
                      OR asset.telegram_chat_id IS NULL
                      OR execution.telegram_chat_id <> asset.telegram_chat_id
                      OR execution.telegram_chat_id <> core_group.group_id
                      OR jsonb_typeof(policy.allowed_topics) <> 'array'
                      OR EXISTS (
                          SELECT 1 FROM jsonb_array_elements(policy.allowed_topics) AS topic(value)
                          WHERE jsonb_typeof(topic.value) <> 'string'
                      )
                  )
                ORDER BY execution.id LIMIT 50
            ) AS invalid;
            IF invalid_ids IS NOT NULL THEN
                RAISE EXCEPTION
                    'stage3 persona queued execution mapping precheck failed; execution_ids=%',
                    invalid_ids;
            END IF;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION pg_temp.vanguard_stage3_clean_text(
            input TEXT, max_chars INTEGER
        )
        RETURNS TEXT
        LANGUAGE SQL
        IMMUTABLE
        PARALLEL SAFE
        AS $$
            SELECT LEFT(
                BTRIM(
                    regexp_replace(
                        regexp_replace(
                        regexp_replace(
                            regexp_replace(
                                normalize(COALESCE(input, ''), NFKC),
                                E'[\\r\\n\\t]+', ' ', 'g'
                            ),
                            '[[:cntrl:]]', '', 'g'
                        ),
                        U&'[\\200B-\\200F\\202A-\\202E\\2060\\2066-\\2069\\FEFF]',
                        '', 'g'
                    ),
                        '[[:space:]]+', ' ', 'g'
                    )
                ),
                max_chars
            )
        $$
        """
    )
    op.execute(
        f"""
        WITH queued_snapshots AS (
            SELECT
                execution.id AS execution_id,
                COALESCE((
                    SELECT jsonb_agg(limited.cleaned ORDER BY limited.ordinality)
                    FROM (
                        SELECT deduplicated.cleaned, deduplicated.ordinality
                        FROM (
                            SELECT DISTINCT ON (lower(cleaned.cleaned))
                                cleaned.cleaned, cleaned.ordinality
                            FROM (
                                SELECT
                                    pg_temp.vanguard_stage3_clean_text(topic.value, 100) AS cleaned,
                                    topic.ordinality
                                FROM jsonb_array_elements_text(policy.allowed_topics)
                                    WITH ORDINALITY AS topic(value, ordinality)
                            ) AS cleaned
                            WHERE cleaned.cleaned <> ''
                            ORDER BY lower(cleaned.cleaned), cleaned.ordinality
                        ) AS deduplicated
                        ORDER BY deduplicated.ordinality
                        LIMIT 20
                    ) AS limited
                ), '[]'::jsonb) AS allowed_topics,
                pg_temp.vanguard_stage3_clean_text(core_group.title, 255) AS group_title
            FROM group_account_message_execution AS execution
            JOIN group_account_message_policy AS policy ON policy.id = execution.policy_id
            JOIN owned_group_assets AS asset ON asset.id = execution.owned_group_asset_id
            JOIN "group" AS core_group ON core_group.id = execution.core_group_id
            WHERE execution.mode_snapshot = 'ai'
              AND execution.status = 'queued'
              AND execution.persona_source_snapshot IS NULL
              AND execution.owned_group_asset_id = policy.owned_group_asset_id
              AND policy.owned_group_asset_id = asset.id
              AND execution.account_id = policy.account_id
              AND execution.core_group_id = policy.core_group_id
              AND policy.core_group_id = asset.core_group_id
              AND asset.core_group_id = core_group.id
              AND execution.telegram_chat_id = asset.telegram_chat_id
              AND execution.telegram_chat_id = core_group.group_id
        )
        UPDATE group_account_message_execution AS execution
        SET persona_revision_snapshot = 0,
            persona_source_snapshot = 'legacy_default',
            persona_snapshot = '{_NEUTRAL_PERSONA_JSON}'::jsonb,
            persona_hash = '{_NEUTRAL_PERSONA_HASH}',
            prompt_template_version = '{_LEGACY_PROMPT_VERSION}',
            prompt_context = jsonb_set(
                COALESCE(execution.prompt_context, '{{}}'::jsonb),
                '{{business_snapshot_v1}}',
                jsonb_build_object(
                    'allowed_topics', queued_snapshots.allowed_topics,
                    'group_title', queued_snapshots.group_title
                ),
                TRUE
            )
        FROM queued_snapshots
        WHERE execution.id = queued_snapshots.execution_id
        """
    )
    op.execute(
        f"""
        UPDATE group_account_message_execution
        SET persona_revision_snapshot = 0,
            persona_source_snapshot = 'legacy_default',
            persona_snapshot = '{_NEUTRAL_PERSONA_JSON}'::jsonb,
            persona_hash = '{_NEUTRAL_PERSONA_HASH}',
            prompt_template_version = '{_LEGACY_PROMPT_VERSION}'
        WHERE mode_snapshot = 'ai'
          AND status IN ('pending_review', 'ready_to_send')
          AND persona_source_snapshot IS NULL
        """
    )
    op.execute(
        """
        DO $$
        DECLARE missing_ids TEXT;
        BEGIN
            SELECT string_agg(id::text, ',' ORDER BY id) INTO missing_ids
            FROM (
                SELECT id FROM group_account_message_execution
                WHERE mode_snapshot = 'ai' AND status = 'queued'
                  AND (
                      persona_revision_snapshot IS NULL OR persona_source_snapshot IS NULL
                      OR persona_snapshot IS NULL OR persona_hash IS NULL
                      OR prompt_template_version IS NULL OR prompt_context IS NULL
                      OR NOT (prompt_context ? 'business_snapshot_v1')
                  )
                ORDER BY id LIMIT 50
            ) AS missing;
            IF missing_ids IS NOT NULL THEN
                RAISE EXCEPTION
                    'stage3 persona queued execution final check failed; execution_ids=%',
                    missing_ids;
            END IF;
        END
        $$
        """
    )


def _json_object(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise RuntimeError("stage3 persona migration requires JSON object prompt_context")
    return dict(value)


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        raise RuntimeError("stage3 persona migration requires array allowed_topics")
    return value


def _clean_text(value: Any, max_chars: int) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    clean = "".join(
        character
        for character in normalized
        if character in {"\n", "\r", "\t"}
        or not unicodedata.category(character).startswith("C")
    )
    return " ".join(clean.replace("\r", "\n").splitlines()).strip()[:max_chars]


def _sqlite_precheck_and_backfill() -> None:
    bind = op.get_bind()
    active = bind.execute(
        sa.text(
            "SELECT id FROM group_account_message_execution "
            "WHERE status IN ('generating', 'sending') ORDER BY id LIMIT 50"
        )
    ).scalars()
    active_ids = [int(item) for item in active]
    if active_ids:
        raise RuntimeError(
            "stage3 persona migration requires generating/sending to be empty; "
            f"execution_ids={','.join(map(str, active_ids))}"
        )

    rows = bind.execute(
        sa.text(
            """
            SELECT
                execution.id,
                execution.policy_id,
                execution.owned_group_asset_id,
                execution.core_group_id,
                execution.telegram_chat_id,
                execution.account_id,
                execution.prompt_context,
                policy.id AS joined_policy_id,
                policy.owned_group_asset_id AS policy_asset_id,
                policy.core_group_id AS policy_group_id,
                policy.account_id AS policy_account_id,
                policy.allowed_topics,
                asset.id AS joined_asset_id,
                asset.core_group_id AS asset_group_id,
                asset.telegram_chat_id AS asset_chat_id,
                core_group.id AS joined_group_id,
                core_group.group_id AS group_chat_id,
                core_group.title AS group_title
            FROM group_account_message_execution AS execution
            LEFT JOIN group_account_message_policy AS policy
                ON policy.id = execution.policy_id
            LEFT JOIN owned_group_assets AS asset
                ON asset.id = execution.owned_group_asset_id
            LEFT JOIN "group" AS core_group
                ON core_group.id = execution.core_group_id
            WHERE execution.mode_snapshot = 'ai'
              AND execution.status = 'queued'
            ORDER BY execution.id
            """
        )
    ).mappings()

    invalid_ids: list[int] = []
    updates: list[dict[str, Any]] = []
    for row in rows:
        execution_id = int(row["id"])
        try:
            allowed_topics_raw = _json_list(row["allowed_topics"])
            if any(not isinstance(item, str) for item in allowed_topics_raw):
                raise ValueError("allowed_topics contains a non-string")
            mapping_valid = (
                row["joined_policy_id"] is not None
                and row["joined_asset_id"] is not None
                and row["joined_group_id"] is not None
                and row["owned_group_asset_id"] == row["policy_asset_id"]
                and row["policy_asset_id"] == row["joined_asset_id"]
                and row["account_id"] == row["policy_account_id"]
                and row["core_group_id"] == row["policy_group_id"]
                and row["policy_group_id"] == row["asset_group_id"]
                and row["asset_group_id"] == row["joined_group_id"]
                and row["asset_chat_id"] is not None
                and row["telegram_chat_id"] == row["asset_chat_id"]
                and row["telegram_chat_id"] == row["group_chat_id"]
            )
            if not mapping_valid:
                raise ValueError("mapping mismatch")
        except (TypeError, ValueError, json.JSONDecodeError, RuntimeError):
            invalid_ids.append(execution_id)
            continue

        normalized_topics: list[str] = []
        seen: set[str] = set()
        for item in allowed_topics_raw:
            cleaned = _clean_text(item, 100)
            key = cleaned.casefold()
            if not cleaned or key in seen:
                continue
            seen.add(key)
            normalized_topics.append(cleaned)
            if len(normalized_topics) >= 20:
                break
        prompt_context = _json_object(row["prompt_context"])
        prompt_context.pop("business_snapshot_v1", None)
        prompt_context["business_snapshot_v1"] = {
            "allowed_topics": normalized_topics,
            "group_title": _clean_text(row["group_title"], 255),
        }
        updates.append(
            {
                "execution_id": execution_id,
                "persona_snapshot": _NEUTRAL_PERSONA_JSON,
                "persona_hash": _NEUTRAL_PERSONA_HASH,
                "prompt_template_version": _LEGACY_PROMPT_VERSION,
                "prompt_context": json.dumps(
                    prompt_context,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            }
        )

    if invalid_ids:
        raise RuntimeError(
            "stage3 persona queued execution mapping precheck failed; "
            f"execution_ids={','.join(map(str, invalid_ids[:50]))}"
        )

    if updates:
        bind.execute(
            sa.text(
                """
                UPDATE group_account_message_execution
                SET persona_revision_snapshot = 0,
                    persona_source_snapshot = 'legacy_default',
                    persona_snapshot = :persona_snapshot,
                    persona_hash = :persona_hash,
                    prompt_template_version = :prompt_template_version,
                    prompt_context = :prompt_context
                WHERE id = :execution_id
                  AND persona_source_snapshot IS NULL
                """
            ),
            updates,
        )

    bind.execute(
        sa.text(
            """
            UPDATE group_account_message_execution
            SET persona_revision_snapshot = 0,
                persona_source_snapshot = 'legacy_default',
                persona_snapshot = :persona_snapshot,
                persona_hash = :persona_hash,
                prompt_template_version = :prompt_template_version
            WHERE mode_snapshot = 'ai'
              AND status IN ('pending_review', 'ready_to_send')
              AND persona_source_snapshot IS NULL
            """
        ),
        {
            "persona_snapshot": _NEUTRAL_PERSONA_JSON,
            "persona_hash": _NEUTRAL_PERSONA_HASH,
            "prompt_template_version": _LEGACY_PROMPT_VERSION,
        },
    )

    missing = bind.execute(
        sa.text(
            """
            SELECT id FROM group_account_message_execution
            WHERE mode_snapshot = 'ai' AND status = 'queued'
              AND (
                  persona_revision_snapshot IS NULL OR persona_source_snapshot IS NULL
                  OR persona_snapshot IS NULL OR persona_hash IS NULL
                  OR prompt_template_version IS NULL OR prompt_context IS NULL
                  OR json_type(prompt_context, '$.business_snapshot_v1') IS NULL
              )
            ORDER BY id LIMIT 50
            """
        )
    ).scalars()
    missing_ids = [int(item) for item in missing]
    if missing_ids:
        raise RuntimeError(
            "stage3 persona queued execution final check failed; "
            f"execution_ids={','.join(map(str, missing_ids))}"
        )


def _create_constraints(dialect: str) -> None:
    constraints = _COMMON_CONSTRAINTS + (
        _SQLITE_HASH_CONSTRAINTS if dialect == "sqlite" else _POSTGRES_HASH_CONSTRAINTS
    )
    if dialect == "sqlite":
        with op.batch_alter_table(_TABLE) as batch_op:
            for name, condition in constraints:
                batch_op.create_check_constraint(op.f(name), condition)
        return
    for name, condition in constraints:
        op.create_check_constraint(op.f(name), _TABLE, condition)


def upgrade() -> None:
    dialect = _dialect_name()
    if dialect == "sqlite":
        with op.batch_alter_table(_TABLE) as batch_op:
            for column in _columns():
                batch_op.add_column(column)
    else:
        for column in _columns():
            op.add_column(_TABLE, column)

    if dialect == "postgresql":
        _postgresql_precheck_and_backfill()
    elif dialect == "sqlite":
        _sqlite_precheck_and_backfill()
    _create_constraints(dialect)


def downgrade() -> None:
    dialect = _dialect_name()
    constraints = _COMMON_CONSTRAINTS + (
        _SQLITE_HASH_CONSTRAINTS if dialect == "sqlite" else _POSTGRES_HASH_CONSTRAINTS
    )
    columns = tuple(column.name for column in _columns())
    if dialect == "sqlite":
        with op.batch_alter_table(_TABLE) as batch_op:
            for name, _condition in reversed(constraints):
                batch_op.drop_constraint(op.f(name), type_="check")
            for column_name in reversed(columns):
                batch_op.drop_column(column_name)
        return
    for name, _condition in reversed(constraints):
        op.drop_constraint(op.f(name), _TABLE, type_="check")
    for column_name in reversed(columns):
        op.drop_column(_TABLE, column_name)
