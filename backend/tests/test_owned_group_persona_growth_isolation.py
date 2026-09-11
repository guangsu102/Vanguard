from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event

from app.core import automation_settings
from app.core.account import persona as persona_module
from app.core.account.models import (
    AccountOperationConfig,
    AccountStatus,
    AccountType,
    TelegramAccount,
)
from app.core.account.persona import PersonaV1, hash_persona
from app.core.group.models import Group
from app.modules.owned_group import messaging_content_service
from app.modules.owned_group import messaging_trigger_service as trigger_module
from app.modules.owned_group.messaging_models import GroupAccountMessagePolicy
from app.modules.owned_group.messaging_target import OwnedGroupMessageTarget
from app.modules.owned_group.messaging_trigger_service import (
    OwnedGroupMessageTriggerService,
)
from app.modules.owned_group.models import OwnedGroupAsset

BACKEND_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_FILES = (
    "app/api/account_personas.py",
    "app/core/account/persona.py",
    "app/modules/owned_group/messaging_content_service.py",
    "app/modules/owned_group/messaging_execution_service.py",
    "app/modules/owned_group/messaging_outbound_governance.py",
    "app/modules/owned_group/messaging_prompt_builder.py",
    "app/modules/owned_group/messaging_trigger_service.py",
)
FORBIDDEN_MODEL_SYMBOLS = frozenset(
    {
        "AdCampaign",
        "AccountAdBinding",
        "AdCreative",
        "AdDeliveryLog",
        "GroupAdProfile",
        "GroupAdPolicyEvent",
        "GroupAdOnlyAssessment",
        "GroupAdHandover",
        "GroupAdOnlyEvent",
    }
)
FORBIDDEN_TABLE_NAMES = frozenset(
    {
        "ad_campaign",
        "account_ad_binding",
        "ad_creative",
        "ad_delivery_log",
        "group_ad_profile",
        "group_ad_policy_event",
        "group_ad_only_assessment",
        "group_ad_handover",
        "group_ad_only_event",
    }
)
FORBIDDEN_IMPORT_PREFIXES = (
    "app.modules.acquisition.ad_only",
    "app.modules.acquisition.automation",
    "app.api.ad_only",
    "app.api.automation",
)


def _tree(relative_path: str) -> ast.Module:
    source = (BACKEND_ROOT / relative_path).read_text(encoding="utf-8")
    return ast.parse(source, filename=relative_path)


def test_persona_runtime_has_no_growth_ad_model_dependency() -> None:
    failures: list[str] = []
    for relative_path in RUNTIME_FILES:
        tree = _tree(relative_path)
        symbols = {
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name)
        } | {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
        }
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        forbidden = sorted((symbols | imported) & FORBIDDEN_MODEL_SYMBOLS)
        if forbidden:
            failures.append(f"{relative_path}: {', '.join(forbidden)}")
    assert not failures, "\n".join(failures)


def test_persona_runtime_has_no_growth_ad_import_edge() -> None:
    failures: list[str] = []
    for relative_path in RUNTIME_FILES:
        tree = _tree(relative_path)
        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
        forbidden = sorted(
            module
            for module in modules
            if module.startswith(FORBIDDEN_IMPORT_PREFIXES)
        )
        if forbidden:
            failures.append(f"{relative_path}: {', '.join(forbidden)}")
    assert not failures, "\n".join(failures)


