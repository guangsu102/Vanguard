from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.modules.acquisition.auto_reply.speaker import SpeakResult
from app.modules.owned_group.messaging_content_service import (
    OwnedGroupMessageContentService,
)
from app.modules.owned_group.messaging_contracts import OwnedGroupMessagingError
from app.modules.owned_group.messaging_execution_service import (
    OwnedGroupMessageExecutionService,
)
from app.modules.owned_group.messaging_outbound_governance import (
    OutboundGovernanceDecision,
)
from app.modules.owned_group.messaging_policy_service import (
    OwnedGroupMessagingPolicyService,
)
from app.modules.owned_group.messaging_schemas import ExecutionApprove


def _strict_execution(*, status: str = "pending_review") -> SimpleNamespace:
    return SimpleNamespace(
        id=81,
        policy_id=8,
        owned_group_asset_id=18,
        core_group_id=28,
        telegram_chat_id=-10028,
        account_id=38,
        trigger_type="manual",
        source_message_id=None,
        reply_to_message_id=None,
        message_purpose="community_ai",
        content_category="community",
        mode_snapshot="ai",
        persona_source_snapshot="configured",
        persona_snapshot={
            "schema_version": 1,
            "name": "专业顾问",
            "tone": "专业克制",
            "reply_length": "medium",
            "emoji_style": "none",
            "language": "zh-CN",
            "catchphrases": [],
            "forbidden_topics": [],
            "ad_style": "soft",
            "system_prompt": "使用专业而克制的表达方式。",
        },
        governance_rules_hash="a" * 64,
        promotion_config_snapshot=None,
        status=status,
        content="审核前正文",
        content_hash="b" * 64,
        prompt_hash="c" * 64,
        scheduled_at=None,
        next_retry_at=None,
        lease_id=None,
        lease_expires_at=None,
        write_started_at=None,
        attempt_count=0,
        revision=4,
        requested_by=3,
        correlation_id="corr-81",
        telegram_message_id=None,
        sent_at=None,
        error_code=None,
        error_message=None,
        prompt_context={},
    )


@pytest.mark.asyncio
async def test_review_override_runs_stage3_governance_and_audits_hashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.modules.owned_group import messaging_policy_service as policy_module

    execution = _strict_execution()
    db = AsyncMock()
    db.add = MagicMock()
    db.scalar.return_value = None
    db.execute.return_value = SimpleNamespace(rowcount=1)
    service = OwnedGroupMessagingPolicyService(db)
    service.get_execution = AsyncMock(return_value=execution)  # type: ignore[method-assign]
    service.get_execution_response = AsyncMock(  # type: ignore[method-assign]
        return_value={"execution_id": execution.id}
    )
    service._add_audit = MagicMock()  # type: ignore[method-assign]
    validated = SimpleNamespace(content="审核后安全正文", content_hash="d" * 64)
    validate_final = AsyncMock(return_value=validated)
    monkeypatch.setattr(
        OwnedGroupMessageContentService,
        "validate_final_content",
        validate_final,
    )
    review_governance = AsyncMock(
        return_value=SimpleNamespace(governance_rules_hash="e" * 64)
    )
    monkeypatch.setattr(
        policy_module,
        "govern_review_override",
        review_governance,
    )
    monkeypatch.setattr(
        policy_module,
        "get_owned_group_messaging_settings",
        AsyncMock(return_value={"contentDedupeWindowSeconds": 21600}),
    )

    result = await service._approve_execution_locked(
        execution.owned_group_asset_id,
        execution.id,
        ExecutionApprove(revision=4, content_override="审核后安全正文"),
        actor={"id": 99},
        correlation_id="review-corr",
    )

    assert result == {"execution_id": execution.id}
    review_governance.assert_awaited_once_with(
        db=db,
        execution=execution,
        text=validated.content,
    )
    assert execution.prompt_hash == "c" * 64
    audit = service._add_audit.call_args.kwargs
    before = audit["before_state"]
    after = audit["after_state"]
    assert before["content_hash"] == "b" * 64
    assert after["content_hash"] == "d" * 64
    assert after["generation_governance_rules_hash"] == "a" * 64
    assert after["review_governance_rules_hash"] == "e" * 64


