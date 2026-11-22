"""Configured official OpenAI reviews cross preflight without widening destinations."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.ai.llm_client import LLMClient
from app.core.config import settings
from app.modules.acquisition import qualification_ai as ai
from app.modules.acquisition.automation import GroupAdRulesAuditResult


@pytest.mark.parametrize("base_url", [
    "https://api.openai.com",
    "https://api.openai.com/",
    "https://api.openai.com/v1",
    "https://api.openai.com/v1/",
    "https://api.openai.com:443/v1",
    "https://api.openai.com:443/v1/",
    "https://api.pipenai.xyz/v1",
    "https://pipenai.xyz",
    "https://ark.cn-beijing.volces.com/api/coding/v3",
])
def test_explicitly_approved_destination(base_url):
    assert ai.approved_destination(base_url)


@pytest.mark.parametrize("base_url", [
    "http://api.openai.com/v1",
    "https://api.openai.com:80/v1",
    "https://api.openai.com:444/v1",
    "https://user@api.openai.com/v1",
    "https://user:password@api.openai.com/v1",
    "https://api.openai.com/v1?token=not-a-key",
    "https://api.openai.com/v1#fragment",
    "https://api.openai.com.evil.example/v1",
    "https://api.openai.com@evil.example/v1",
    "https://api.openai.com./v1",
    "https://api.openai.com/v2",
    "https://api.openai.com/v1/responses",
    "https://api.openai.com/api/coding/v3",
    "https://unknown.example/v1",
    "https://ark.cn-beijing.volces.com/v1",
    "https://api.pipenai.xyz/api/coding/v3",
    "",
    "api.openai.com/v1",
    "https://api.openai.com:invalid/v1",
])
def test_other_destinations_remain_rejected(base_url):
    assert not ai.approved_destination(base_url)


@pytest.mark.parametrize("configured", [
    "https://api.openai.com",
    "https://api.openai.com/",
    "https://api.openai.com/v1",
    "https://api.openai.com:443/v1/",
])
def test_real_client_normalization_preserves_official_destination(configured):
    assert ai.approved_destination(LLMClient._normalize_openai_base_url(configured))


@pytest.mark.asyncio
@pytest.mark.parametrize("base_url,expected_reason,slot_calls", [
    ("https://api.openai.com/v1", "group_rules_ai_capacity_unavailable", 1),
    ("https://api.openai.com.evil.example/v1", "group_rules_ai_destination_unapproved", 0),
])
async def test_real_review_preflight_respects_destination_before_once_claim(
    monkeypatch, base_url, expected_reason, slot_calls,
):
    monkeypatch.setattr(settings, "GROWTH_EFFICIENCY_ENABLED", False)
    # Construct no SDK/cache instance: the real type check and review preflight
    # still run, and the exhausted-slot response fences every external call.
    client = object.__new__(LLMClient)
    client.base_url = base_url
    db = MagicMock()
    db.get = AsyncMock(return_value=None)
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    claim = AsyncMock(return_value=None)
    monkeypatch.setattr(ai, "claim_slot", claim)
    generate = AsyncMock(side_effect=AssertionError("preflight invoked provider"))
    service = SimpleNamespace(db=db, _ad_policy_llm=lambda: client,
                              _evaluate_group_ad_rules_with_ai=generate)
    snapshot = {"raw_peer_id": 42, "evidence": [
        {"source": "full_about", "text": "Rules available for review", "sender_role": "admin"},
    ]}
    result = await ai.review_semantics(service, snapshot,
        SimpleNamespace(id=2, profile_bio="test"), GroupAdRulesAuditResult(),
        {"ad_policy_ai_enabled": True})
    assert result.reason == expected_reason
    assert claim.await_count == slot_calls
    assert snapshot["ai_pending"] and not snapshot["ai_request_started"]
    db.add.assert_not_called()
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()
    generate.assert_not_awaited()
