"""Strict PP-AI review exercises the actual prompt, parser and single-call gate."""

import json
from datetime import datetime
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.modules.acquisition.qualification_service import review_schedule
from app.modules.acquisition.automation import (
    AcquisitionAutomationService,
    GroupAdRulesAuditResult,
)

STRICT = {
    "require_evidence_arguments": True,
    "ad_policy_ai_enabled": True,
    "ad_policy_ai_model": "test-model",
    "ad_policy_ai_min_confidence": 95,
    "ad_policy_ai_require_second_pass": True,
}


def advertisements():
    return [
        {
            "source": "recent_promotional_message",
            "text": "家具出售联系 https://chairs.example",
            "sender_id": 10,
            "sender_role": "ordinary",
            "message_id": 101,
            "age_hours": 25,
            "accessible": True,
            "warning_search_complete": True,
        },
        {
            "source": "recent_promotional_message",
            "text": "鲜花批发联系 https://flowers.example",
            "sender_id": 20,
            "sender_role": "ordinary",
            "message_id": 102,
            "age_hours": 26,
            "accessible": True,
            "warning_search_complete": True,
        },
    ]


def verdict(**overrides):
    return {
        "mode": "soft_ad_trial",
        "confidence": 97,
        "explicit_permission": False,
        "direct_posting_without_prior_approval": False,
        "requires_admin_approval": False,
        "observed_soft_ad_tolerance": True,
        "low_risk_trial_suitable": True,
        "conflict": False,
        "applicable_prohibition": False,
        "supporting_evidence_indexes": [0, 1],
        "opposing_evidence_indexes": [],
        "opposing_evidence_resolution": "",
        "rationale": "Items 0 and 1 are distinct ordinary-member advertisements; item 0 retained 25 hours.",
        **overrides,
    }


async def evaluate(evidence, first, second=None, *, capacity=None, local=None):
    agent = AcquisitionAutomationService(MagicMock())
    agent._record_llm_health = AsyncMock()
    agent._ad_policy_llm_client = SimpleNamespace(
        generate=AsyncMock(
            side_effect=[
                json.dumps(first),
                json.dumps(second if second is not None else first),
            ]
        )
    )
    result = await agent._evaluate_group_ad_rules_with_ai(
        evidence,
        local or GroupAdRulesAuditResult(evidence=evidence),
        capacity or STRICT,
    )
    return result, agent


@pytest.mark.asyncio
async def test_single_strict_95_review_preserve_positive_and_explicit_empty_negative_evidence():
    result, agent = await evaluate(advertisements(), verdict())
    assert result.ad_allowed is True
    assert result.confidence == 97
    assert len(result.ai_reviews) == 1
    for review in result.ai_reviews:
        assert review["supporting_evidence_indexes"] == [0, 1]
        assert review["opposing_evidence_indexes"] == []
        assert review["evidence_arguments_valid"]
    assert agent._ad_policy_llm_client.generate.await_count == 1
    prompts = agent._ad_policy_llm_client.generate.call_args_list
    assert "opposing_evidence_indexes" in prompts[0].kwargs["system_prompt"]
    assert "Any prohibition wins" not in prompts[0].kwargs["system_prompt"]
    assert prompts[0].kwargs["max_retries"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("supporting_evidence_indexes", [0, 999]),
        ("supporting_evidence_indexes", ["0", 1]),
        ("supporting_evidence_indexes", [True, 1]),
        ("supporting_evidence_indexes", []),
        ("opposing_evidence_indexes", None),
        ("applicable_prohibition", None),
    ],
)
async def test_strict_review_cannot_omit_or_fabricate_evidence(field, value):
    second = verdict()
    second[field] = value
    result, _ = await evaluate(advertisements(), second)
    assert result.ad_allowed is False
    assert result.reason == "group_rules_ai_evidence_arguments_incomplete"


@pytest.mark.asyncio
async def test_legacy_response_remains_compatible_but_cannot_pass_strict_qualification():
    legacy = verdict(evidence_indexes=[0, 1])
    del legacy["supporting_evidence_indexes"]
    del legacy["opposing_evidence_indexes"]
    result, _ = await evaluate(advertisements(), legacy)
    assert result.ad_allowed is False
    assert result.reason == "group_rules_ai_evidence_arguments_incomplete"
    parsed = AcquisitionAutomationService._parse_ad_policy_ai_response(json.dumps(legacy), 2)
    assert parsed["evidence_indexes"] == [0, 1]
    assert not parsed["evidence_arguments_present"]
    old_capacity = {
        key: value for key, value in STRICT.items() if key != "require_evidence_arguments"
    }
    legacy_result, _ = await evaluate(advertisements(), legacy, capacity=old_capacity)
    assert legacy_result.ad_allowed is True


