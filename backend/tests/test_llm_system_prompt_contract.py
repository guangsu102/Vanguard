from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.core.ai.llm_client import (
    LLMCacheContext,
    LLMClient,
    LLMClientError,
    LLMProvider,
)


class _Cache:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.get_calls = 0

    async def get(self, key: str) -> str | None:
        self.get_calls += 1
        return self.values.get(key)

    async def set(self, key: str, value: str, ttl: int | None = None) -> bool:
        self.values[key] = value
        return True


def _context(*, preview: bool = False) -> LLMCacheContext:
    nonce = str(uuid4()) if preview else None
    return LLMCacheContext(
        cache_scope=f"preview:{nonce}" if preview else "execution:9001:account:17:persona:3:generation:1",
        execution_id=None if preview else 9001,
        account_id=17,
        persona_revision=None if preview else 3,
        generation_attempt=1,
        prompt_template_version="owned-group-persona-v1",
        persona_hash="a" * 64,
        governance_rules_hash="b" * 64,
        policy_revision=6,
        asset_id=25,
        content_category="community",
        preview_nonce=nonce,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", [LLMProvider.OPENAI, LLMProvider.ANTHROPIC])
async def test_generate_response_preserves_system_and_user_roles(provider: LLMProvider) -> None:
    client = LLMClient(provider=provider, api_key="test")
    client.cache = _Cache()
    method_name = "_call_openai" if provider == LLMProvider.OPENAI else "_call_anthropic"
    provider_call = AsyncMock(return_value="有效中文回复")
    setattr(client, method_name, provider_call)

    response = await client.generate_response(
        system_prompt="高优先级安全边界",
        user_prompt="不可信群上下文",
        requires_system_role=True,
        cache_context=_context(),
        model="test-model",
        temperature=0,
        max_tokens=80,
    )

    messages = provider_call.await_args.args[0]
    assert messages == [
        {"role": "system", "content": "高优先级安全边界"},
        {"role": "user", "content": "不可信群上下文"},
    ]
    assert response.content == "有效中文回复"
    assert response.provider == provider.value
    assert response.cached is False


@pytest.mark.asyncio
async def test_local_strict_role_gate_runs_before_cache_and_provider() -> None:
    client = LLMClient(provider=LLMProvider.LOCAL)
    cache = _Cache()
    client.cache = cache
    client._call_local = AsyncMock(return_value="不应调用")

    with pytest.raises(LLMClientError) as error:
        await client.generate_response(
            system_prompt="安全边界",
            user_prompt="用户内容",
            requires_system_role=True,
            cache_context=_context(),
        )

    assert error.value.code == "AI_PROVIDER_SYSTEM_PROMPT_UNSUPPORTED"
    assert cache.get_calls == 0
    client._call_local.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_neutral_compatibility_keeps_existing_user_only_semantics() -> None:
    client = LLMClient(provider=LLMProvider.LOCAL)
    client.cache = _Cache()
    client._call_local = AsyncMock(return_value="中性中文回复")

    result = await client.generate_response(
        system_prompt="阶段二可能非空的 system",
        user_prompt="阶段二 user prompt",
        requires_system_role=False,
        cache_context=_context(),
    )

    assert result.content == "中性中文回复"
    assert client._call_local.await_args.args[0] == "阶段二 user prompt"


@pytest.mark.asyncio
async def test_token_budget_counts_system_user_and_reserved_response() -> None:
    client = LLMClient(provider=LLMProvider.OPENAI, api_key="test")
    client.cache = _Cache()
    client.PERSONA_CONTEXT_TOKEN_LIMIT = 20
    client._call_openai = AsyncMock(return_value="不应调用")

    with pytest.raises(LLMClientError) as error:
        await client.generate_response(
            system_prompt="安全边界" * 8,
            user_prompt="用户上下文" * 8,
            requires_system_role=True,
            cache_context=_context(),
            max_tokens=10,
        )

    assert error.value.code == "AI_PROMPT_TOO_LARGE"
    client._call_openai.assert_not_awaited()


def test_capabilities_are_explicit_and_immutable() -> None:
    assert LLMClient(provider=LLMProvider.OPENAI).capabilities().supports_system_role is True
    assert LLMClient(provider=LLMProvider.ANTHROPIC).capabilities().supports_system_role is True
    assert LLMClient(provider=LLMProvider.LOCAL).capabilities().supports_system_role is False
