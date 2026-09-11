from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.modules.owned_group.messaging_content_service import (
    OwnedGroupMessageContentService,
)
from app.modules.owned_group.messaging_contracts import OwnedGroupMessagingError
from app.modules.owned_group.messaging_execution_service import (
    OwnedGroupMessageExecutionService,
)


def _execution(*, source: str | None, mode: str = "ai") -> SimpleNamespace:
    return SimpleNamespace(
        trigger_type="manual",
        content_category="community",
        template_id=None,
        topic=None,
        mode_snapshot=mode,
        persona_source_snapshot=source,
        prompt_context={},
    )


def _context() -> dict[str, object]:
    return {
        "business_snapshot_v1": {"allowed_topics": [], "group_title": "测试群"},
        "source_text": "普通上下文",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["configured", "neutral_default"])
async def test_strict_persona_sources_fail_before_external_generation(source: str) -> None:
    service = OwnedGroupMessageContentService(AsyncMock())
    service.generate = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(OwnedGroupMessagingError) as exc_info:
        await service.generate_for_execution(
            _execution(source=source),
            SimpleNamespace(),
            SimpleNamespace(),
            prompt_context=_context(),
        )

    assert exc_info.value.code == "AI_PROVIDER_UNSAFE"
    assert exc_info.value.http_status == 503
    service.generate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["feature_disabled_default", "legacy_default"])
async def test_compatibility_sources_keep_stage_two_generator(source: str) -> None:
    expected = object()
    service = OwnedGroupMessageContentService(AsyncMock())
    service.generate = AsyncMock(return_value=expected)  # type: ignore[method-assign]

    result = await service.generate_for_execution(
        _execution(source=source),
        SimpleNamespace(),
        SimpleNamespace(),
        prompt_context=_context(),
    )

    assert result is expected
    service.generate.assert_awaited_once()


@pytest.mark.asyncio
async def test_ai_execution_without_business_snapshot_fails_before_generation() -> None:
    service = OwnedGroupMessageContentService(AsyncMock())
    service.generate = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(OwnedGroupMessagingError) as exc_info:
        await service.generate_for_execution(
            _execution(source="legacy_default"),
            SimpleNamespace(),
            SimpleNamespace(),
            prompt_context={},
        )

    assert exc_info.value.code == "EXECUTION_BUSINESS_SNAPSHOT_MISSING"
    service.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_compatibility_generation_uses_frozen_business_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.modules.owned_group import messaging_content_service as content_module

    monkeypatch.setattr(
        content_module,
        "get_group_ai_interaction_settings",
        AsyncMock(return_value={"replyMaxChars": 120}),
    )
    db = AsyncMock()
    service = OwnedGroupMessageContentService(db)
    service._generate_ai = AsyncMock(return_value="自然中文回复")  # type: ignore[method-assign]
    execution = _execution(source="legacy_default")
    execution.topic = "冻结话题"
    policy = SimpleNamespace(
        id=9,
        account_id=17,
        mode="ai",
        allowed_topics=["已修改的当前话题"],
        require_review=False,
        promotion_config={"mode": "off"},
    )
    target = SimpleNamespace(
        owned_group_asset_id=8,
        core_group_id=7,
        telegram_chat_id=-1007,
    )

    result = await service.generate_for_execution(
        execution,
        policy,
        target,
        prompt_context={
            "business_snapshot_v1": {
                "allowed_topics": ["冻结话题"],
                "group_title": "冻结群名",
            }
        },
    )

    assert result.content == "自然中文回复"
    assert result.topic == "冻结话题"
    generation_args = service._generate_ai.await_args.kwargs
    assert generation_args["group_name"] == "冻结群名"
    assert generation_args["allowed_topics"] == ("冻结话题",)
    assert generation_args["topic"] == "冻结话题"
    db.get.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "snapshot",
    [
        {"allowed_topics": ("话题",), "group_title": "测试群"},
        {"allowed_topics": ["重复", "重复"], "group_title": "测试群"},
        {"allowed_topics": [""], "group_title": "测试群"},
        {"allowed_topics": [], "group_title": 7},
    ],
)
async def test_invalid_business_snapshot_never_falls_back_to_current_records(
    snapshot: dict[str, object],
) -> None:
    db = AsyncMock()
    service = OwnedGroupMessageContentService(db)
    service._generate_ai = AsyncMock()  # type: ignore[method-assign]
    policy = SimpleNamespace(
        id=9,
        account_id=17,
        mode="ai",
        allowed_topics=["当前话题"],
        require_review=False,
        promotion_config={"mode": "off"},
    )

    with pytest.raises(OwnedGroupMessagingError) as exc_info:
        await service.generate_for_execution(
            _execution(source="feature_disabled_default"),
            policy,
            SimpleNamespace(core_group_id=7),
            prompt_context={"business_snapshot_v1": snapshot},
        )

    assert exc_info.value.code == "EXECUTION_BUSINESS_SNAPSHOT_INVALID"
    service._generate_ai.assert_not_awaited()
    db.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_ai_execution_without_known_persona_source_fails_closed() -> None:
    service = OwnedGroupMessageContentService(AsyncMock())
    service.generate = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(OwnedGroupMessagingError) as exc_info:
        await service.generate_for_execution(
            _execution(source=None),
            SimpleNamespace(),
            SimpleNamespace(),
            prompt_context=_context(),
        )

    assert exc_info.value.code == "PERSONA_CONFIG_INVALID"
    service.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_template_execution_never_enters_persona_gate() -> None:
    expected = object()
    service = OwnedGroupMessageContentService(AsyncMock())
    service.generate = AsyncMock(return_value=expected)  # type: ignore[method-assign]

    result = await service.generate_for_execution(
        _execution(source=None, mode="template"),
        SimpleNamespace(),
        SimpleNamespace(),
        prompt_context={},
    )

    assert result is expected
    service.generate.assert_awaited_once()