def test_persona_runtime_has_no_direct_growth_ad_sql_literal() -> None:
    failures: list[str] = []
    for relative_path in RUNTIME_FILES:
        tree = _tree(relative_path)
        string_literals = {
            node.value.casefold()
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        hits = sorted(
            table_name
            for table_name in FORBIDDEN_TABLE_NAMES
            if any(table_name in literal for literal in string_literals)
        )
        if hits:
            failures.append(f"{relative_path}: {', '.join(hits)}")
    assert not failures, "\n".join(failures)


@pytest.mark.asyncio
async def test_persona_snapshot_execution_path_never_reads_growth_ad_state(
    test_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove the real snapshot/create path is isolated from Growth ad state."""

    persona = PersonaV1(
        schema_version=1,
        name="技术型群友",
        tone="自然、克制",
        interests=("网络稳定性",),
        expertise=("技术排障",),
        reply_length="short",
        preferred_topics=("使用体验",),
        forbidden_topics=("过度营销",),
        ad_style="neutral",
        catchphrases=(),
        language_style="zh_cn",
        system_prompt="",
    )
    account = TelegramAccount(
        phone="+15550008801",
        identifier="+15550008801",
        account_type=AccountType.PROMOTER,
        api_config_name="default",
        country_code="US",
        session_name="persona-growth-isolation",
        session_string="test-session",
        status=AccountStatus.ONLINE,
        is_active=True,
        ai_persona=persona.model_dump(mode="json"),
        ai_persona_revision=1,
        ai_persona_hash=hash_persona(persona),
    )
    test_db.add(account)
    await test_db.flush()
    test_db.add(
        AccountOperationConfig(
            account_id=account.id,
            operation_mode="growth",
            enabled=True,
        )
    )
    group = Group(group_id=-100000008801, title="Persona 动态隔离群")
    test_db.add(group)
    await test_db.flush()
    asset = OwnedGroupAsset(
        internal_name="persona-growth-isolation",
        telegram_chat_id=group.group_id,
        title=group.title,
        owner_account_id=account.id,
        core_group_id=group.id,
        status="ready",
    )
    test_db.add(asset)
    await test_db.flush()
    policy = GroupAccountMessagePolicy(
        owned_group_asset_id=asset.id,
        core_group_id=group.id,
        account_id=account.id,
        mode="ai",
        trigger_config={
            "manual": {
                "enabled": True,
                "allowed_content_categories": ["community"],
            },
            "dedupe_window_seconds": 21600,
        },
        promotion_config={"mode": "off"},
        daily_limit=10,
        cooldown_seconds=300,
        allowed_topics=["使用体验"],
        require_review=False,
        enabled=True,
    )
    test_db.add(policy)
    await test_db.commit()

    expected_accessors = {
        "get_ad_capacity_settings",
        "get_ad_delivery_execution_settings",
        "get_ad_delivery_throttle_settings",
        "get_ad_failure_policy_settings",
        "get_ad_only_recommendation_settings",
    }
    accessor_names = {
        name
        for name, value in vars(automation_settings).items()
        if name.startswith("get_ad_") and callable(value)
    }
    assert expected_accessors <= accessor_names
    ad_accessors: dict[str, AsyncMock] = {}
    runtime_modules = (
        persona_module,
        trigger_module,
        messaging_content_service,
    )
    for name in sorted(accessor_names):
        spy = AsyncMock(side_effect=AssertionError(f"unexpected Growth accessor: {name}"))
        ad_accessors[name] = spy
        monkeypatch.setattr(automation_settings, name, spy)
        for module in runtime_modules:
            if name in vars(module):
                monkeypatch.setattr(module, name, spy)

    persona_settings = AsyncMock(return_value={"effectiveEnabled": True})
    monkeypatch.setattr(
        trigger_module,
        "get_owned_group_ai_persona_settings",
        persona_settings,
    )

    statements: list[str] = []

    def capture_statement(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        statements.append(statement.casefold())

    engine = test_db.bind
    assert engine is not None
    event.listen(engine.sync_engine, "before_cursor_execute", capture_statement)
    try:
        execution, created = await OwnedGroupMessageTriggerService(
            test_db
        ).create_execution(
            target=OwnedGroupMessageTarget(
                owned_group_asset_id=asset.id,
                core_group_id=group.id,
                telegram_chat_id=group.group_id,
                managed_binding_id=1,
                group_title=group.title,
            ),
            policy=policy,
            trigger_type="manual",
            content_category="community",
            idempotency_key="persona-growth-isolation-execution",
            correlation_id="persona-growth-isolation-correlation",
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture_statement)

    assert created is True
    assert execution.persona_source_snapshot == "configured"
    assert execution.persona_hash == hash_persona(persona)
    assert persona_settings.await_count == 1
    for spy in ad_accessors.values():
        spy.assert_not_awaited()

    sql_text = "\n".join(statements)
    assert "group_account_message_execution" in sql_text
    assert "telegram_account" in sql_text
    assert "telegram_account_operation_config" in sql_text
    ad_table_hits = sorted(
        table_name for table_name in FORBIDDEN_TABLE_NAMES if table_name in sql_text
    )
    assert ad_table_hits == []
