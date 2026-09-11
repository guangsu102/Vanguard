"""Shared fail-closed AI token budget for owned-group messaging."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.core.redis import RedisCache
from app.modules.owned_group.messaging_contracts import OwnedGroupMessagingError

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_BUDGET_TTL_SECONDS = 48 * 3600
_RESERVE_AI_BUDGET_LUA = """
local current_raw = redis.call('GET', KEYS[1])
local current = tonumber(current_raw or '0')
local budget = tonumber(ARGV[1])
local amount = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
if current == nil or budget == nil or amount == nil or ttl == nil then
    return -2
end
if current + amount > budget then
    return -1
end
local updated = redis.call('INCRBY', KEYS[1], amount)
if redis.call('TTL', KEYS[1]) < 0 then
    redis.call('EXPIRE', KEYS[1], ttl)
end
return updated
"""


class OwnedGroupAIBudgetGate:
    """Atomically reserve the conservative output-token estimate before an LLM call."""

    def __init__(self, cache: RedisCache | None = None) -> None:
        self.cache = cache or RedisCache()

    @staticmethod
    def daily_key(now: datetime | None = None) -> str:
        current = now or datetime.now(_SHANGHAI)
        if current.tzinfo is None:
            current = current.replace(tzinfo=_SHANGHAI)
        else:
            current = current.astimezone(_SHANGHAI)
        return f"owned_group:message:ai_tokens:{current.strftime('%Y%m%d')}"

    async def reserve(
        self,
        ai_settings: Mapping[str, Any],
        *,
        estimated_tokens: int | None = None,
        max_tokens_cap: int = 500,
    ) -> int:
        try:
            budget = int(ai_settings["dailyTokenBudget"])
            estimate = (
                int(estimated_tokens)
                if estimated_tokens is not None
                else int(ai_settings.get("maxTokens", 180) or 180)
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise self._unavailable_error() from exc

        estimate = max(1, min(estimate, max(int(max_tokens_cap), 1)))
        if budget <= 0:
            raise self._reached_error(budget=budget, estimate=estimate)

        client = getattr(self.cache, "client", None)
        if client is None:
            raise self._unavailable_error()
        try:
            reserved = int(
                await client.eval(
                    _RESERVE_AI_BUDGET_LUA,
                    1,
                    self.daily_key(),
                    budget,
                    estimate,
                    _BUDGET_TTL_SECONDS,
                )
            )
        except Exception as exc:
            raise self._unavailable_error() from exc
        if reserved == -1:
            raise self._reached_error(budget=budget, estimate=estimate)
        if reserved < 0:
            raise self._unavailable_error()
        return reserved

    @staticmethod
    def _reached_error(*, budget: int, estimate: int) -> OwnedGroupMessagingError:
        return OwnedGroupMessagingError(
            "AI_TOKEN_BUDGET_REACHED",
            "群 AI 每日 Token 预算已耗尽",
            http_status=429,
            details={
                "daily_token_budget": max(int(budget), 0),
                "estimated_tokens": int(estimate),
            },
        )

    @staticmethod
    def _unavailable_error() -> OwnedGroupMessagingError:
        return OwnedGroupMessagingError(
            "AI_TOKEN_BUDGET_UNAVAILABLE",
            "群 AI Token 预算服务不可用",
            http_status=503,
            retryable=True,
        )
