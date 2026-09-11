from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from app.core import database as database_module
from app.core import persona_observability as observability_module
from app.core.account.persona import NEUTRAL_PERSONA
from app.core.ai import llm_client as llm_module
from app.core.ai.llm_client import LLMCacheContext, LLMClient, LLMProvider
from app.core.persona_observability import (
    LOGICAL_METRIC_NAMES,
    SAFE_STRUCTURED_EVENT_FIELDS,
    STRUCTURED_EVENT_NAMES,
    observe_prompt_build_call,
    record_growth_ad_config_access,
    record_llm_usage,
    record_outbound_evaluation,
    record_persona_account_mismatch,
    record_persona_configured,
    record_persona_preview,
    record_persona_resolve,
    record_persona_snapshot,
    record_persona_update,
    refresh_persona_configured_gauge,
    refresh_persona_configured_gauge_at_startup,
)
from app.modules.owned_group.messaging_contracts import OwnedGroupMessagingError
from app.modules.owned_group.messaging_outbound_governance import (
    OutboundGovernanceDecision,
)

_PERSONA_HASH = "a" * 64
_PROMPT_HASH = "b" * 64
_GOVERNANCE_HASH = "c" * 64


class RecordingLogger:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def info(self, event: str, **fields: Any) -> None:
        self.calls.append((event, fields))


class ExplodingLogger:
    def info(self, event: str, **fields: Any) -> None:
        del event, fields
        raise RuntimeError("logging backend unavailable")


class MemoryCache:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, ttl: int | None = None) -> bool:
        del ttl
        self.values[key] = value
        return True


def _one(log: RecordingLogger, event: str) -> dict[str, Any]:
    rows = [fields for name, fields in log.calls if name == event]
    assert len(rows) == 1
    return rows[0]


def test_contract_names_and_field_allowlist_match_stage_three() -> None:
    assert {
        "account_persona.resolve",
        "account_persona.snapshot",
        "account_persona.preview",
        "owned_group_prompt.build",
        "owned_group_persona.llm_usage",
        "owned_group_outbound_governance.evaluate",
    } <= STRUCTURED_EVENT_NAMES
    assert {
        "account_persona_configured",
        "account_persona_update_total",
        "account_persona_resolve_total",
        "account_persona_preview_total",
        "account_persona_preview_duration_seconds",
        "owned_group_persona_prompt_build_total",
        "owned_group_persona_llm_tokens_total",
        "owned_group_persona_outbound_block_total",
        "owned_group_persona_account_mismatch_total",
        "owned_group_persona_growth_ad_config_access_total",
    } == LOGICAL_METRIC_NAMES
    assert {
        "persona",
        "system_prompt",
        "user_prompt",
        "context",
        "content",
        "sensitive_term",
        "phone",
        "session",
        "token",
        "proxy",
        "api_key",
    }.isdisjoint(SAFE_STRUCTURED_EVENT_FIELDS)


def test_persona_update_resolve_and_snapshot_emit_only_hash_prefixes() -> None:
    log = RecordingLogger()
    record_persona_update(
        account_id=7,
        revision=3,
        persona_hash=_PERSONA_HASH,
        request_id="req-7",
        result="success",
        log=log,
    )
    record_persona_resolve(
        account_id=7,
        persona_source="configured",
        revision=3,
        persona_hash=_PERSONA_HASH,
        result="success",
        log=log,
    )
    record_persona_snapshot(
        account_id=7,
        asset_id=8,
        core_group_id=9,
        persona_source="configured",
        revision=3,
        persona_hash=_PERSONA_HASH,
        log=log,
    )

    assert _one(log, "account_persona.update")["hash_prefix"] == "a" * 12
    assert _one(log, "account_persona.resolve")["hash_prefix"] == "a" * 12
    assert _one(log, "account_persona.snapshot")["hash_prefix"] == "a" * 12
    update_metric = _one(log, "account_persona_update_total")
    assert update_metric == {"value": 1, "result": "success", "reason_code": "NONE"}
    resolve_metric = _one(log, "account_persona_resolve_total")
    assert resolve_metric == {"value": 1, "source": "configured", "result": "success"}
    encoded = json.dumps(log.calls)
    assert _PERSONA_HASH not in encoded
    assert "system_prompt" not in encoded