@pytest.mark.asyncio
async def test_ai_community_final_validation_does_not_require_promotion_composition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.modules.owned_group import messaging_content_service as content_module

    monkeypatch.setattr(
        content_module,
        "get_group_ai_interaction_settings",
        AsyncMock(return_value={"replyMaxChars": 120}),
    )
    service = OwnedGroupMessageContentService(AsyncMock())

    result = await service.validate_final_content(
        "自然中文回复",
        content_category="community",
        mode_snapshot="ai",
    )

    assert result.content == "自然中文回复"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "promotion_config"),
    [
        (
            "活动说明\n查看详情",
            {
                "cta_text": "查看详情",
                "destination_url": "https://example.com/join",
            },
        ),
        (
            "活动说明\n查看详情\nhttps://example.com/join\nhttps://example.com/join",
            {
                "cta_text": "查看详情",
                "destination_url": "https://example.com/join",
            },
        ),
        (
            "活动说明\nhttps://example.com/join",
            {
                "cta_text": "查看详情",
                "destination_url": "https://example.com/join",
            },
        ),
        (
            "活动说明\n查看详情\n查看详情\nhttps://example.com/join",
            {
                "cta_text": "查看详情",
                "destination_url": "https://example.com/join",
            },
        ),
        (
            "活动说明\n查看详情",
            {"cta_text": "查看详情"},
        ),
        (
            "活动说明\nhttps://example.com/join",
            {"destination_url": "https://example.com/join"},
        ),
    ],
)
async def test_ai_promotion_final_validation_requires_exact_locked_composition(
    monkeypatch: pytest.MonkeyPatch,
    content: str,
    promotion_config: dict[str, str],
) -> None:
    from app.modules.owned_group import messaging_content_service as content_module

    monkeypatch.setattr(
        content_module,
        "get_group_ai_interaction_settings",
        AsyncMock(return_value={"replyMaxChars": 500}),
    )
    service = OwnedGroupMessageContentService(AsyncMock())

    with pytest.raises(OwnedGroupMessagingError) as exc_info:
        await service.validate_final_content(
            content,
            content_category="promotion",
            promotion_config=promotion_config,
            mode_snapshot="ai",
        )

    assert exc_info.value.code == "PROMOTION_COMPOSITION_INVALID"