@pytest.mark.asyncio
async def test_review_governance_block_keeps_execution_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.modules.owned_group import messaging_policy_service as policy_module

    execution = _strict_execution()
    db = AsyncMock()
    db.add = MagicMock()
    service = OwnedGroupMessagingPolicyService(db)
    service.get_execution = AsyncMock(return_value=execution)  # type: ignore[method-assign]
    service._add_audit = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr(
        OwnedGroupMessageContentService,
        "validate_final_content",
        AsyncMock(
            return_value=SimpleNamespace(
                content="命中禁区的编辑正文",
                content_hash="d" * 64,
            )
        ),
    )
    monkeypatch.setattr(
        policy_module,
        "govern_review_override",
        AsyncMock(
            side_effect=OwnedGroupMessagingError(
                "CONTENT_POLICY_BLOCKED",
                "审核内容未通过治理",
            )
        ),
    )

    with pytest.raises(OwnedGroupMessagingError) as exc_info:
        await service._approve_execution_locked(
            execution.owned_group_asset_id,
            execution.id,
            ExecutionApprove(revision=4, content_override="命中禁区的编辑正文"),
            actor={"id": 99},
            correlation_id="review-corr",
        )

    assert exc_info.value.code == "CONTENT_POLICY_BLOCKED"
    assert execution.status == "pending_review"
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()
    service._add_audit.assert_not_called()