def test_prompt_observer_never_receives_prompt_and_preserves_business_result() -> None:
    log = RecordingLogger()
    built = SimpleNamespace(prompt_hash=_PROMPT_HASH, system_prompt="do-not-log")
    result = observe_prompt_build_call(
        lambda: built,
        execution_id=21,
        account_id=7,
        asset_id=8,
        core_group_id=9,
        content_category="community",
        persona_source="configured",
        revision=3,
        persona_hash=_PERSONA_HASH,
        prompt_template_version="owned-group-persona-v1",
        log=log,
    )
    assert result is built
    event = _one(log, "owned_group_prompt.build")
    assert event["hash_prefix"] == "b" * 12
    assert event["result"] == "success"
    assert "do-not-log" not in json.dumps(log.calls)
    assert _one(log, "owned_group_persona_prompt_build_total") == {
        "value": 1,
        "result": "success",
        "content_category": "community",
    }


def test_preview_event_and_metrics_are_content_free() -> None:
    log = RecordingLogger()
    record_persona_preview(
        account_id=7,
        asset_id=8,
        core_group_id=9,
        content_category="community",
        persona_source="draft",
        revision=0,
        persona_hash=_PERSONA_HASH,
        prompt_template_version="owned-group-persona-v1",
        result="failed",
        reason_code="AI_PROVIDER_UNSAFE",
        duration_ms=250,
        request_id="preview-1",
        log=log,
    )
    preview = _one(log, "account_persona.preview")
    assert preview["hash_prefix"] == "a" * 12
    assert preview["duration_ms"] == 250
    assert _one(log, "account_persona_preview_total") == {
        "value": 1,
        "result": "failed",
        "reason_code": "AI_PROVIDER_UNSAFE",
        "content_category": "community",
    }
    assert _one(log, "account_persona_preview_duration_seconds") == {"value": 0.25}
    assert _PERSONA_HASH not in json.dumps(log.calls)


def test_prompt_observer_preserves_build_failure_and_sanitizes_reason() -> None:
    class BuildError(RuntimeError):
        code = "PROMPT_BUILD_FAILED"

    log = RecordingLogger()

    def fail() -> None:
        raise BuildError("raw prompt must not be logged")

    with pytest.raises(BuildError, match="raw prompt"):
        observe_prompt_build_call(
            fail,
            execution_id=21,
            account_id=7,
            asset_id=8,
            core_group_id=9,
            content_category="promotion",
            persona_source="neutral_default",
            revision=0,
            persona_hash=_PERSONA_HASH,
            prompt_template_version="owned-group-persona-v1",
            log=log,
        )
    event = _one(log, "owned_group_prompt.build")
    assert event["result"] == "failed"
    assert event["reason_code"] == "PROMPT_BUILD_FAILED"
    assert "raw prompt" not in json.dumps(log.calls)


def test_llm_usage_uses_integer_cost_and_low_cardinality_metric_labels() -> None:
    log = RecordingLogger()
    record_llm_usage(
        execution_id=21,
        account_id=7,
        asset_id=8,
        content_category="community",
        persona_source="configured",
        revision=3,
        persona_hash=_PERSONA_HASH,
        prompt_template_version="owned-group-persona-v1",
        provider="custom-provider",
        model="unsafe model name with spaces",
        input_tokens=123,
        output_tokens=45,
        cost_microunits=678,
        cache_hit=False,
        usage_source="estimated",
        result="success",
        duration_ms=12,
        log=log,
    )
    usage = _one(log, "owned_group_persona.llm_usage")
    assert usage["provider"] == "unknown"
    assert usage["model"] == "unknown"
    assert usage["cost_microunits"] == 678
    assert isinstance(usage["cost_microunits"], int)
    metrics = [fields for name, fields in log.calls if name == "owned_group_persona_llm_tokens_total"]
    assert metrics == [
        {
            "value": 123,
            "direction": "input",
            "provider": "unknown",
            "model": "unknown",
            "persona_source": "configured",
        },
        {
            "value": 45,
            "direction": "output",
            "provider": "unknown",
            "model": "unknown",
            "persona_source": "configured",
        },
    ]
    assert all("account_id" not in metric for metric in metrics)
    assert all("revision" not in metric for metric in metrics)