@pytest.mark.asyncio
async def test_theme_cannot_replace_advertisement_citations_even_when_history_is_available():
    evidence = [{"source": "group_profile", "text": "AI ChatGPT推广交流"}] + advertisements()
    result, _ = await evaluate(evidence, verdict(supporting_evidence_indexes=[0]))
    assert result.ad_allowed is False
    assert result.reason == "group_rules_ai_evidence_arguments_incomplete"


@pytest.mark.asyncio
async def test_strict_path_forces_single_review_and_95_even_if_capacity_is_weaker():
    capacity = {
        **STRICT,
        "ad_policy_ai_min_confidence": 80,
        "ad_policy_ai_require_second_pass": False,
    }
    result, agent = await evaluate(advertisements(), verdict(confidence=94), capacity=capacity)
    assert result.ad_allowed is False
    assert len(result.ai_reviews) == 1
    assert agent._ad_policy_llm_client.generate.await_count == 1
    assert result.reason == "group_rules_ai_consensus_failed"


@pytest.mark.asyncio
async def test_negated_ban_is_semantically_reviewed_instead_of_regex_veto():
    evidence = [{"source": "pinned_message", "text": "不禁止广告，普通成员可直接发布文字广告"}]
    agent = AcquisitionAutomationService(MagicMock())
    local = agent._evaluate_group_ad_rules(evidence)
    review = verdict(
        mode="soft_ad_allowed",
        explicit_permission=True,
        direct_posting_without_prior_approval=True,
        supporting_evidence_indexes=[0],
        opposing_evidence_indexes=[0],
        opposing_evidence_resolution="Index 0 negates the prohibition; it explicitly allows ordinary-member text ads.",
        rationale="The complete sentence at index 0 directly permits this format.",
    )
    result, _ = await evaluate(evidence, review, local=local)
    assert result.ad_allowed is True
    assert result.policy_mode == "soft_ad_allowed"


@pytest.mark.asyncio
async def test_updated_rule_requires_old_opposition_to_be_cited_and_resolved():
    evidence = [
        {
            "source": "pinned_message",
            "text": "旧群规：禁止广告",
            "created_at": "2026-09-01T00:00:00",
        },
        {
            "source": "admin_rule",
            "text": "最新规则替代旧群规：普通成员可直接发布文字广告",
            "sender_role": "admin",
            "created_at": "2026-09-22T00:00:00",
        },
    ]
    review = verdict(
        mode="soft_ad_allowed",
        explicit_permission=True,
        direct_posting_without_prior_approval=True,
        supporting_evidence_indexes=[1],
        opposing_evidence_indexes=[0],
        opposing_evidence_resolution="The verified administrator update at 1 explicitly replaces old rule 0.",
        rationale="Rule 1 supersedes rule 0 and authorizes the requested format.",
    )
    result, _ = await evaluate(evidence, review)
    assert result.ad_allowed is True
    incomplete = deepcopy(review)
    incomplete["opposing_evidence_indexes"] = []
    result, _ = await evaluate(evidence, incomplete)
    assert result.ad_allowed is False
    assert result.reason == "group_rules_ai_evidence_arguments_incomplete"


@pytest.mark.asyncio
async def test_applicable_ban_cannot_be_overridden_by_confidence_or_member_ads():
    evidence = advertisements() + [{"source": "pinned_message", "text": "本群禁止广告"}]
    review = verdict(
        confidence=100,
        opposing_evidence_indexes=[2],
        applicable_prohibition=True,
        opposing_evidence_resolution="The current ban at 2 applies to this text/profile-CTA promotion.",
    )
    result, _ = await evaluate(evidence, review)
    assert result.ad_allowed is False
    assert result.policy_mode == "forbidden"


@pytest.mark.asyncio
async def test_unresolved_multi_pin_conflict_stays_unknown():
    evidence = [
        {"source": "pinned_message", "text": "允许普通成员直接发布广告"},
        {"source": "pinned_message", "text": "禁止广告"},
    ]
    review = verdict(
        mode="soft_ad_allowed",
        explicit_permission=True,
        direct_posting_without_prior_approval=True,
        supporting_evidence_indexes=[0],
        opposing_evidence_indexes=[1],
        conflict=True,
        opposing_evidence_resolution="Both pins appear current and their precedence cannot be established.",
    )
    result, _ = await evaluate(evidence, review)
    assert result.ad_allowed is False
    assert result.reason == "group_rules_ai_unresolved_conflict"


