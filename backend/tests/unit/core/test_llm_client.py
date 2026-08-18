"""
Unit Tests for LLM Client Module

Tests cover:
- Client initialization
- Model selection
- OpenAI API calls (mocked)
- Anthropic API calls (mocked)
- Local model calls (mocked)
- Request caching
- Cost tracking
- Error handling
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass

from app.core.ai.llm_client import (
    LLMClient,
    LLMProvider,
    LLMResponse,
    CostStats,
)


class TestLLMClient:
    """Test LLMClient class."""

    def test_init_default(self):
        """Test initialization with defaults."""
        client = LLMClient()
        assert client.provider == LLMProvider.OPENAI
        assert client.api_key is None

    def test_init_openai(self):
        """Test initialization with OpenAI provider."""
        client = LLMClient(provider=LLMProvider.OPENAI, api_key="test-key")
        assert client.provider == LLMProvider.OPENAI
        assert client.api_key == "test-key"

    def test_init_anthropic(self):
        """Test initialization with Anthropic provider."""
        client = LLMClient(provider=LLMProvider.ANTHROPIC, api_key="test-key")
        assert client.provider == LLMProvider.ANTHROPIC

    def test_init_local(self):
        """Test initialization with Local provider."""
        client = LLMClient(provider=LLMProvider.LOCAL)
        assert client.provider == LLMProvider.LOCAL

    def test_cache_ttl_default(self):
        """Test default cache TTL."""
        client = LLMClient()
        assert client.cache_ttl == 3600

    def test_cache_ttl_custom(self):
        """Test custom cache TTL."""
        client = LLMClient(cache_ttl=7200)
        assert client.cache_ttl == 7200

    def test_stats_initialized(self):
        """Test stats are initialized."""
        client = LLMClient()
        assert client.stats.total_requests == 0
        assert client.stats.total_tokens == 0
        assert client.stats.total_cost == 0.0
        assert client.stats.cache_hits == 0


class TestModels:
    """Test model configurations."""

    def test_openai_models_defined(self):
        """Test OpenAI models are defined."""
        models = LLMClient.MODELS[LLMProvider.OPENAI]
        assert "fast" in models
        assert "balanced" in models
        assert "quality" in models
        assert models["fast"] == "gpt-5.6-terra"

    def test_openai_model_tiers_use_runtime_settings(self, monkeypatch):
        """Test configurable OpenAI model tiers."""
        from app.core.config import settings

        monkeypatch.setattr(settings, "LLM_FAST_MODEL", "fast-test-model")
        monkeypatch.setattr(settings, "LLM_MODEL", "balanced-test-model")
        client = LLMClient(provider=LLMProvider.OPENAI)

        assert client.model_for("fast") == "fast-test-model"
        assert client.model_for("balanced") == "balanced-test-model"

    def test_anthropic_models_defined(self):
        """Test Anthropic models are defined."""
        models = LLMClient.MODELS[LLMProvider.ANTHROPIC]
        assert "fast" in models
        assert "balanced" in models
        assert "quality" in models

    def test_local_models_defined(self):
        """Test Local models are defined."""
        models = LLMClient.MODELS[LLMProvider.LOCAL]
        assert "fast" in models
        assert "balanced" in models
        assert "quality" in models


class TestPricing:
    """Test pricing configuration."""

    def test_gpt4o_mini_pricing(self):
        """Test GPT-4o-mini pricing."""
        pricing = LLMClient.PRICING["gpt-4o-mini"]
        assert pricing["input"] == 0.00015
        assert pricing["output"] == 0.0006

    def test_gpt4o_pricing(self):
        """Test GPT-4o pricing."""
        pricing = LLMClient.PRICING["gpt-4o"]
        assert pricing["input"] == 0.005
        assert pricing["output"] == 0.015

    def test_claude_pricing(self):
        """Test Claude pricing."""
        pricing = LLMClient.PRICING["claude-3-haiku"]
        assert pricing["input"] == 0.00025
        assert pricing["output"] == 0.00125


class TestGenerate:
    """Test generate method."""

    @pytest.fixture
    def client(self):
        """Create LLMClient instance."""
        return LLMClient(provider=LLMProvider.OPENAI, api_key="test-key")

    @pytest.mark.asyncio
    async def test_generate_without_cache(self, client):
        """Test generation without cache hit."""
        with patch.object(client, '_call_openai', new_callable=AsyncMock) as mock_call:
            mock_call.return_value = "Test response"
            response = await client.generate("Test prompt")

            assert response == "Test response"
            mock_call.assert_called_once()

    @pytest.mark.asyncio
    async def test_generate_with_specific_model(self, client):
        """Test generation with specific model."""
        with patch.object(client, '_call_openai', new_callable=AsyncMock) as mock_call:
            mock_call.return_value = "Test response"
            await client.generate("Test prompt", model="gpt-4o")

            mock_call.assert_called_once()
            call_args = mock_call.call_args
            assert call_args[0][1] == "gpt-4o"

    @pytest.mark.asyncio
    async def test_generate_updates_stats(self, client):
        """Test generation updates statistics."""
        with patch.object(client, '_call_openai', new_callable=AsyncMock) as mock_call:
            mock_call.return_value = "Test response"

            initial_requests = client.stats.total_requests
            await client.generate("Test prompt")

            assert client.stats.total_requests == initial_requests + 1

    @pytest.mark.asyncio
    async def test_generate_with_system_prompt(self, client):
        """Test generation with system prompt."""
        with patch.object(client, '_call_openai', new_callable=AsyncMock) as mock_call:
            mock_call.return_value = "Test response"
            await client.generate(
                "User prompt",
                system_prompt="You are a helpful assistant"
            )

            mock_call.assert_called_once()
            call_args = mock_call.call_args
            messages = call_args[0][0]
            assert len(messages) == 2
            assert messages[0]["role"] == "system"

    @pytest.mark.asyncio
    async def test_generate_with_temperature(self, client):
        """Test generation with custom temperature."""
        with patch.object(client, '_call_openai', new_callable=AsyncMock) as mock_call:
            mock_call.return_value = "Test response"
            await client.generate("Test prompt", temperature=0.5)

            mock_call.assert_called_once()
            call_args = mock_call.call_args
            assert call_args[0][2] == 0.5

    @pytest.mark.asyncio
    async def test_generate_with_max_tokens(self, client):
        """Test generation with custom max tokens."""
        with patch.object(client, '_call_openai', new_callable=AsyncMock) as mock_call:
            mock_call.return_value = "Test response"
            await client.generate("Test prompt", max_tokens=1000)

            mock_call.assert_called_once()
            call_args = mock_call.call_args
            assert call_args[0][3] == 1000


class TestCallOpenAI:
    """Test OpenAI API call."""

    @pytest.fixture
    def client(self):
        """Create LLMClient instance."""
        return LLMClient(provider=LLMProvider.OPENAI, api_key="test-key")

    @pytest.mark.asyncio
    async def test_call_openai_success(self, client):
        """Test successful OpenAI API call."""
        messages = [{"role": "user", "content": "Test"}]

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Test response"

        with patch('openai.AsyncOpenAI') as mock_openai:
            mock_client = MagicMock()
            mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
            mock_openai.return_value = mock_client

            result = await client._call_openai(messages, "gpt-4o", 0.7, 500)

            assert result == "Test response"

    @pytest.mark.asyncio
    async def test_call_openai_import_error(self, client):
        """Test OpenAI call with import error."""
        messages = [{"role": "user", "content": "Test"}]

        with patch('openai.AsyncOpenAI', side_effect=ImportError):
            result = await client._call_openai(messages, "gpt-4o", 0.7, 500)

            assert "not configured" in result.lower()


class TestCallAnthropic:
    """Test Anthropic API call."""

    @pytest.fixture
    def client(self):
        """Create LLMClient instance."""
        return LLMClient(provider=LLMProvider.ANTHROPIC, api_key="test-key")

    @pytest.mark.asyncio
    async def test_call_anthropic_success(self, client):
        """Test successful Anthropic API call."""
        messages = [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "Test"}
        ]

        mock_response = MagicMock()
        mock_response.content = [MagicMock()]
        mock_response.content[0].text = "Test response"

        with patch('anthropic.AsyncAnthropic') as mock_anthropic:
            mock_client = MagicMock()
            mock_client.messages.create = AsyncMock(return_value=mock_response)
            mock_anthropic.return_value = mock_client

            result = await client._call_anthropic(messages, "claude-3-sonnet", 0.7, 500)

            assert result == "Test response"

    @pytest.mark.asyncio
    async def test_call_anthropic_separates_system(self, client):
        """Test Anthropic call separates system message."""
        messages = [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "Test"}
        ]

        mock_response = MagicMock()
        mock_response.content = [MagicMock()]
        mock_response.content[0].text = "Test response"

        with patch('anthropic.AsyncAnthropic') as mock_anthropic:
            mock_client = MagicMock()
            mock_client.messages.create = AsyncMock(return_value=mock_response)
            mock_anthropic.return_value = mock_client

            await client._call_anthropic(messages, "claude-3-sonnet", 0.7, 500)

            call_kwargs = mock_client.messages.create.call_args[1]
            assert call_kwargs["system"] == "System"
            assert call_kwargs["messages"][0]["content"] == "Test"


class TestCallLocal:
    """Test local model (Ollama) API call."""

    @pytest.fixture
    def client(self):
        """Create LLMClient instance."""
        return LLMClient(provider=LLMProvider.LOCAL)

    @pytest.mark.asyncio
    async def test_call_local_fallback(self, client):
        """Test local API call falls back when not configured."""
        # Test the fallback path
        result = await client._call_fallback("local")
        assert "not configured" in result.lower()


class TestCostCalculation:
    """Test cost calculation."""

    @pytest.fixture
    def client(self):
        """Create LLMClient instance."""
        return LLMClient(provider=LLMProvider.OPENAI)

    def test_calculate_cost_known_model(self, client):
        """Test cost calculation for known model."""
        cost = client._calculate_cost("gpt-4o-mini", 1000)
        expected = (1000 / 1000) * (0.00015 + 0.0006)
        assert cost == expected

    def test_calculate_cost_unknown_model(self, client):
        """Test cost calculation for unknown model."""
        cost = client._calculate_cost("unknown-model", 1000)
        expected = (1000 / 1000) * (0.001 + 0.002)
        assert cost == expected

    def test_estimate_tokens(self, client):
        """Test token estimation."""
        tokens = client._estimate_tokens("Hello world", "Response")
        assert tokens > 0


class TestStats:
    """Test cost statistics."""

    @pytest.fixture
    def client(self):
        """Create LLMClient instance."""
        return LLMClient()

    def test_get_stats(self, client):
        """Test getting statistics."""
        stats = client.get_stats()

        assert "total_requests" in stats
        assert "total_tokens" in stats
        assert "total_cost" in stats
        assert "cache_hits" in stats
        assert "cache_hit_rate" in stats

    def test_reset_stats(self, client):
        """Test resetting statistics."""
        client.stats.total_requests = 10
        client.stats.total_tokens = 1000
        client.stats.total_cost = 5.0

        client.reset_stats()

        assert client.stats.total_requests == 0
        assert client.stats.total_tokens == 0
        assert client.stats.total_cost == 0.0


class TestLLMProviderEnum:
    """Test LLMProvider enum."""

    def test_all_providers_defined(self):
        """Test all expected providers are defined."""
        expected = {LLMProvider.OPENAI, LLMProvider.ANTHROPIC, LLMProvider.LOCAL}
        assert set(LLMProvider) == expected

    def test_provider_values(self):
        """Test provider string values."""
        assert LLMProvider.OPENAI.value == "openai"
        assert LLMProvider.ANTHROPIC.value == "anthropic"
        assert LLMProvider.LOCAL.value == "local"


class TestLLMResponse:
    """Test LLMResponse dataclass."""

    def test_create_response(self):
        """Test creating LLMResponse."""
        response = LLMResponse(
            content="Test content",
            model="gpt-4o",
            tokens_used=100,
            cost=0.01,
            cached=False
        )

        assert response.content == "Test content"
        assert response.model == "gpt-4o"
        assert response.tokens_used == 100
        assert response.cost == 0.01
        assert response.cached is False

    def test_response_cached_default(self):
        """Test cached default value."""
        response = LLMResponse(
            content="Test",
            model="gpt-4o",
            tokens_used=50,
            cost=0.005
        )
        assert response.cached is False


class TestCostStats:
    """Test CostStats dataclass."""

    def test_create_cost_stats(self):
        """Test creating CostStats."""
        stats = CostStats(
            total_requests=100,
            total_tokens=10000,
            total_cost=10.0,
            cache_hits=25
        )

        assert stats.total_requests == 100
        assert stats.total_tokens == 10000
        assert stats.total_cost == 10.0
        assert stats.cache_hits == 25

    def test_cost_stats_defaults(self):
        """Test CostStats default values."""
        stats = CostStats()
        assert stats.total_requests == 0
        assert stats.total_tokens == 0
        assert stats.total_cost == 0.0
        assert stats.cache_hits == 0