@pytest.mark.asyncio
async def test_llm_v2_records_estimated_usage_and_zero_billable_cache_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observer = Mock()
    monkeypatch.setattr(llm_module, "record_llm_usage", observer)
    client = LLMClient(provider=LLMProvider.OPENAI, api_key="test")
    client.cache = MemoryCache()
    client._call_openai = AsyncMock(return_value="安全中文回复")
    context = LLMCacheContext(
        cache_scope="execution:21:account:7:persona:3:generation:1",
        execution_id=21,
        account_id=7,
        persona_revision=3,
        generation_attempt=1,
        prompt_template_version="owned-group-persona-v1",
        persona_hash=_PERSONA_HASH,
        governance_rules_hash=_GOVERNANCE_HASH,
        policy_revision=2,
        asset_id=8,
        content_category="community",
    )

    first = await client.generate_response(
        system_prompt="安全系统约束",
        user_prompt="不可信上下文",
        requires_system_role=True,
        cache_context=context,
        model="test-model",
        max_tokens=32,
    )
    cached = await client.generate_response(
        system_prompt="安全系统约束",
        user_prompt="不可信上下文",
        requires_system_role=True,
        cache_context=context,
        model="test-model",
        max_tokens=32,
    )

    assert first.cached is False
    assert cached.cached is True
    assert observer.call_count == 2
    live_usage = observer.call_args_list[0].kwargs
    assert live_usage["usage_source"] == "estimated"
    assert live_usage["cache_hit"] is False
    assert live_usage["input_tokens"] > 0
    assert live_usage["output_tokens"] > 0
    assert isinstance(live_usage["cost_microunits"], int)
    cache_usage = observer.call_args_list[1].kwargs
    assert cache_usage["usage_source"] == "cache"
    assert cache_usage["cache_hit"] is True
    assert cache_usage["input_tokens"] == 0
    assert cache_usage["output_tokens"] == 0
    assert cache_usage["cost_microunits"] == 0


def test_outbound_block_and_account_mismatch_emit_required_metrics() -> None:
    log = RecordingLogger()
    record_outbound_evaluation(
        execution_id=21,
        account_id=7,
        asset_id=8,
        core_group_id=9,
        content_category="promotion",
        persona_source="configured",
        stage="send",
        allowed=False,
        reason_code="CONTENT_POLICY_CHANGED",
        governance_hash=_GOVERNANCE_HASH,
        duration_ms=5,
        log=log,
    )
    record_persona_account_mismatch(
        account_id=7,
        persona_source="configured",
        revision=3,
        persona_hash=_PERSONA_HASH,
        log=log,
    )
    assert _one(log, "owned_group_persona_outbound_block_total") == {
        "value": 1,
        "stage": "send",
        "reason_code": "CONTENT_POLICY_CHANGED",
        "content_category": "promotion",
    }
    assert _one(log, "owned_group_persona_account_mismatch_total") == {"value": 1}
    governance = _one(log, "owned_group_outbound_governance.evaluate")
    assert governance["hash_prefix"] == "c" * 12
    assert "matched_term" not in json.dumps(log.calls)


@pytest.mark.asyncio
async def test_real_governance_bridge_observes_all_four_strict_stages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.modules.owned_group import messaging_governance_integration as integration

    observer = Mock()
    evaluator = AsyncMock(
        return_value=OutboundGovernanceDecision(
            allowed=True,
            reason_code=None,
            governance_rules_hash=_GOVERNANCE_HASH,
        )
    )
    monkeypatch.setattr(integration, "record_outbound_evaluation", observer)
    monkeypatch.setattr(integration, "evaluate_outbound_content", evaluator)
    execution = SimpleNamespace(
        id=21,
        account_id=7,
        owned_group_asset_id=8,
        core_group_id=9,
        content_category="community",
        mode_snapshot="ai",
        persona_source_snapshot="configured",
        persona_snapshot=NEUTRAL_PERSONA.model_dump(mode="json"),
        governance_rules_hash=_GOVERNANCE_HASH,
        promotion_config_snapshot=None,
        correlation_id="governance-21",
    )

    await integration.govern_generated_body(
        db=AsyncMock(),
        execution=execution,
        text="安全中文消息",
    )
    await integration.govern_generated_final(
        db=AsyncMock(),
        execution=execution,
        text="安全中文消息",
    )
    await integration.govern_review_override(
        db=AsyncMock(),
        execution=execution,
        text="安全中文消息",
    )
    await integration.govern_before_send(
        db=AsyncMock(),
        execution=execution,
        text="安全中文消息",
    )

    assert [call.kwargs["stage"] for call in observer.call_args_list] == [
        "generated_body",
        "generated_final",
        "review",
        "send",
    ]
    assert all(call.kwargs["allowed"] is True for call in observer.call_args_list)
    assert all("text" not in call.kwargs for call in observer.call_args_list)
    assert all("persona_snapshot" not in call.kwargs for call in observer.call_args_list)


