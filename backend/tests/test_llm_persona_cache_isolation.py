from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.core.ai.llm_client import LLMCacheContext, LLMClient, LLMProvider


class _Cache:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.ttls: list[int | None] = []

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, ttl: int | None = None) -> bool:
        self.values[key] = value
        self.ttls.append(ttl)
        return True


def _execution_context() -> LLMCacheContext:
    return LLMCacheContext(
        cache_scope="execution:9001:account:17:persona:3:generation:1",
        execution_id=9001,
        account_id=17,
        persona_revision=3,
        generation_attempt=1,
        prompt_template_version="owned-group-persona-v1",
        persona_hash="a" * 64,
        governance_rules_hash="b" * 64,
        policy_revision=6,
        asset_id=25,
        content_category="community",
    )


def _preview_context() -> LLMCacheContext:
    nonce = str(uuid4())
    return LLMCacheContext(
        cache_scope=f"preview:{nonce}",
        execution_id=None,
        account_id=17,
        persona_revision=None,
        generation_attempt=1,
        prompt_template_version="owned-group-persona-v1",
        persona_hash="a" * 64,
        governance_rules_hash="b" * 64,
        policy_revision=6,
        asset_id=25,
        content_category="community",
        preview_nonce=nonce,
    )


def test_v2_key_contains_complete_hashed_material_without_raw_prompt() -> None:
    client = LLMClient(
        provider=LLMProvider.OPENAI,
        base_url="HTTPS://例子.测试:443/custom//v1/",
    )
    key, material = client.build_v2_cache_key(
        system_prompt="绝密 system",
        user_prompt="群消息原文",
        cache_context=_execution_context(),
        model="configured-model",
        temperature=0.7,
        max_tokens=300,
    )

    assert key.startswith("llm:v2:")
    assert len(key) == len("llm:v2:") + 64
    assert set(material) == {
        "cache_schema_version",
        "provider",
        "model",
        "temperature",
        "max_tokens",
        "system_prompt_sha256",
        "user_prompt_sha256",
        "provider_endpoint_sha256",
        "cache_scope",
        "execution_id",
        "account_id",
        "persona_revision",
        "generation_attempt",
        "prompt_template_version",
        "persona_hash",
        "governance_rules_hash",
        "policy_revision",
        "asset_id",
        "content_category",
        "preview_nonce",
    }
    assert "绝密" not in repr(material)
    assert "群消息" not in repr(material)


@pytest.mark.asyncio
async def test_cache_isolated_by_account_execution_persona_and_preview() -> None:
    client = LLMClient(provider=LLMProvider.OPENAI, api_key="test", cache_ttl=7200)
    cache = _Cache()
    client.cache = cache
    client._call_openai = AsyncMock(side_effect=["回复一", "回复二", "回复三", "回复四"])

    arguments = {
        "system_prompt": "安全",
        "user_prompt": "同一个 Prompt",
        "requires_system_role": True,
        "model": "test-model",
        "temperature": 0,
        "max_tokens": 80,
    }
    first = await client.generate_response(
        **arguments,
        cache_context=_execution_context(),
    )
    cached = await client.generate_response(
        **arguments,
        cache_context=_execution_context(),
    )
    other_account = await client.generate_response(
        **arguments,
        cache_context=replace(
            _execution_context(),
            account_id=18,
            cache_scope="execution:9001:account:18:persona:3:generation:1",
        ),
    )
    other_execution = await client.generate_response(
        **arguments,
        cache_context=replace(
            _execution_context(),
            execution_id=9002,
            cache_scope="execution:9002:account:17:persona:3:generation:1",
        ),
    )
    preview = await client.generate_response(
        **arguments,
        cache_context=_preview_context(),
    )

    assert first.content == cached.content == "回复一"
    assert cached.cached is True
    assert other_account.content == "回复二"
    assert other_execution.content == "回复三"
    assert preview.content == "回复四"
    assert client._call_openai.await_count == 4
    assert cache.ttls and all(ttl == 3600 for ttl in cache.ttls)


def test_endpoint_hash_changes_and_cache_context_rejects_invalid_shapes() -> None:
    official = LLMClient(provider=LLMProvider.OPENAI)
    explicit_official = LLMClient(
        provider=LLMProvider.OPENAI,
        base_url="HTTPS://API.OPENAI.COM:443/v1/",
    )
    custom = LLMClient(provider=LLMProvider.OPENAI, base_url="https://api.example/v1")
    assert official.provider_endpoint_sha256() == explicit_official.provider_endpoint_sha256()
    assert official.provider_endpoint_sha256() != custom.provider_endpoint_sha256()

    with pytest.raises(ValueError, match="generation_attempt"):
        replace(_execution_context(), generation_attempt=4)
    with pytest.raises(ValueError, match="execution calls"):
        replace(_execution_context(), preview_nonce=str(uuid4()))
    neutral = replace(
        _execution_context(),
        persona_revision=0,
        cache_scope="execution:9001:account:17:persona:0:generation:1",
    )
    assert neutral.persona_revision == 0
    with pytest.raises(ValueError, match="require persona_revision"):
        replace(_execution_context(), persona_revision=None)
