import json
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event

from app.core.account.models import (
    AccountOperationConfig,
    AccountStatus,
    AccountType,
    TelegramAccount,
)
from app.core.account.persona import PersonaV1, hash_persona
from app.modules.owned_group import messaging_policy_service as policy_service_module
from app.modules.owned_group.messaging_models import GroupAccountMessageExecution
from app.modules.owned_group.messaging_policy_service import (
    OwnedGroupMessagingPolicyService,
)


def _persona(*, name: str, system_prompt: str = "") -> PersonaV1:
    return PersonaV1(
        schema_version=1,
        name=name,
        tone="自然、克制",
        interests=(),
        expertise=("技术排障",),
        reply_length="short",
        preferred_topics=(),
        forbidden_topics=(),
        ad_style="neutral",
        catchphrases=(),
        language_style="zh_cn",
        system_prompt=system_prompt,
    )


def _account(
    suffix: str,
    *,
    persona: dict[str, Any] | None,
    revision: int,
    persona_hash: str | None,
) -> TelegramAccount:
    return TelegramAccount(
        identifier=f"persona-summary-{suffix}",
        session_name=f"persona-summary-{suffix}",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
        session_string="test-session",
        ai_persona=persona,
        ai_persona_revision=revision,
        ai_persona_hash=persona_hash,
        ai_persona_updated_at=None,
        ai_persona_updated_by=None,
    )


