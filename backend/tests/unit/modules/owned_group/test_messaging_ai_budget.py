from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.modules.owned_group.messaging_ai_budget import OwnedGroupAIBudgetGate
from app.modules.owned_group.messaging_content_service import OwnedGroupMessageContentService
from app.modules.owned_group.messaging_contracts import OwnedGroupMessagingError
from app.modules.owned_group.messaging_target import OwnedGroupMessageTarget
from app.modules.owned_group.messaging_trigger_service import (
    IncomingOwnedGroupMessage,
    OwnedGroupMessageTriggerService,
)


class FakeAtomicRedis:
    """Small concurrent fake that only implements the Lua contracts under test."""

    def __init__(self) -> None:
        self.values: dict[str, int] = {}
        self.seen: set[str] = set()
        self.eval_calls: list[tuple[int, tuple[object, ...]]] = []
        self._lock = asyncio.Lock()

    async def eval(self, script: str, numkeys: int, *args: object) -> int:
        del script
        self.eval_calls.append((numkeys, args))
        async with self._lock:
            if numkeys == 1:
                key, budget, amount, _ttl = args
                name = str(key)
                current = self.values.get(name, 0)
                if current + int(amount) > int(budget):
                    return -1
                current += int(amount)
                self.values[name] = current
                return current
            if numkeys == 2:
                counter_key, seen_key, interval, _ttl = args
                seen_name = str(seen_key)
                if seen_name in self.seen:
                    return 0
                self.seen.add(seen_name)
                counter_name = str(counter_key)
                count = self.values.get(counter_name, 0) + 1
                self.values[counter_name] = count
                return int(count % int(interval) == 0)
            raise AssertionError(f"unexpected Lua contract: {numkeys}")


def _cache(client: object | None) -> SimpleNamespace:
    return SimpleNamespace(client=client)


def _target() -> OwnedGroupMessageTarget:
    return OwnedGroupMessageTarget(
        owned_group_asset_id=90,
        core_group_id=91,
        telegram_chat_id=-10090,
        managed_binding_id=92,
    )


def _ai_settings(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "enabled": True,
        "allowSemanticTriggeredReply": True,
        "dailyTokenBudget": 1000,
        "maxTokens": 40,
        "semanticEvaluateEveryMessages": 1,
        "semanticScanWindowMessages": 30,
        "semanticAllowedIntents": ["question"],
        "semanticBlockedIntents": [],
        "semanticDecisionPrompt": "判断是否需要回复",
        "systemPrompt": "安全回复",
    }
    value.update(overrides)
    return value


@pytest.mark.asyncio
async def test_generation_and_semantic_fail_closed_without_redis(monkeypatch) -> None:
    from app.modules.owned_group import messaging_content_service as content_module

    settings = _ai_settings()
    monkeypatch.setattr(
        content_module,
        "get_group_ai_interaction_settings",
        AsyncMock(return_value=settings),
    )
    content_llm = SimpleNamespace(
        model_for=MagicMock(return_value="fast-model"),
        generate=AsyncMock(return_value="不会生成"),
    )
    content = OwnedGroupMessageContentService(
        AsyncMock(),
        llm_client=content_llm,
        cache=_cache(None),
    )

    with pytest.raises(OwnedGroupMessagingError) as content_error:
        await content._generate_ai(
            target=_target(),
            policy=SimpleNamespace(allowed_topics=["产品答疑"]),
            category="community",
            trigger_type="reply",
            group_name="测试群",
            topic=None,
            instruction=None,
            source_text="怎么使用？",
            recent_context=(),
        )

    assert content_error.value.code == "AI_TOKEN_BUDGET_UNAVAILABLE"
    content_llm.generate.assert_not_awaited()

    semantic_llm = SimpleNamespace(
        model_for=MagicMock(return_value="fast-model"),
        generate=AsyncMock(return_value="{}"),
    )
    trigger = OwnedGroupMessageTriggerService(
        AsyncMock(),
        semantic_llm_client=semantic_llm,
        cache=_cache(None),
    )
    message = _incoming_message(1)
    with pytest.raises(OwnedGroupMessagingError) as semantic_error:
        await trigger._semantic_match(
            message,
            SimpleNamespace(id=4, allowed_topics=["产品答疑"]),
            settings=settings,
            minimum=0.8,
        )

    assert semantic_error.value.code == "AI_TOKEN_BUDGET_UNAVAILABLE"
    semantic_llm.generate.assert_not_awaited()
    assert (
        await trigger._semantic_evaluation_due(
            asset_id=90,
            source_message_id=1,
            settings=settings,
        )
        is False
    )


@pytest.mark.asyncio
async def test_semantic_missing_provider_does_not_consume_budget() -> None:
    budget_gate = SimpleNamespace(reserve=AsyncMock())
    service = OwnedGroupMessageTriggerService(
        AsyncMock(),
        ai_budget_gate=budget_gate,
    )
    service._build_llm_client = MagicMock(return_value=None)

    matched = await service._semantic_match(
        _incoming_message(1),
        SimpleNamespace(id=4, allowed_topics=["产品答疑"]),
        settings=_ai_settings(),
        minimum=0.8,
    )

    assert matched is False
    budget_gate.reserve.assert_not_awaited()


