"""Network-free regression coverage for bounded upstream failure handling."""

import asyncio
from datetime import UTC, datetime
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from openai import APITimeoutError

from app.core.ai.keyword_generator import KeywordGenerator
from app.core.ai.llm_client import LLMClient, LLMClientError
from app.core.redis import RedisCache


class ExpiringRedis:
    """Shared clock-controlled Redis stand-in for independent LLM clients."""

    def __init__(self):
        self.now = 0
        self.values = {}

    async def get(self, key):
        value, expires = self.values.get(key, (None, 0))
        return value if expires > self.now else None

    async def setex(self, key, ttl, value):
        self.values[key] = (value, self.now + ttl)
        return True

    async def eval(self, script, key_count, key, seconds):
        previous = self.values.get(key, (None, 0))[1]
        self.values[key] = ("1", max(previous, self.now + seconds))
        return 1


def upstream_error(status, headers=None):
    error = RuntimeError("upstream body contains private request material")
    error.status_code = status
    error.response = SimpleNamespace(headers=headers or {})
    return error


@pytest.mark.parametrize(
    "error,expected",
    [
        (TimeoutError(), True),
        (upstream_error(429), True),
        (upstream_error(503), True),
        (upstream_error(400), False),
        (RuntimeError("permanent"), False),
    ],
)
def test_temporary_upstream_error_classification(error, expected):
    assert LLMClient.is_temporary_upstream_error(error) is expected


@pytest.fixture
def shared_redis():
    return ExpiringRedis()


@pytest.fixture
def sdk(monkeypatch):
    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value="recovered")
    client.close = AsyncMock()
    constructor = MagicMock(return_value=client)
    monkeypatch.setattr("openai.AsyncOpenAI", constructor)
    return client, constructor


def make_client(shared_redis, **kwargs):
    client = LLMClient(api_key=kwargs.pop("api_key", "test-secret"), **kwargs)
    client.cache = RedisCache(shared_redis)
    return client


async def call(client, model="model-a"):
    return await client._call_openai([{"role": "user", "content": "test"}], model, 0, 40)


@pytest.mark.parametrize("status,seconds", [(429, 60), (502, 30), (503, 30), (504, 30)])
async def test_upstream_failure_is_shared_across_clients_and_recovers_after_expiry(shared_redis, sdk, status, seconds):
    provider, constructor = sdk
    original = upstream_error(status)
    provider.chat.completions.create.side_effect = [original, "recovered"]
    first = make_client(shared_redis)
    second = make_client(shared_redis)

    with pytest.raises(RuntimeError) as failed:
        await call(first)
    assert failed.value is original
    with pytest.raises(LLMClientError) as blocked:
        await call(second)
    assert blocked.value.code == "AI_PROVIDER_COOLDOWN"
    assert constructor.call_count == 1
    provider.close.assert_awaited_once()

    shared_redis.now += seconds
    assert await call(second) == "recovered"
    assert provider.chat.completions.create.await_count == 2
    assert constructor.call_args.kwargs["max_retries"] == 2
    assert constructor.call_args.kwargs["timeout"].read == 45.0
    assert constructor.call_args.kwargs["timeout"].connect == 5.0


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_permanent_errors_do_not_create_shared_cooldown(shared_redis, sdk, status):
    provider, _ = sdk
    provider.chat.completions.create.side_effect = upstream_error(status)
    client = make_client(shared_redis)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await call(client)
    assert shared_redis.values == {}
    assert provider.chat.completions.create.await_count == 2


@pytest.mark.parametrize("changed", ["model", "endpoint", "credential"])
async def test_cooldown_does_not_block_other_models_endpoints_or_credentials(shared_redis, sdk, changed):
    provider, _ = sdk
    provider.chat.completions.create.side_effect = [upstream_error(429), "recovered"]
    first = make_client(shared_redis)
    with pytest.raises(RuntimeError):
        await call(first)
    second = make_client(
        shared_redis,
        **({"base_url": "https://different.example/v1"} if changed == "endpoint" else {}),
        **({"api_key": "different-secret"} if changed == "credential" else {}),
    )
    assert await call(second, "model-b" if changed == "model" else "model-a") == "recovered"
    assert all("test-secret" not in key and "model-a" not in key for key in shared_redis.values)