@pytest.mark.asyncio
async def test_policy_persona_summaries_batch_load_accounts_and_configs_once(test_db) -> None:
    canary = "CANARY_PERSONA_SYSTEM_PROMPT_MUST_NOT_LEAK"
    valid_persona = _persona(name="技术型群友", system_prompt=canary)
    valid = _account(
        "valid",
        persona=valid_persona.model_dump(mode="json"),
        revision=3,
        persona_hash=hash_persona(valid_persona),
    )
    corrupt = _account(
        "corrupt",
        persona={"schema_version": 999, "system_prompt": canary},
        revision=4,
        persona_hash="b" * 64,
    )
    missing_config = _account(
        "missing-config",
        persona=None,
        revision=0,
        persona_hash=None,
    )
    test_db.add_all([valid, corrupt, missing_config])
    await test_db.flush()
    test_db.add_all(
        [
            AccountOperationConfig(
                account_id=valid.id,
                operation_mode="growth",
                enabled=True,
            ),
            AccountOperationConfig(
                account_id=corrupt.id,
                operation_mode="growth",
                enabled=True,
            ),
        ]
    )
    await test_db.commit()

    statements: list[str] = []

    def capture_statement(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        statements.append(statement)

    engine = test_db.bind
    assert engine is not None
    event.listen(engine.sync_engine, "before_cursor_execute", capture_statement)
    try:
        summaries = await OwnedGroupMessagingPolicyService(test_db)._policy_persona_summaries(
            {valid.id, corrupt.id, missing_config.id},
            effective_enabled=True,
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture_statement)

    assert len(statements) == 1
    assert "telegram_account_operation_config" in statements[0]
    assert summaries[valid.id] == {
        "account_id": valid.id,
        "configured": True,
        "name": "技术型群友",
        "revision": 3,
        "applicable": True,
        "effective_enabled": True,
    }
    assert summaries[corrupt.id] == {
        "account_id": corrupt.id,
        "configured": True,
        "name": None,
        "revision": 4,
        "applicable": False,
        "effective_enabled": True,
    }
    assert summaries[missing_config.id]["configured"] is False
    assert summaries[missing_config.id]["applicable"] is False
    assert canary not in json.dumps(summaries, ensure_ascii=False)


def _execution(
    *,
    mode: str,
    persona: PersonaV1 | None = None,
) -> GroupAccountMessageExecution:
    now = datetime.utcnow()
    execution = GroupAccountMessageExecution(
        policy_id=15,
        owned_group_asset_id=901,
        core_group_id=801,
        telegram_chat_id=-1000000000901,
        account_id=7,
        trigger_type="manual",
        message_purpose="community_ai" if mode == "ai" else "template",
        content_category="community",
        mode_snapshot=mode,
        policy_revision=1,
        status="sent",
        idempotency_key=f"persona-summary-{mode}",
        correlation_id=f"persona-summary-{mode}",
        attempt_count=0,
        revision=1,
        created_at=now,
        updated_at=now,
        sent_at=now,
    )
    execution.id = 88
    if persona is not None:
        execution.persona_source_snapshot = "configured"
        execution.persona_revision_snapshot = 3
        execution.persona_snapshot = persona.model_dump(mode="json")
        execution.persona_hash = hash_persona(persona)
        execution.prompt_template_version = "owned-group-persona-v1"
        execution.prompt_hash = "1" * 64
        execution.governance_rules_hash = "2" * 64
    return execution


@pytest.mark.asyncio
async def test_execution_response_uses_only_frozen_persona_summary(monkeypatch) -> None:
    canary = "CANARY_FROZEN_SYSTEM_PROMPT_MUST_NOT_LEAK"
    persona = _persona(name="冻结技术群友", system_prompt=canary)
    execution = _execution(mode="ai", persona=persona)
    monkeypatch.setattr(
        policy_service_module,
        "get_owned_group_messaging_settings",
        AsyncMock(return_value={"reviewTtlHours": 24}),
    )

    response = await OwnedGroupMessagingPolicyService(AsyncMock())._execution_response(
        execution,
        include_content=False,
        include_sensitive=False,
    )

    assert response["persona"] == {
        "source": "configured",
        "name": "冻结技术群友",
        "revision": 3,
        "hash_prefix": hash_persona(persona)[:12],
    }
    assert response["prompt_template_version"] == "owned-group-persona-v1"
    assert response["prompt_hash_prefix"] == "1" * 12
    assert response["governance_rules_hash_prefix"] == "2" * 12
    assert canary not in json.dumps(response, ensure_ascii=False)


@pytest.mark.asyncio
async def test_execution_response_marks_untracked_ai_history(monkeypatch) -> None:
    monkeypatch.setattr(
        policy_service_module,
        "get_owned_group_messaging_settings",
        AsyncMock(return_value={"reviewTtlHours": 24}),
    )

    response = await OwnedGroupMessagingPolicyService(AsyncMock())._execution_response(
        _execution(mode="ai"),
        include_content=False,
        include_sensitive=False,
    )

    assert response["persona"] == {
        "source": "legacy_untracked",
        "name": None,
        "revision": None,
        "hash_prefix": None,
    }
    assert response["prompt_template_version"] is None
    assert response["prompt_hash_prefix"] is None
    assert response["governance_rules_hash_prefix"] is None


@pytest.mark.asyncio
async def test_template_execution_hides_all_persona_and_prompt_metadata(monkeypatch) -> None:
    canary = "CANARY_TEMPLATE_MUST_NOT_DISPLAY_PERSONA"
    execution = _execution(mode="template")
    execution.persona_source_snapshot = "configured"
    execution.persona_revision_snapshot = 9
    execution.persona_snapshot = _persona(name=canary).model_dump(mode="json")
    execution.persona_hash = "a" * 64
    execution.prompt_template_version = "poisoned-template-version"
    execution.prompt_hash = "b" * 64
    execution.governance_rules_hash = "c" * 64
    monkeypatch.setattr(
        policy_service_module,
        "get_owned_group_messaging_settings",
        AsyncMock(return_value={"reviewTtlHours": 24}),
    )

    response = await OwnedGroupMessagingPolicyService(AsyncMock())._execution_response(
        execution,
        include_content=False,
        include_sensitive=False,
    )

    assert response["persona"] is None
    assert response["prompt_template_version"] is None
    assert response["prompt_hash_prefix"] is None
    assert response["governance_rules_hash_prefix"] is None
    assert canary not in json.dumps(response, ensure_ascii=False)