@pytest.mark.asyncio
async def test_generation_and_semantic_share_one_daily_budget(monkeypatch) -> None:
    from app.modules.owned_group import messaging_content_service as content_module

    redis = FakeAtomicRedis()
    cache = _cache(redis)
    settings = _ai_settings(dailyTokenBudget=100, maxTokens=60)
    monkeypatch.setattr(
        content_module,
        "get_group_ai_interaction_settings",
        AsyncMock(return_value=settings),
    )
    content_llm = SimpleNamespace(
        model_for=MagicMock(return_value="fast-model"),
        generate=AsyncMock(return_value="自然回复"),
    )
    content = OwnedGroupMessageContentService(
        AsyncMock(),
        llm_client=content_llm,
        cache=cache,
    )
    await content._generate_ai(
        target=_target(),
        policy=SimpleNamespace(allowed_topics=["产品答疑"]),
        category="community",
        trigger_type="reply",
        group_name="测试群",
        topic=None,
        instruction=None,
        source_text="怎么使用？",
        recent_context=(),
    )

    semantic_llm = SimpleNamespace(
        model_for=MagicMock(return_value="fast-model"),
        generate=AsyncMock(return_value="{}"),
    )
    trigger = OwnedGroupMessageTriggerService(
        AsyncMock(),
        semantic_llm_client=semantic_llm,
        cache=cache,
    )
    with pytest.raises(OwnedGroupMessagingError) as exc_info:
        await trigger._semantic_match(
            _incoming_message(2),
            SimpleNamespace(id=4, allowed_topics=["产品答疑"]),
            settings=settings,
            minimum=0.8,
        )

    assert exc_info.value.code == "AI_TOKEN_BUDGET_REACHED"
    semantic_llm.generate.assert_not_awaited()
    assert redis.values[OwnedGroupAIBudgetGate.daily_key()] == 60


@pytest.mark.asyncio
async def test_ai_budget_reservation_is_atomic_under_concurrency() -> None:
    redis = FakeAtomicRedis()
    gate = OwnedGroupAIBudgetGate(_cache(redis))
    settings = _ai_settings(dailyTokenBudget=100)

    results = await asyncio.gather(
        *(gate.reserve(settings, estimated_tokens=30) for _ in range(8)),
        return_exceptions=True,
    )

    accepted = sorted(value for value in results if isinstance(value, int))
    rejected = [value for value in results if isinstance(value, OwnedGroupMessagingError)]
    assert accepted == [30, 60, 90]
    assert len(rejected) == 5
    assert {error.code for error in rejected} == {"AI_TOKEN_BUDGET_REACHED"}
    assert redis.values[OwnedGroupAIBudgetGate.daily_key()] == 90
    assert len(redis.eval_calls) == 8


@pytest.mark.asyncio
async def test_semantic_evaluation_uses_global_every_messages_throttle(monkeypatch) -> None:
    from app.modules.owned_group import messaging_trigger_service as trigger_module

    redis = FakeAtomicRedis()
    settings = _ai_settings(semanticEvaluateEveryMessages=3)
    monkeypatch.setattr(
        trigger_module,
        "get_group_ai_interaction_settings",
        AsyncMock(return_value=settings),
    )
    llm = SimpleNamespace(
        model_for=MagicMock(return_value="fast-model"),
        generate=AsyncMock(
            return_value=json.dumps(
                {
                    "should_reply": True,
                    "target_message_id": 3,
                    "intent": "question",
                    "confidence": 0.91,
                    "reply": "可以这样处理",
                }
            )
        ),
    )
    service = OwnedGroupMessageTriggerService(
        AsyncMock(),
        semantic_llm_client=llm,
        cache=_cache(redis),
    )
    service._choose_policy = AsyncMock(return_value=None)
    policy = SimpleNamespace(
        id=4,
        owned_group_asset_id=90,
        account_id=7,
        mode="ai",
        promotion_config={"mode": "off"},
        allowed_topics=["产品答疑"],
        trigger_config={
            "reply": {
                "enabled": True,
                "strategy": "semantic",
                "semantic_min_confidence": 0.8,
                "content_category": "community",
            }
        },
    )

    for message_id in (1, 2, 3, 3, 4):
        await service._reply_execution(
            target=_target(),
            policies=[policy],
            message=_incoming_message(message_id),
        )

    llm.generate.assert_awaited_once()
    semantic_counter = next(
        value for key, value in redis.values.items() if key.endswith(":count")
    )
    assert semantic_counter == 4
    assert redis.values[OwnedGroupAIBudgetGate.daily_key()] == 40


def _incoming_message(message_id: int) -> IncomingOwnedGroupMessage:
    return IncomingOwnedGroupMessage(
        telegram_chat_id=-10090,
        source_message_id=message_id,
        sender_id=8008,
        sender_name="member",
        text="套餐应该怎么选择？",
        occurred_at=datetime.now(UTC),
    )