@pytest.mark.asyncio
@pytest.mark.parametrize("configured,expected", [(45, 45), (120, 120), (300, 120), (1, 5)])
async def test_audit_deadline_reaches_dedicated_sdk_without_hidden_retry(configured, expected):
    from app.core.ai.llm_client import LLMClient

    agent = AcquisitionAutomationService(MagicMock())
    client = LLMClient(api_key="test-key")
    client.generate = AsyncMock(return_value=json.dumps(verdict()))
    agent._ad_policy_llm_client = client
    result = await agent._ask_ad_policy_ai(
        advertisements(), model="test-model", timeout_seconds=configured,
    )
    assert result["mode"] == "soft_ad_trial"
    assert client.OPENAI_REQUEST_TIMEOUT_SECONDS == expected
    assert client.OPENAI_TOTAL_TIMEOUT_SECONDS == expected
    assert client.OPENAI_MAX_RETRIES == 0
    client.generate.assert_awaited_once()
    # The default client used by unrelated modules is unchanged.
    assert LLMClient.OPENAI_REQUEST_TIMEOUT_SECONDS == 45
    assert LLMClient.OPENAI_MAX_RETRIES == 2


def test_dedicated_group_policy_endpoint_does_not_replace_global_llm(monkeypatch):
    from app.modules.acquisition import automation

    monkeypatch.setattr(automation.settings, "OPENAI_API_KEY", "global-test-key")
    monkeypatch.setattr(automation.settings, "OPENAI_BASE_URL", "https://api.pipenai.xyz/v1")
    monkeypatch.setattr(automation.settings, "GROUP_AD_POLICY_API_KEY", "dedicated-test-key")
    monkeypatch.setattr(
        automation.settings,
        "GROUP_AD_POLICY_BASE_URL",
        "https://ark.cn-beijing.volces.com/api/coding/v3",
    )
    service = object.__new__(AcquisitionAutomationService)
    service._ad_policy_llm_client = None
    client = service._ad_policy_llm()
    assert client.api_key == "dedicated-test-key"
    assert client.base_url == "https://ark.cn-beijing.volces.com/api/coding/v3"
    assert client.openai_extra_body == {"reasoning_effort": "low"}
    assert automation.settings.OPENAI_API_KEY == "global-test-key"

    service._ad_policy_llm_client = None
    monkeypatch.setattr(automation.settings, "GROUP_AD_POLICY_API_KEY", None)
    assert service._ad_policy_llm() is None


@pytest.mark.asyncio
async def test_v3_trial_prompt_requires_one_retained_ordinary_ad():
    result, agent = await evaluate(advertisements(), verdict())
    assert result.ad_allowed is True
    prompt = agent._ad_policy_llm_client.generate.await_args_list[0].kwargs[
        "system_prompt"
    ]
    assert "requires one visible advertisement" in prompt
    assert "the verified retained ordinary-member advertisement" in prompt

class ProviderContentRejected(Exception):
    status_code = 400
    body = {"error": {"code": "SensitiveContentDetected"}}


@pytest.mark.asyncio
async def test_provider_content_rejection_defers_without_authorizing_ads():
    service = AcquisitionAutomationService(MagicMock())
    service._record_llm_health = AsyncMock()
    service._ask_ad_policy_ai = AsyncMock(side_effect=ProviderContentRejected())
    result = await service._evaluate_group_ad_rules_with_ai(
        advertisements(), GroupAdRulesAuditResult(evidence=advertisements()), STRICT
    )
    assert result.ad_allowed is False
    assert result.reason == "group_rules_ai_provider_content_rejected"
    assert service._ask_ad_policy_ai.await_count == 1
    snapshot = {
        "decision": "observe",
        "reason": result.reason,
        "ai_review_incomplete": True,
        "permissions": {},
    }
    now = datetime(2026, 9, 24, 13)
    verdict, reason, state, retry = review_schedule(snapshot, {}, now)
    # An inconclusive provider rejection observes under degraded pacing (ads
    # stay unauthorized) instead of failing closed on the first attempt.
    assert (verdict, reason, state) == (
        "observe", "group_rules_ai_provider_content_rejected", "completed"
    )
    assert snapshot.get("ai_final") is not True
    # Third consecutive inconclusive attempt on the same material fails closed.
    from app.modules.acquisition.qualification_retry import rule_retry_fingerprint
    fingerprint = rule_retry_fingerprint(snapshot)
    verdict, _, _, retry = review_schedule(
        dict(snapshot),
        {"rejection_fingerprint": fingerprint, "unchanged_rejection_count": 2},
        now,
    )
    assert (verdict, retry) == ("reject", None)
