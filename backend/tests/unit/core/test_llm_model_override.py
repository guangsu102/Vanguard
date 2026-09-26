from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from app.core.ai.llm_client import LLMCacheContext, LLMClient, LLMProvider
from app.core.config import settings

class Cache:
    def __init__(self):
        self.values = {}
    async def get(self, key):
        return self.values.get(key)
    async def set(self, key, value, ttl=None):
        self.values[key] = value

@pytest.fixture
def pinned(monkeypatch):
    monkeypatch.setattr(settings, "LLM_MODEL_OVERRIDE", "glm-5.3-flash")
    monkeypatch.setattr(settings, "LLM_REASONING_EFFORT", "low")
    client = LLMClient(api_key="test", base_url="https://example.invalid/v1")
    client.cache = Cache()
    return client

@pytest.mark.parametrize("tier", ["fast", "balanced", "quality"])
def test_pin_covers_every_openai_tier(pinned, tier):
    assert pinned.model_for(tier) == "glm-5.3-flash"

@pytest.mark.asyncio
async def test_explicit_legacy_model_cannot_reuse_gpt_cache_or_reach_provider(pinned):
    old_key = pinned._get_cache_key("prompt", "gpt-4o", 0.7, None)
    pinned.cache.values[old_key] = "old GPT response"
    pinned._call_openai = AsyncMock(return_value="GLM response")
    assert await pinned.generate("prompt", model="gpt-4o") == "GLM response"
    assert pinned._call_openai.await_args.args[1] == "glm-5.3-flash"
    assert await pinned.generate("prompt", model="gpt-5.6-sol") == "GLM response"
    assert pinned._call_openai.await_count == 1

@pytest.mark.asyncio
async def test_persona_response_and_cache_report_effective_model(pinned):
    context = LLMCacheContext(
        cache_scope="execution:1:account:2:persona:3:generation:1",
        execution_id=1, account_id=2, persona_revision=3, generation_attempt=1,
        prompt_template_version="owned-group-persona-v1", persona_hash="a" * 64,
        governance_rules_hash="b" * 64, policy_revision=1, asset_id=1,
        content_category="community",
    )
    pinned._call_openai = AsyncMock(return_value="GLM response")
    result = await pinned.generate_response(
        system_prompt="system", user_prompt="user", requires_system_role=True,
        cache_context=context, model="gpt-4o",
    )
    assert result.model == "glm-5.3-flash"
    assert pinned._call_openai.await_args.args[1] == "glm-5.3-flash"
    again = await pinned.generate_response(
        system_prompt="system", user_prompt="user", requires_system_role=True,
        cache_context=context, model="gpt-5.6-sol",
    )
    assert again.cached is True
    assert again.model == "glm-5.3-flash"
    assert pinned._call_openai.await_count == 1

@pytest.mark.asyncio
async def test_provider_boundary_pins_direct_calls_and_passes_reasoning_setting(pinned):
    pinned._check_upstream_cooldown = AsyncMock()
    response = MagicMock()
    response.choices[0].message.content = "OK"
    sdk = MagicMock()
    sdk.chat.completions.create = AsyncMock(return_value=response)
    sdk.close = AsyncMock()
    with patch("openai.AsyncOpenAI", return_value=sdk):
        assert await pinned._call_openai([{"role": "user", "content": "test"}], "gpt-4o", 0, 64) == "OK"
    request = sdk.chat.completions.create.await_args.kwargs
    assert request["model"] == "glm-5.3-flash"
    assert request["extra_body"] == {"reasoning_effort": "low"}

def test_openai_pin_does_not_change_other_provider_models(pinned):
    client = LLMClient(provider=LLMProvider.ANTHROPIC, api_key="test")
    assert client._effective_model("claude-test") == "claude-test"
    assert client.openai_extra_body == {}
