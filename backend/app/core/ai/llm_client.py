"""
LLM Client Module

Provides unified interface for LLM API calls with caching and cost tracking.

Features:
- Multiple provider support (OpenAI, Anthropic, local)
- Request caching
- Cost tracking
- Rate limiting
"""

import asyncio
import hashlib
import inspect
import json
import math
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from email.utils import parsedate_to_datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Literal, Optional
from urllib.parse import quote, unquote, urlsplit
from uuid import UUID

import structlog

from app.core.config import settings
from app.core.persona_observability import record_llm_usage
from app.core.redis import RedisCache

logger = structlog.get_logger()


class LLMProvider(str, Enum):  # noqa: UP042 - preserve existing public Enum semantics
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
    provider: str = ""
    usage: Mapping[str, int] = field(default_factory=dict)
    finish_reason: str | None = None
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class LLMProviderCapabilities:
    """Immutable capabilities advertised by one provider adapter."""

    provider: LLMProvider
    supports_system_role: bool


class LLMClientError(RuntimeError):
    """Stable, content-free error returned by the stage-three LLM path."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


_LOWER_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CACHE_SCOPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,255}$")
_PERSONA_PROMPT_TEMPLATE_VERSIONS = frozenset({"owned-group-persona-v1"})


def _is_strict_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True, slots=True)
class LLMCacheContext:
    """Complete business isolation material for the Persona LLM v2 cache."""

    cache_scope: str
    execution_id: int | None
    account_id: int
    persona_revision: int | None
    generation_attempt: int
    prompt_template_version: str
    persona_hash: str
    governance_rules_hash: str | None
    policy_revision: int
    asset_id: int
    content_category: Literal["community", "promotion"]
    preview_nonce: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.cache_scope, str) or not _CACHE_SCOPE_RE.fullmatch(
            self.cache_scope
        ):
            raise ValueError("cache_scope must be a non-empty, opaque safe identifier")
        for name in ("account_id", "policy_revision", "asset_id"):
            value = getattr(self, name)
            if not _is_strict_int(value) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.execution_id is not None and (
            not _is_strict_int(self.execution_id) or self.execution_id <= 0
        ):
            raise ValueError("execution_id must be a positive integer or null")
        if self.persona_revision is not None and (
            not _is_strict_int(self.persona_revision) or self.persona_revision < 0
        ):
            raise ValueError("persona_revision must be a non-negative integer or null")
        if not _is_strict_int(self.generation_attempt) or not 1 <= self.generation_attempt <= 3:
            raise ValueError("generation_attempt must be between 1 and 3")
        if self.prompt_template_version not in _PERSONA_PROMPT_TEMPLATE_VERSIONS:
            raise ValueError("prompt_template_version is unsupported")
        if not _LOWER_SHA256_RE.fullmatch(self.persona_hash):
            raise ValueError("persona_hash must be a lowercase SHA-256 digest")
        if self.governance_rules_hash is not None and not _LOWER_SHA256_RE.fullmatch(
            self.governance_rules_hash
        ):
            raise ValueError("governance_rules_hash must be a lowercase SHA-256 digest or null")
        if self.content_category not in {"community", "promotion"}:
            raise ValueError("content_category is unsupported")

        is_preview = self.execution_id is None
        if is_preview:
            if self.preview_nonce is None:
                raise ValueError("preview calls require preview_nonce")
            try:
                parsed_nonce = UUID(self.preview_nonce)
            except (TypeError, ValueError, AttributeError) as exc:
                raise ValueError("preview_nonce must be a UUID") from exc
            if str(parsed_nonce) != self.preview_nonce.lower():
                raise ValueError("preview_nonce must use canonical UUID form")
        elif self.preview_nonce is not None:
            raise ValueError("execution calls must not provide preview_nonce")
        elif self.persona_revision is None:
            raise ValueError("execution calls require persona_revision")


_PROVIDER_CAPABILITIES: Mapping[LLMProvider, LLMProviderCapabilities] = MappingProxyType(
    {
        LLMProvider.OPENAI: LLMProviderCapabilities(
            provider=LLMProvider.OPENAI,
            supports_system_role=True,
        ),
        LLMProvider.ANTHROPIC: LLMProviderCapabilities(
            provider=LLMProvider.ANTHROPIC,
            supports_system_role=True,
        ),
        LLMProvider.LOCAL: LLMProviderCapabilities(
            provider=LLMProvider.LOCAL,
            supports_system_role=False,
        ),
    }
)


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

    # A conservative common denominator for the stage-three adapters.  Callers
    # must trim optional Prompt material before reaching this final fail-closed
    # gate; max_tokens is the reserved response budget.
    PERSONA_CONTEXT_TOKEN_LIMIT = 8192

    # The SDK owns retries and Retry-After backoff. Bound the full call as well
    # so repeated SDK timeouts cannot occupy a keyword worker for many minutes.
    OPENAI_REQUEST_TIMEOUT_SECONDS = 45.0
    OPENAI_TOTAL_TIMEOUT_SECONDS = 90.0
    OPENAI_MAX_RETRIES = 2
    UPSTREAM_COOLDOWN_SECONDS = 30
    RATE_LIMIT_COOLDOWN_SECONDS = 60
    MAX_UPSTREAM_COOLDOWN_SECONDS = 300

    # Model configurations
    MODELS = {
        LLMProvider.OPENAI: {
            "fast": "gpt-5.6-sol",
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
        api_key: str | None = None,
        base_url: str | None = None,
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

    def capabilities(self) -> LLMProviderCapabilities:
        """Return immutable local capabilities without making a provider request."""

        return _PROVIDER_CAPABILITIES[self.provider]

    def provider_endpoint_sha256(self) -> str:
        """Hash the credential-free canonical endpoint used by this client."""

        if self.provider != LLMProvider.OPENAI or not self.base_url:
            material = f"official:{self.provider.value}:default"
        else:
            canonical_endpoint = self._canonical_custom_endpoint(self.base_url)
            material = (
                "official:openai:default"
                if canonical_endpoint == "https://api.openai.com/v1"
                else canonical_endpoint
            )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @staticmethod
    def _canonical_custom_endpoint(base_url: str) -> str:
        """Return a credential-free canonical URL suitable only for hashing."""

        try:
            parsed = urlsplit(base_url)
            port = parsed.port
        except ValueError as exc:
            raise LLMClientError("AI_PROVIDER_UNSAFE", "AI provider endpoint is invalid") from exc
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"} or not parsed.hostname:
            raise LLMClientError("AI_PROVIDER_UNSAFE", "AI provider endpoint is invalid")
        if parsed.username is not None or parsed.password is not None:
            raise LLMClientError("AI_PROVIDER_UNSAFE", "AI provider endpoint cannot contain userinfo")
        if parsed.query or parsed.fragment:
            raise LLMClientError("AI_PROVIDER_UNSAFE", "AI provider endpoint cannot contain query data")
        try:
            host = parsed.hostname.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise LLMClientError("AI_PROVIDER_UNSAFE", "AI provider endpoint host is invalid") from exc
        if not host:
            raise LLMClientError("AI_PROVIDER_UNSAFE", "AI provider endpoint host is invalid")
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
        authority = host if port is None or default_port else f"{host}:{port}"
        decoded_path = unquote(parsed.path or "")
        normalized_path = "/".join(part for part in decoded_path.split("/") if part)
        encoded_path = quote(normalized_path, safe="@-._~!$&'()*+,;=:")
        path = f"/{encoded_path}" if encoded_path else ""
        return f"{scheme}://{authority}{path}"

    def build_v2_cache_key(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        cache_context: LLMCacheContext,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> tuple[str, Mapping[str, Any]]:
        """Build the complete canonical Persona cache key without raw prompt data."""

        if not isinstance(cache_context, LLMCacheContext):
            raise TypeError("cache_context must be LLMCacheContext")
        if not isinstance(system_prompt, str) or not isinstance(user_prompt, str):
            raise TypeError("system_prompt and user_prompt must be strings")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
            raise TypeError("temperature must be numeric")
        if not math.isfinite(float(temperature)) or not 0 <= float(temperature) <= 2:
            raise ValueError("temperature must be between 0 and 2")
        if not _is_strict_int(max_tokens) or max_tokens <= 0:
            raise ValueError("max_tokens must be a positive integer")

        material: dict[str, Any] = {
            "cache_schema_version": "llm:v2",
            "provider": self.provider.value,
            "model": model,
            "temperature": float(temperature),
            "max_tokens": max_tokens,
            "system_prompt_sha256": hashlib.sha256(system_prompt.encode("utf-8")).hexdigest(),
            "user_prompt_sha256": hashlib.sha256(user_prompt.encode("utf-8")).hexdigest(),
            "provider_endpoint_sha256": self.provider_endpoint_sha256(),
            "cache_scope": cache_context.cache_scope,
            "execution_id": cache_context.execution_id,
            "account_id": cache_context.account_id,
            "persona_revision": cache_context.persona_revision,
            "generation_attempt": cache_context.generation_attempt,
            "prompt_template_version": cache_context.prompt_template_version,
            "persona_hash": cache_context.persona_hash,
            "governance_rules_hash": cache_context.governance_rules_hash,
            "policy_revision": cache_context.policy_revision,
            "asset_id": cache_context.asset_id,
            "content_category": cache_context.content_category,
            "preview_nonce": cache_context.preview_nonce,
        }
        canonical = json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return f"llm:v2:{digest}", MappingProxyType(material)

    async def generate_response(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        requires_system_role: bool,
        cache_context: LLMCacheContext,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """Generate a Persona response with strict role and cache isolation contracts."""

        observation_started = time.perf_counter()
        if not isinstance(requires_system_role, bool):
            raise TypeError("requires_system_role must be bool")
        capabilities = self.capabilities()
        if requires_system_role and not capabilities.supports_system_role:
            raise LLMClientError(
                "AI_PROVIDER_SYSTEM_PROMPT_UNSUPPORTED",
                "AI provider does not preserve the system role",
            )

        resolved_model = model if model is not None else self.model_for("balanced")
        resolved_temperature = 0.7 if temperature is None else temperature
        resolved_max_tokens = 500 if max_tokens is None else max_tokens
        estimated_input_tokens = self._estimate_persona_input_tokens(
            system_prompt,
            user_prompt,
        )
        if estimated_input_tokens + resolved_max_tokens > self.PERSONA_CONTEXT_TOKEN_LIMIT:
            raise LLMClientError(
                "AI_PROMPT_TOO_LARGE",
                "AI Prompt exceeds the provider token budget",
            )
        cache_key, _ = self.build_v2_cache_key(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            cache_context=cache_context,
            model=resolved_model,
            temperature=resolved_temperature,
            max_tokens=resolved_max_tokens,
        )

        cached = await self._read_v2_cache(cache_key, resolved_model)
        if cached is not None:
            self.stats.cache_hits += 1
            self.logger.debug("cache_hit", cache_schema="llm:v2", key_sha256=cache_key[7:])
            record_llm_usage(
                execution_id=cache_context.execution_id,
                account_id=cache_context.account_id,
                asset_id=cache_context.asset_id,
                content_category=cache_context.content_category,
                persona_source=self._persona_source_for_observability(cache_context),
                revision=cache_context.persona_revision or 0,
                persona_hash=cache_context.persona_hash,
                prompt_template_version=cache_context.prompt_template_version,
                provider=self.provider.value,
                model=resolved_model,
                input_tokens=0,
                output_tokens=0,
                cost_microunits=0,
                cache_hit=True,
                usage_source="cache",
                result="success",
                duration_ms=int((time.perf_counter() - observation_started) * 1000),
            )
            return cached

        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})

        try:
            if self.provider == LLMProvider.OPENAI:
                raw_response = await self._call_openai(
                    messages,
                    resolved_model,
                    float(resolved_temperature),
                    resolved_max_tokens,
                )
            elif self.provider == LLMProvider.ANTHROPIC:
                raw_response = await self._call_anthropic(
                    messages,
                    resolved_model,
                    float(resolved_temperature),
                    resolved_max_tokens,
                )
            else:
                raw_response = await self._call_local(
                    user_prompt,
                    resolved_model,
                    float(resolved_temperature),
                    resolved_max_tokens,
                )
        except LLMClientError:
            raise
        except Exception as exc:
            self.logger.error(
                "llm_error",
                provider=self.provider.value,
                error_type=type(exc).__name__,
                cache_schema="llm:v2",
            )
            raise LLMClientError("AI_GENERATION_FAILED", "AI provider request failed") from exc

        response = self._coerce_v2_response(
            raw_response,
            model=resolved_model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        self.stats.total_requests += 1
        self.stats.total_tokens += response.tokens_used
        self.stats.total_cost += response.cost
        await self._write_v2_cache(cache_key, response)
        self.logger.debug(
            "llm_response",
            provider=self.provider.value,
            model=resolved_model,
            tokens=response.tokens_used,
            cache_schema="llm:v2",
            cached=False,
        )
        exact_usage = (
            isinstance(raw_response, LLMResponse)
            and _is_strict_int(response.usage.get("input_tokens"))
            and _is_strict_int(response.usage.get("output_tokens"))
            and response.usage["input_tokens"] >= 0
            and response.usage["output_tokens"] >= 0
        )
        input_tokens = (
            int(response.usage["input_tokens"])
            if exact_usage
            else estimated_input_tokens
        )
        output_tokens = (
            int(response.usage["output_tokens"])
            if exact_usage
            else self._estimate_persona_input_tokens("", response.content)
        )
        cost = float(response.cost)
        cost_microunits = (
            int(round(cost * 1_000_000))
            if math.isfinite(cost) and cost >= 0
            else 0
        )
        record_llm_usage(
            execution_id=cache_context.execution_id,
            account_id=cache_context.account_id,
            asset_id=cache_context.asset_id,
            content_category=cache_context.content_category,
            persona_source=self._persona_source_for_observability(cache_context),
            revision=cache_context.persona_revision or 0,
            persona_hash=cache_context.persona_hash,
            prompt_template_version=cache_context.prompt_template_version,
            provider=self.provider.value,
            model=resolved_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_microunits=cost_microunits,
            cache_hit=False,
            usage_source="provider" if exact_usage else "estimated",
            result="success",
            duration_ms=int((time.perf_counter() - observation_started) * 1000),
            request_id=response.request_id,
        )
        return response

    @staticmethod
    def _persona_source_for_observability(cache_context: LLMCacheContext) -> str:
        if cache_context.persona_revision is None:
            return "draft"
        if cache_context.persona_revision == 0:
            return "neutral_default"
        return "configured"

    @staticmethod
    def _estimate_persona_input_tokens(system_prompt: str, user_prompt: str) -> int:
        """Conservatively estimate ASCII and CJK Prompt tokens together."""

        combined = f"{system_prompt}\n{user_prompt}"
        non_ascii = sum(ord(character) > 127 for character in combined)
        ascii_count = len(combined) - non_ascii
        return max(1, non_ascii + (ascii_count + 3) // 4)

    def _coerce_v2_response(
        self,
        raw_response: str | LLMResponse,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
    ) -> LLMResponse:
        if isinstance(raw_response, LLMResponse):
            content = raw_response.content
            response = replace(
                raw_response,
                model=model,
                provider=self.provider.value,
                cached=False,
            )
        elif isinstance(raw_response, str):
            content = raw_response
            input_tokens = self._estimate_persona_input_tokens(system_prompt, user_prompt)
            output_tokens = self._estimate_persona_input_tokens("", content)
            tokens = input_tokens + output_tokens
            response = LLMResponse(
                content=content,
                provider=self.provider.value,
                model=model,
                usage={"input_tokens": input_tokens, "output_tokens": output_tokens},
                tokens_used=tokens,
                cost=self._calculate_cost(model, tokens),
            )
        else:
            raise LLMClientError("AI_GENERATION_FAILED", "AI provider response is invalid")
        if not content.strip():
            raise LLMClientError("AI_GENERATION_FAILED", "AI provider returned empty content")
        if content == "API not configured. Please set API key.":
            raise LLMClientError("AI_PROVIDER_UNSAFE", "AI provider fallback is unsafe")
        return response

    async def _read_v2_cache(self, cache_key: str, model: str) -> LLMResponse | None:
        try:
            raw_value = await self.cache.get(cache_key)
        except Exception as exc:
            self.logger.warning(
                "cache_read_failed",
                cache_schema="llm:v2",
                error_type=type(exc).__name__,
            )
            return None
        if not raw_value:
            return None
        try:
            value = json.loads(raw_value)
            if not isinstance(value, dict) or set(value) != {
                "content",
                "provider",
                "model",
                "usage",
                "tokens_used",
                "cost",
                "finish_reason",
                "request_id",
            }:
                raise ValueError("invalid envelope fields")
            if value["provider"] != self.provider.value or value["model"] != model:
                raise ValueError("cache envelope provider mismatch")
            if not isinstance(value["content"], str) or not value["content"].strip():
                raise ValueError("empty cached content")
            if value["content"] == "API not configured. Please set API key.":
                raise ValueError("unsafe cached fallback content")
            usage = value["usage"]
            if not isinstance(usage, dict) or any(
                not isinstance(key, str) or not _is_strict_int(item) or item < 0
                for key, item in usage.items()
            ):
                raise ValueError("invalid cached usage")
            if (
                not _is_strict_int(value["tokens_used"])
                or value["tokens_used"] < 0
                or isinstance(value["cost"], bool)
                or not isinstance(value["cost"], (int, float))
                or not math.isfinite(float(value["cost"]))
                or value["cost"] < 0
                or value["finish_reason"] is not None
                and not isinstance(value["finish_reason"], str)
                or value["request_id"] is not None
                and not isinstance(value["request_id"], str)
            ):
                raise ValueError("invalid cached response metadata")
            return LLMResponse(
                content=value["content"],
                provider=value["provider"],
                model=value["model"],
                usage=usage,
                tokens_used=int(value["tokens_used"]),
                cost=float(value["cost"]),
                finish_reason=value["finish_reason"],
                request_id=value["request_id"],
                cached=True,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            self.logger.warning(
                "cache_value_invalid",
                cache_schema="llm:v2",
                error_type=type(exc).__name__,
            )
            return None

    async def _write_v2_cache(self, cache_key: str, response: LLMResponse) -> None:
        try:
            value = {
                "content": response.content,
                "provider": response.provider,
                "model": response.model,
                "usage": dict(response.usage),
                "tokens_used": response.tokens_used,
                "cost": response.cost,
                "finish_reason": response.finish_reason,
                "request_id": response.request_id,
            }
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            await self.cache.set(cache_key, encoded, ttl=min(max(self.cache_ttl, 1), 3600))
        except Exception as exc:
            self.logger.warning(
                "cache_write_failed",
                cache_schema="llm:v2",
                error_type=type(exc).__name__,
            )

    @staticmethod
    def _normalize_openai_base_url(base_url: str | None) -> str | None:
        if not base_url:
            return None
        normalized = base_url.rstrip("/")
        if normalized.endswith("/v1") or "/v1/" in normalized:
            return normalized
        return f"{normalized}/v1"

    @staticmethod
    def is_temporary_upstream_error(exc: Exception) -> bool:
        """Return whether an OpenAI-compatible failure is safe to retry later."""

        if getattr(exc, "status_code", None) in {429, 502, 503, 504}:
            return True
        if isinstance(exc, TimeoutError):
            return True
        try:
            from openai import APIConnectionError
        except ImportError:
            return False
        return isinstance(exc, APIConnectionError)

    async def generate(
        self,
        prompt: str,
        model: Optional[str] = None,  # noqa: UP045 - preserve public signature
        temperature: float = 0.7,
        max_tokens: int = 500,
        system_prompt: Optional[str] = None,  # noqa: UP045 - preserve public signature
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
            self.logger.error(
                "llm_error",
                provider=self.provider.value,
                error_type=type(e).__name__,
            )
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
            from openai import AsyncOpenAI, Timeout

            cooldown_key = self._upstream_cooldown_key(model)
            await self._check_upstream_cooldown(cooldown_key)
            client_kwargs: dict[str, Any] = {
                "api_key": self.api_key,
                "timeout": Timeout(self.OPENAI_REQUEST_TIMEOUT_SECONDS, connect=5.0),
                "max_retries": self.OPENAI_MAX_RETRIES,
            }
            if self.base_url:
                client_kwargs["base_url"] = self.base_url
            client = AsyncOpenAI(**client_kwargs)
            try:
                async with asyncio.timeout(self.OPENAI_TOTAL_TIMEOUT_SECONDS):
                    response = await client.chat.completions.create(
                        model=model,
                        messages=messages,
                        temperature=temperature,
                        max_tokens=max_tokens,
                    )
            except Exception as exc:
                # Caller cancellation (for example the group audit's 45s
                # deadline) is not an upstream outage and must propagate.
                if self.is_temporary_upstream_error(exc):
                    await self._record_upstream_cooldown(cooldown_key, exc)
                raise
            finally:
                close_result = client.close()
                if inspect.isawaitable(close_result):
                    await close_result

            return self._extract_openai_content(response)

        except ImportError:
            self.logger.warning("openai_not_installed")
            return await self._call_fallback("openai")

    def _upstream_cooldown_key(self, model: str) -> str:
        """Share backoff across workers without exposing endpoint or credentials."""
        material = json.dumps(
            [
                self.provider.value,
                self.base_url or "https://api.openai.com/v1",
                model,
                hashlib.sha256((self.api_key or "").encode()).hexdigest(),
            ],
            separators=(",", ":"),
        )
        return "llm:upstream-cooldown:" + hashlib.sha256(material.encode()).hexdigest()

    async def _check_upstream_cooldown(self, key: str) -> None:
        try:
            active = await self.cache.get(key)
        except Exception as exc:
            self.logger.warning("llm_cooldown_read_failed", error_type=type(exc).__name__)
            return
        if active:
            raise LLMClientError("AI_PROVIDER_COOLDOWN", "AI provider is temporarily cooling down")

    def _upstream_cooldown_seconds(self, exc: Exception) -> int:
        seconds = (
            self.RATE_LIMIT_COOLDOWN_SECONDS
            if getattr(exc, "status_code", None) == 429
            else self.UPSTREAM_COOLDOWN_SECONDS
        )
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", {})
        for header, multiplier in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
            raw_value = headers.get(header)
            if raw_value is None:
                continue
            try:
                retry_after = float(raw_value) * multiplier
            except (TypeError, ValueError):
                if header != "retry-after":
                    continue
                try:
                    retry_after = parsedate_to_datetime(raw_value).timestamp() - time.time()
                except (TypeError, ValueError, OverflowError, OSError):
                    continue
            if math.isfinite(retry_after) and retry_after > 0:
                seconds = max(seconds, math.ceil(min(retry_after, self.MAX_UPSTREAM_COOLDOWN_SECONDS)))
                break
        return min(seconds, self.MAX_UPSTREAM_COOLDOWN_SECONDS)

    async def _record_upstream_cooldown(self, key: str, exc: Exception) -> None:
        seconds = self._upstream_cooldown_seconds(exc)
        try:
            # Concurrent account workers may see different Retry-After values.
            # Extend the shared deadline atomically; never shorten a longer one.
            redis_client = self.cache.client
            stored = False
            if redis_client is not None:
                await redis_client.eval(
                    "if redis.call('TTL', KEYS[1]) < tonumber(ARGV[1]) then "
                    "redis.call('SET', KEYS[1], '1', 'EX', ARGV[1]) end; return 1",
                    1,
                    key,
                    seconds,
                )
                stored = True
        except Exception as cache_exc:
            self.logger.warning("llm_cooldown_write_failed", error_type=type(cache_exc).__name__)
            stored = False
        self.logger.warning(
            "llm_upstream_unavailable",
            error_type=type(exc).__name__,
            status_code=getattr(exc, "status_code", None),
            cooldown_seconds=seconds,
            cooldown_shared=bool(stored),
        )

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
        model: str | None,
        temperature: float,
        system_prompt: str | None,
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