async def test_content_cache_hit_remains_available_during_cooldown(shared_redis, sdk):
    provider, constructor = sdk
    client = make_client(shared_redis)
    cache_key = client._get_cache_key("cached prompt", "model-a", 0.7, None)
    await client.cache.set(cache_key, "cached answer", ttl=3600)
    await client._record_upstream_cooldown(client._upstream_cooldown_key("model-a"), upstream_error(429))

    assert await client.generate("cached prompt", model="model-a") == "cached answer"
    constructor.assert_not_called()
    provider.chat.completions.create.assert_not_awaited()


@pytest.mark.parametrize("headers,expected", [
    ({}, 60),
    ({"retry-after": "120"}, 120),
    ({"retry-after": "60.1"}, 61),
    ({"retry-after": "99999999"}, 300),
    ({"retry-after-ms": "125000"}, 125),
    ({"retry-after-ms": "invalid", "retry-after": "80"}, 80),
    ({"retry-after": "-1"}, 60),
    ({"retry-after": "NaN"}, 60),
    ({"retry-after": "Infinity"}, 60),
    ({"retry-after": "invalid"}, 60),
])
def test_retry_after_is_bounded_and_invalid_values_are_safe(shared_redis, headers, expected):
    client = make_client(shared_redis)
    assert client._upstream_cooldown_seconds(upstream_error(429, headers)) == expected


def test_retry_after_http_date(shared_redis, monkeypatch):
    monkeypatch.setattr("app.core.ai.llm_client.time.time", lambda: 1_800_000_000)
    header = format_datetime(datetime.fromtimestamp(1_800_000_120, UTC), usegmt=True)
    assert make_client(shared_redis)._upstream_cooldown_seconds(upstream_error(429, {"retry-after": header})) == 120


async def test_sdk_timeout_creates_short_cooldown(shared_redis, sdk):
    provider, _ = sdk
    provider.chat.completions.create.side_effect = APITimeoutError(request=MagicMock())
    client = make_client(shared_redis)
    with pytest.raises(APITimeoutError):
        await call(client)
    assert shared_redis.values[client._upstream_cooldown_key("model-a")][1] == 30


async def test_total_timeout_cools_down_but_caller_cancellation_does_not(shared_redis, sdk):
    provider, _ = sdk

    async def stalled(**kwargs):
        await asyncio.Event().wait()

    provider.chat.completions.create.side_effect = stalled
    client = make_client(shared_redis)
    client.OPENAI_TOTAL_TIMEOUT_SECONDS = 0.01
    with pytest.raises(TimeoutError):
        await call(client)
    assert await client.cache.get(client._upstream_cooldown_key("model-a")) == "1"
    provider.close.assert_awaited_once()

    shared_redis.values.clear()
    client.OPENAI_TOTAL_TIMEOUT_SECONDS = 10
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(call(client), timeout=0.01)
    assert shared_redis.values == {}
    assert provider.close.await_count == 2


async def test_redis_failure_preserves_original_provider_error(shared_redis, sdk):
    provider, _ = sdk
    original = upstream_error(503)
    provider.chat.completions.create.side_effect = original
    shared_redis.get = AsyncMock(side_effect=ConnectionError("cache unavailable"))
    shared_redis.eval = AsyncMock(side_effect=ConnectionError("cache unavailable"))
    with pytest.raises(RuntimeError) as failed:
        await call(make_client(shared_redis))
    assert failed.value is original
    provider.close.assert_awaited_once()


async def test_keyword_fallback_during_cooldown_avoids_upstream_and_sensitive_logs(shared_redis, sdk):
    client = make_client(shared_redis)
    model = client.model_for("balanced")
    await client._record_upstream_cooldown(client._upstream_cooldown_key(model), upstream_error(429))
    generator = KeywordGenerator(client)
    generator.logger = MagicMock()

    keywords = await generator.generate(category="demand", count=5)
    assert len(keywords) == 5
    sdk[1].assert_not_called()
    error_fields = generator.logger.error.call_args.kwargs
    assert error_fields["error_code"] == "AI_PROVIDER_COOLDOWN"
    assert "error" not in error_fields

    generator.llm = SimpleNamespace(generate=AsyncMock(side_effect=upstream_error(503)))
    await generator.generate(category="demand", count=5)
    assert generator.logger.error.call_args.kwargs["status_code"] == 503
    assert "private request material" not in str(generator.logger.mock_calls)