@pytest.mark.asyncio
async def test_send_gate_returns_latest_governance_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.modules.owned_group import messaging_execution_service as execution_module

    execution = _strict_execution(status="ready_to_send")
    policy = SimpleNamespace(
        trigger_config={},
        daily_limit=20,
        cooldown_seconds=0,
    )
    db = AsyncMock()
    db.get.return_value = SimpleNamespace(id=execution.account_id)
    db.scalar.side_effect = [0, 0, 0, None]
    service = OwnedGroupMessageExecutionService(
        db,
        account_pool=MagicMock(),
        content_service=MagicMock(),
        speaker=MagicMock(),
    )
    service._policy_gate = AsyncMock()  # type: ignore[method-assign]
    service._duplicate_execution_id = AsyncMock(return_value=None)  # type: ignore[method-assign]
    service.resolver.validate_account_eligibility = AsyncMock(
        return_value=SimpleNamespace(blocking_reasons=())
    )
    service.content_service.validate_final_content = AsyncMock(
        return_value=SimpleNamespace(
            content=execution.content,
            content_hash=execution.content_hash,
        )
    )
    send_governance = AsyncMock(
        return_value=SimpleNamespace(governance_rules_hash="f" * 64)
    )
    monkeypatch.setattr(execution_module, "govern_before_send", send_governance)
    monkeypatch.setattr(
        execution_module,
        "get_group_ai_interaction_settings",
        AsyncMock(return_value={"enabled": True}),
    )

    result = await service._check_send_gates(
        execution,
        policy,
        {
            "globalMaxPerGroupPerDay": 100,
            "globalMaxPerAccountPerDay": 100,
            "minGroupCooldownSeconds": 0,
            "contentDedupeWindowSeconds": 21600,
        },
    )

    assert result == "f" * 64
    send_governance.assert_awaited_once_with(
        db=db,
        execution=execution,
        text=execution.content,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("send_hash", "expected_code"),
    [
        ("a" * 64, "CONTENT_POLICY_BLOCKED"),
        ("f" * 64, "CONTENT_POLICY_CHANGED"),
    ],
)
async def test_send_governance_distinguishes_changed_rules(
    monkeypatch: pytest.MonkeyPatch,
    send_hash: str,
    expected_code: str,
) -> None:
    from app.modules.owned_group import messaging_governance_integration as integration

    execution = _strict_execution(status="ready_to_send")
    monkeypatch.setattr(
        integration,
        "_evaluate",
        AsyncMock(
            return_value=OutboundGovernanceDecision(
                allowed=False,
                reason_code="PERSONA_FORBIDDEN_TOPIC_MATCHED",
                matched_term_hashes=("1" * 64,),
                governance_rules_hash=send_hash,
            )
        ),
    )

    with pytest.raises(OwnedGroupMessagingError) as exc_info:
        await integration.govern_before_send(
            db=AsyncMock(),
            execution=execution,
            text=execution.content,
        )

    assert exc_info.value.code == expected_code
    assert (
        exc_info.value.details["generation_governance_rules_hash"]
        == execution.governance_rules_hash
    )
    assert exc_info.value.details["send_governance_rules_hash"] == send_hash


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "persona_source",
    ["feature_disabled_default", "legacy_default"],
)
async def test_compatibility_persona_sources_bypass_stage3_review_and_send_governance(
    monkeypatch: pytest.MonkeyPatch,
    persona_source: str,
) -> None:
    from app.modules.owned_group import messaging_governance_integration as integration

    execution = _strict_execution(status="ready_to_send")
    execution.persona_source_snapshot = persona_source
    evaluate = AsyncMock()
    monkeypatch.setattr(integration, "_evaluate", evaluate)

    review = await integration.govern_review_override(
        db=AsyncMock(),
        execution=execution,
        text=execution.content,
    )
    send = await integration.govern_before_send(
        db=AsyncMock(),
        execution=execution,
        text=execution.content,
    )

    assert review is None
    assert send is None
    evaluate.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_policy_skip_audit_contains_only_governance_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.modules.owned_group import messaging_execution_service as execution_module

    @asynccontextmanager
    async def unlocked(*_args: object, **_kwargs: object):
        yield

    execution = _strict_execution(status="ready_to_send")
    db = AsyncMock()
    db.add = MagicMock()
    db.get.return_value = execution
    db.scalar.return_value = execution
    speaker = SimpleNamespace(send_owned_group_message=AsyncMock())
    prompt_store = SimpleNamespace(discard=AsyncMock(return_value=True))
    service = OwnedGroupMessageExecutionService(
        db,
        account_pool=MagicMock(),
        content_service=MagicMock(),
        speaker=speaker,
        prompt_context_store=prompt_store,
    )
    service.resolver.resolve = AsyncMock(
        return_value=SimpleNamespace(
            core_group_id=execution.core_group_id,
            telegram_chat_id=execution.telegram_chat_id,
        )
    )
    service._load_policy = AsyncMock(return_value=SimpleNamespace())  # type: ignore[method-assign]
    service._check_send_gates = AsyncMock(  # type: ignore[method-assign]
        side_effect=OwnedGroupMessagingError(
            "CONTENT_POLICY_CHANGED",
            "群治理规则变化后阻止发送",
            details={
                "generation_governance_rules_hash": "a" * 64,
                "send_governance_rules_hash": "f" * 64,
                "governance_reason_code": "CONTENT_POLICY_BLOCKED",
                "matched_rule_ids": [17],
                "matched_term_hashes": ["1" * 64],
                "unsafe_raw_text": "must-not-audit",
            },
        )
    )
    service._add_audit = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr(
        execution_module,
        "telegram_chat_advisory_lock",
        unlocked,
    )
    monkeypatch.setattr(
        execution_module,
        "telegram_account_advisory_lock",
        unlocked,
    )
    monkeypatch.setattr(
        execution_module,
        "get_owned_group_messaging_settings",
        AsyncMock(return_value={"enabled": True, "dryRun": False}),
    )
    monkeypatch.setattr(
        execution_module.settings,
        "OWNED_GROUP_MESSAGING_ENABLED",
        True,
    )

    result = await service.send_execution(execution.id)

    assert result is execution
    assert execution.status == "skipped"
    assert execution.error_code == "CONTENT_POLICY_CHANGED"
    speaker.send_owned_group_message.assert_not_awaited()
    after = service._add_audit.call_args.kwargs["after"]
    assert after["generation_governance_rules_hash"] == "a" * 64
    assert after["send_governance_rules_hash"] == "f" * 64
    assert after["matched_rule_ids"] == [17]
    assert "unsafe_raw_text" not in after


@pytest.mark.asyncio
async def test_sent_audit_records_generation_and_send_governance_hashes() -> None:
    execution = _strict_execution(status="sending")
    execution.lease_id = "lease-81"
    execution.write_started_at = object()
    db = AsyncMock()
    db.add = MagicMock()
    db.get.return_value = execution
    db.scalar.return_value = 1
    service = OwnedGroupMessageExecutionService(
        db,
        account_pool=MagicMock(),
        content_service=MagicMock(),
        speaker=MagicMock(),
    )
    service._add_audit = MagicMock()  # type: ignore[method-assign]

    result = await service._finalize_send(
        execution_id=execution.id,
        lease_id="lease-81",
        result=SpeakResult(success=True, message_id=981),
        runtime={"maxSendAttempts": 3},
        send_governance_rules_hash="f" * 64,
    )

    assert result is execution
    after = service._add_audit.call_args.kwargs["after"]
    assert after["generation_governance_rules_hash"] == "a" * 64
    assert after["send_governance_rules_hash"] == "f" * 64
    assert after["telegram_message_id"] == 981