@pytest.mark.asyncio
async def test_send_observation_uses_changed_reason_and_compatibility_bypass_is_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.modules.owned_group import messaging_governance_integration as integration

    observer = Mock()
    evaluator = AsyncMock(
        return_value=OutboundGovernanceDecision(
            allowed=False,
            reason_code="PERSONA_FORBIDDEN_TOPIC_MATCHED",
            governance_rules_hash="f" * 64,
        )
    )
    monkeypatch.setattr(integration, "record_outbound_evaluation", observer)
    monkeypatch.setattr(integration, "evaluate_outbound_content", evaluator)
    execution = SimpleNamespace(
        id=21,
        account_id=7,
        owned_group_asset_id=8,
        core_group_id=9,
        content_category="community",
        mode_snapshot="ai",
        persona_source_snapshot="configured",
        persona_snapshot=NEUTRAL_PERSONA.model_dump(mode="json"),
        governance_rules_hash=_GOVERNANCE_HASH,
        promotion_config_snapshot=None,
        correlation_id="governance-21",
    )

    with pytest.raises(OwnedGroupMessagingError) as error:
        await integration.govern_before_send(
            db=AsyncMock(),
            execution=execution,
            text="安全中文消息",
        )
    assert error.value.code == "CONTENT_POLICY_CHANGED"
    assert observer.call_args.kwargs["reason_code"] == "CONTENT_POLICY_CHANGED"
    assert observer.call_args.kwargs["stage"] == "send"

    observer.reset_mock()
    evaluator.reset_mock()
    execution.persona_source_snapshot = "legacy_default"
    assert (
        await integration.govern_before_send(
            db=AsyncMock(),
            execution=execution,
            text="阶段二兼容消息",
        )
        is None
    )
    observer.assert_not_called()
    evaluator.assert_not_awaited()


def test_gauge_and_growth_tripwire_have_no_high_cardinality_labels() -> None:
    log = RecordingLogger()
    record_persona_configured(configured_total=4, log=log)
    record_growth_ad_config_access(log=log)
    assert _one(log, "account_persona_configured") == {"value": 4}
    assert _one(log, "owned_group_persona_growth_ad_config_access_total") == {"value": 1}


@pytest.mark.asyncio
async def test_configured_gauge_refresh_counts_exact_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observer = Mock()
    db = SimpleNamespace(scalar=AsyncMock(return_value=4))
    monkeypatch.setattr(observability_module, "record_persona_configured", observer)

    assert await refresh_persona_configured_gauge(db) == 4
    db.scalar.assert_awaited_once()
    observer.assert_called_once_with(configured_total=4)


@pytest.mark.asyncio
async def test_configured_gauge_refresh_failures_never_escape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_failure = SimpleNamespace(scalar=AsyncMock(side_effect=RuntimeError("database down")))
    assert await refresh_persona_configured_gauge(db_failure) is None

    observer_failure = Mock(side_effect=RuntimeError("metrics down"))
    monkeypatch.setattr(
        observability_module,
        "record_persona_configured",
        observer_failure,
    )
    healthy_db = SimpleNamespace(scalar=AsyncMock(return_value=2))
    assert await refresh_persona_configured_gauge(healthy_db) is None


@pytest.mark.asyncio
async def test_startup_gauge_session_failure_never_blocks_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenSessionContext:
        async def __aenter__(self) -> None:
            raise RuntimeError("session unavailable")

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(
        database_module,
        "get_db_session",
        lambda: BrokenSessionContext(),
    )
    assert await refresh_persona_configured_gauge_at_startup() is None


def test_observer_failures_and_invalid_fields_never_change_business_flow() -> None:
    exploding = ExplodingLogger()
    record_persona_update(
        account_id=1,
        revision=0,
        persona_hash=_PERSONA_HASH,
        result="success",
        log=exploding,
    )
    record_llm_usage(
        execution_id=None,
        account_id=-1,
        asset_id=2,
        content_category="community",
        persona_source="configured",
        revision=0,
        persona_hash=_PERSONA_HASH,
        prompt_template_version="owned-group-persona-v1",
        provider="openai",
        model="gpt-5",
        input_tokens=1,
        output_tokens=1,
        cost_microunits=1,
        cache_hit=False,
        usage_source="provider",
        result="success",
        duration_ms=1,
        log=exploding,
    )
    assert (
        observe_prompt_build_call(
            lambda: "business-result",
            execution_id=None,
            account_id=1,
            asset_id=2,
            core_group_id=3,
            content_category="community",
            persona_source="configured",
            revision=1,
            persona_hash=_PERSONA_HASH,
            prompt_template_version="owned-group-persona-v1",
            log=exploding,
        )
        == "business-result"
    )