@pytest.mark.asyncio
async def test_ai_promotion_generation_appends_locked_composition_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.modules.owned_group import messaging_content_service as content_module

    monkeypatch.setattr(
        content_module,
        "get_group_ai_interaction_settings",
        AsyncMock(return_value={"replyMaxChars": 500}),
    )
    db = AsyncMock()
    service = OwnedGroupMessageContentService(db)
    service._generate_ai = AsyncMock(return_value="本周活动说明")  # type: ignore[method-assign]
    policy = SimpleNamespace(
        id=9,
        account_id=17,
        mode="off",
        allowed_topics=["已修改的当前活动"],
        require_review=False,
        promotion_config={
            "mode": "ai",
            "cta_text": "查看详情",
            "destination_url": "https://example.com/join",
        },
    )

    result = await service.generate(
        target=SimpleNamespace(
            owned_group_asset_id=8,
            core_group_id=7,
            telegram_chat_id=-1007,
        ),
        policy=policy,
        trigger_type="manual",
        content_category="promotion",
        topic="冻结活动",
        business_snapshot_v1={
            "allowed_topics": ["冻结活动"],
            "group_title": "冻结群名",
        },
    )

    assert result.content == "本周活动说明\n查看详情\nhttps://example.com/join"
    assert result.would_require_review is True
    generation_args = service._generate_ai.await_args.kwargs
    assert generation_args["group_name"] == "冻结群名"
    assert generation_args["allowed_topics"] == ("冻结活动",)
    db.get.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("generated_body", "expected_code"),
    [
        ("本周活动说明\n查看详情", "PROMOTION_COMPOSITION_INVALID"),
        ("本周活动说明\nhttps://example.com/join", "PROMOTION_URL_INVALID"),
    ],
)
async def test_ai_promotion_body_cannot_supply_locked_composition(
    monkeypatch: pytest.MonkeyPatch,
    generated_body: str,
    expected_code: str,
) -> None:
    from app.modules.owned_group import messaging_content_service as content_module

    monkeypatch.setattr(
        content_module,
        "get_group_ai_interaction_settings",
        AsyncMock(return_value={"replyMaxChars": 500}),
    )
    service = OwnedGroupMessageContentService(AsyncMock())
    service._generate_ai = AsyncMock(  # type: ignore[method-assign]
        return_value=generated_body
    )
    policy = SimpleNamespace(
        id=9,
        account_id=17,
        mode="off",
        allowed_topics=[],
        require_review=False,
        promotion_config={
            "mode": "ai",
            "cta_text": "查看详情",
            "destination_url": "https://example.com/join",
        },
    )

    with pytest.raises(OwnedGroupMessagingError) as exc_info:
        await service.generate(
            target=SimpleNamespace(core_group_id=7),
            policy=policy,
            trigger_type="manual",
            content_category="promotion",
            business_snapshot_v1={
                "allowed_topics": [],
                "group_title": "冻结群名",
            },
        )

    assert exc_info.value.code == expected_code


@pytest.mark.asyncio
async def test_generation_success_persists_prompt_and_governance_hashes_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.modules.owned_group import messaging_execution_service as execution_module

    @asynccontextmanager
    async def unlocked(*_args: object, **_kwargs: object):
        yield

    prompt_hash = "a" * 64
    governance_hash = "b" * 64
    execution = SimpleNamespace(
        id=71,
        policy_id=9,
        owned_group_asset_id=8,
        core_group_id=7,
        telegram_chat_id=-1007,
        account_id=17,
        trigger_type="manual",
        content_category="community",
        mode_snapshot="ai",
        status="queued",
        scheduled_at=None,
        next_retry_at=None,
        lease_id=None,
        lease_expires_at=None,
        revision=1,
        prompt_hash=None,
        governance_rules_hash=None,
        prompt_context={
            "generation_policy_snapshot": {
                "mode": "ai",
                "require_review": False,
                "allowed_topics": [],
                "default_template_id": None,
            },
            "business_snapshot_v1": {"allowed_topics": [], "group_title": "测试群"},
        },
        promotion_config_snapshot=None,
        content=None,
        content_hash=None,
        requested_by=1,
        correlation_id="corr-71",
        error_code=None,
        error_message=None,
    )
    generated = SimpleNamespace(
        content="安全正文",
        content_hash="c" * 64,
        would_require_review=False,
        prompt_hash=prompt_hash,
        governance_rules_hash=governance_hash,
    )
    db = AsyncMock()
    db.add = AsyncMock()
    db.scalar.side_effect = [execution, None]
    db.get.return_value = execution
    content_service = SimpleNamespace(generate_for_execution=AsyncMock(return_value=generated))
    prompt_store = SimpleNamespace(discard=AsyncMock(return_value=True))
    service = OwnedGroupMessageExecutionService(
        db,
        account_pool=SimpleNamespace(),
        content_service=content_service,
        speaker=SimpleNamespace(),
        prompt_context_store=prompt_store,
    )
    service._load_policy = AsyncMock(
        return_value=SimpleNamespace(trigger_config={}, mode="ai")
    )
    service._policy_gate = AsyncMock()
    service.resolver.resolve = AsyncMock(
        return_value=SimpleNamespace(telegram_chat_id=-1007)
    )
    monkeypatch.setattr(execution_module, "telegram_chat_advisory_lock", unlocked)
    monkeypatch.setattr(
        execution_module,
        "get_owned_group_messaging_settings",
        AsyncMock(return_value={}),
    )

    result = await service.prepare_execution(execution.id)

    assert result is execution
    assert execution.status == "ready_to_send"
    assert execution.prompt_hash == prompt_hash
    assert execution.governance_rules_hash == governance_hash
    assert execution.content == "安全正文"
    assert db.commit.await_count == 2
