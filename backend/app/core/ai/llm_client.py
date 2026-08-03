"""
LLM Client Module

Provides unified interface for LLM API calls with caching and cost tracking.

Features:
- Multiple provider support (OpenAI, Anthropic, local)
- Request caching
- Cost tracking
- Rate limiting
"""

import hashlib
import inspect
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

import structlog

from app.core.config import settings
from app.core.redis import RedisCache

logger = structlog.get_logger()


class LLMProvider(str, Enum):
    """LLM provider types."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    LOCAL = "local"


@dataclass
class LLMResponse:
    """LLM API response."""

    content: str
    model: str
    tokens_used: int
    cost: float
    cached: bool = False


@dataclass
class CostStats:
    """LLM cost statistics."""

    total_requests: int = 0
    total_tokens: int = 0
    total_cost: float = 0.0
    cache_hits: int = 0


class LLMClient:
    """
    Unified LLM client with caching and cost tracking.

    Supports multiple providers with automatic model selection.
    """

    # Model configurations
    MODELS = {
        LLMProvider.OPENAI: {
            "fast": "gpt-5.6-luna",
            "balanced": "gpt-4o",
            "quality": "gpt-4-turbo",
        },
        LLMProvider.ANTHROPIC: {
            "fast": "claude-3-haiku-20240307",
            "balanced": "claude-3-sonnet-20240229",
            "quality": "claude-3-opus-20240229",
        },
        LLMProvider.LOCAL: {
            "fast": "qwen2.5:7b",
            "balanced": "qwen2.5:14b",
            "quality": "qwen2.5:72b",
        },
    }

    # Token pricing per 1K tokens
    PRICING = {
        "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
        "gpt-4o": {"input": 0.005, "output": 0.015},
        "claude-3-haiku": {"input": 0.00025, "output": 0.00125},
        "claude-3-sonnet": {"input": 0.003, "output": 0.015},
        "claude-3-opus": {"input": 0.015, "output": 0.075},
    }

    def __init__(
        self,
        provider: LLMProvider = LLMProvider.OPENAI,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        cache_ttl: int = 3600,
    ):
        """
        Initialize LLM client.

        Args:
            provider: LLM provider to use
            api_key: API key for the provider
            base_url: Optional OpenAI-compatible API base URL
            cache_ttl: Cache TTL in seconds
        """
        self.provider = provider
        self.api_key = api_key or (
            settings.OPENAI_API_KEY if provider == LLMProvider.OPENAI else settings.ANTHROPIC_API_KEY
        )
        raw_base_url = base_url or (settings.OPENAI_BASE_URL if provider == LLMProvider.OPENAI else None)
        self.base_url = self._normalize_openai_base_url(raw_base_url) if provider == LLMProvider.OPENAI else None
        self.cache = RedisCache()
        self.cache_ttl = cache_ttl
        self.stats = CostStats()
        self.logger = logger.bind(module="llm_client")

    def model_for(self, tier: str) -> str:
        """Resolve a model tier using runtime configuration where applicable."""
        if self.provider == LLMProvider.OPENAI:
            if tier == "fast":
                return settings.LLM_FAST_MODEL
            if tier == "balanced":
                return settings.LLM_MODEL
        return self.MODELS[self.provider][tier]

    @staticmethod
    def _normalize_openai_base_url(base_url: Optional[str]) -> Optional[str]:
        if not base_url:
            return None
        normalized = base_url.rstrip("/")
        if normalized.endswith("/v1") or "/v1/" in normalized:
            return normalized
        return f"{normalized}/v1"

    async def generate(
        self,
        prompt: str,
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 500,
        system_prompt: Optional[str] = None,
    ) -> str:
        """
        Generate content using LLM.

        Args:
            prompt: User prompt
            model: Specific model to use
            temperature: Sampling temperature
            max_tokens: Maximum tokens to generate
            system_prompt: Optional system prompt

        Returns:
            Generated content
        """
        if model is None:
            model = self.model_for("balanced")

        cache_key = self._get_cache_key(prompt, model, temperature, system_prompt)
        cached = await self.cache.get(cache_key)

        if cached:
            self.stats.cache_hits += 1
            self.logger.debug("cache_hit", key=cache_key[:20])
            return cached

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        try:
            if self.provider == LLMProvider.OPENAI:
                response = await self._call_openai(messages, model, temperature, max_tokens)
            elif self.provider == LLMProvider.ANTHROPIC:
                response = await self._call_anthropic(messages, model, temperature, max_tokens)
            else:
                response = await self._call_local(prompt, model, temperature, max_tokens)

            self.stats.total_requests += 1
            self.stats.total_tokens += self._estimate_tokens(prompt, response)
            self.stats.total_cost += self._calculate_cost(model, self._estimate_tokens(prompt, response))

            await self.cache.set(cache_key, response, ttl=self.cache_ttl)

            self.logger.debug(
                "llm_response",
                model=model,
                tokens=self.stats.total_tokens,
                cached=False,
            )

            return response

        except Exception as e:
            self.logger.error("llm_error", provider=self.provider.value, error=str(e))
            raise

    async def _call_openai(
        self,
        messages: list[dict],
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Call OpenAI API."""
        try:
            from openai import AsyncOpenAI

            client_kwargs: dict[str, Any] = {"api_key": self.api_key}
            if self.base_url:
                client_kwargs["base_url"] = self.base_url
            client = AsyncOpenAI(**client_kwargs)
            try:
                response = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            finally:
                close_result = client.close()
                if inspect.isawaitable(close_result):
                    await close_result

            return self._extract_openai_content(response)

        except ImportError:
            self.logger.warning("openai_not_installed")
            return await self._call_fallback("openai")

    @staticmethod
    def _extract_openai_content(response: Any) -> str:
        """Extract text from OpenAI-compatible SDK or proxy responses."""
        if isinstance(response, str):
            return response

        output_text = getattr(response, "output_text", None)
        if isinstance(output_text, str):
            return output_text

        choices = response.get("choices") if isinstance(response, dict) else getattr(response, "choices", None)

        if not isinstance(response, dict) and not choices:
            model_dump = getattr(response, "model_dump", None)
            if callable(model_dump):
                response = model_dump()

        choices = response.get("choices") if isinstance(response, dict) else getattr(response, "choices", None)
        if choices:
            choice = choices[0]
            message = choice.get("message") if isinstance(choice, dict) else getattr(choice, "message", None)
            content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts: list[str] = []
                for item in content:
                    if isinstance(item, str):
                        parts.append(item)
                    elif isinstance(item, dict):
                        text = item.get("text") or item.get("content")
                        if isinstance(text, str):
                            parts.append(text)
                return "\n".join(parts)

            text = choice.get("text") if isinstance(choice, dict) else getattr(choice, "text", None)
            if isinstance(text, str):
                return text

        if isinstance(response, dict):
            for key in ("content", "text", "response"):
                value = response.get(key)
                if isinstance(value, str):
                    return value

        return ""

    async def _call_anthropic(
        self,
        messages: list[dict],
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Call Anthropic API."""
        try:
            from anthropic import AsyncAnthropic

            client = AsyncAnthropic(api_key=self.api_key)

            system = None
            user_messages = []
            for msg in messages:
                if msg["role"] == "system":
                    system = msg["content"]
                else:
                    user_messages.append(msg)

            response = await client.messages.create(
                model=model,
                system=system,
                messages=user_messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )

            return response.content[0].text

        except ImportError:
            self.logger.warning("anthropic_not_installed")
            return await self._call_fallback("anthropic")

    async def _call_local(
        self,
        prompt: str,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Call local LLM (Ollama)."""
        import aiohttp

        url = "http://localhost:11434/api/generate"

        async with aiohttp.ClientSession() as session:
            payload = {
                "model": model,
                "prompt": prompt,
                "temperature": temperature,
                "options": {"num_predict": max_tokens},
            }

            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("response", "")
                else:
                    return await self._call_fallback("local")

    async def _call_fallback(self, provider: str) -> str:
        """Fallback when API is not available."""
        self.logger.warning("using_fallback", provider=provider)
        return "API not configured. Please set API key."

    def _get_cache_key(
        self,
        prompt: str,
        model: Optional[str],
        temperature: float,
        system_prompt: Optional[str],
    ) -> str:
        """Generate cache key for prompt."""
        content = f"{prompt}:{model}:{temperature}:{system_prompt}"
        return f"llm:{hashlib.md5(content.encode()).hexdigest()}"

    def _estimate_tokens(self, prompt: str, response: str) -> int:
        """Estimate token count (rough approximation)."""
        return int((len(prompt) + len(response)) / 4)

    def _calculate_cost(self, model: str, tokens: int) -> float:
        """Calculate API cost."""
        pricing = self.PRICING.get(model, {"input": 0.001, "output": 0.002})
        return (tokens / 1000) * (pricing["input"] + pricing["output"])

    def get_stats(self) -> dict:
        """Get cost statistics."""
        return {
            "total_requests": self.stats.total_requests,
            "total_tokens": self.stats.total_tokens,
            "total_cost": self.stats.total_cost,
            "cache_hits": self.stats.cache_hits,
            "cache_hit_rate": (
                self.stats.cache_hits / max(self.stats.total_requests, 1) * 100
            ),
        }

    def reset_stats(self) -> None:
        """Reset statistics."""
        self.stats = CostStats()
