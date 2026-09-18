from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.modules.guardian.main import GuardianBot
from app.modules.guardian.models import RuleType, ViolationAction, ViolationLevel
from app.modules.guardian.moderation.action_executor import ActionExecutor
from app.modules.guardian.moderation.rule_engine import EvaluationResult, MatchedRule
from app.modules.guardian.punishment.punishment_mgr import PunishmentResult


@pytest.mark.asyncio
async def test_violation_history_uses_internal_user_id_but_telegram_actions_do_not():
    telegram_user_id = 987654321
    internal_user_id = 42
    punishment_manager = SimpleNamespace(
        calculate_punishment=AsyncMock(
            return_value=PunishmentResult(
                action=ViolationAction.WARN,
                duration=None,
                reason="First warning",
                should_escalate=False,
            )
        ),
        record_violation=AsyncMock(),
        get_warning_count=AsyncMock(return_value=1),
    )
    bot = GuardianBot.__new__(GuardianBot)
    bot._context = SimpleNamespace(
        user_tracker=SimpleNamespace(
            get_or_create_user=AsyncMock(return_value=SimpleNamespace(id=internal_user_id))
        ),
        punishment_manager=punishment_manager,
        action_executor=SimpleNamespace(execute=AsyncMock()),
        warn_system=SimpleNamespace(send_warning=AsyncMock()),
    )
    bot.logger = SimpleNamespace(info=lambda *_args, **_kwargs: None)
    evaluation = EvaluationResult(
        is_violation=True,
        matched_rules=[
            MatchedRule(
                rule_id=7,
                rule_type=RuleType.KEYWORD,
                pattern="blocked",
                matched_content="blocked",
                level=ViolationLevel.LOW,
                action=ViolationAction.WARN,
            )
        ],
        recommended_action=ViolationAction.WARN,
        severity=ViolationLevel.LOW,
    )

    await bot._handle_violation(
        evaluation=evaluation,
        message_id=11,
        chat_id=-100123,
        user_id=telegram_user_id,
        username="member",
        text="blocked content",
        core_group_id=9,
    )

    bot._context.user_tracker.get_or_create_user.assert_awaited_once_with(
        telegram_id=telegram_user_id,
        username="member",
    )
    punishment_manager.calculate_punishment.assert_awaited_once_with(
        user_id=internal_user_id,
        group_id=9,
        level=ViolationLevel.LOW,
        is_repeat=False,
    )
    assert punishment_manager.record_violation.await_args.kwargs["user_id"] == internal_user_id
    punishment_manager.get_warning_count.assert_awaited_once_with(internal_user_id, 9)
    assert bot._context.action_executor.execute.await_args.kwargs["user_id"] == telegram_user_id
    assert bot._context.warn_system.send_warning.await_args.kwargs["user_id"] == telegram_user_id


@pytest.mark.asyncio
async def test_warn_with_message_deletes_exactly_once():
    telegram_client = object()
    telegram_execution = SimpleNamespace(delete_message=AsyncMock())
    executor = ActionExecutor(telegram_client, telegram_execution=telegram_execution)

    result = await executor.execute(
        action=ViolationAction.WARN,
        chat_id=-100456,
        user_id=123,
        message_id=22,
    )

    assert result.success is True
    assert result.action == ViolationAction.WARN
    telegram_execution.delete_message.assert_awaited_once_with(
        telegram_client,
        -100456,
        22,
        source="guardian_moderation",
    )


@pytest.mark.asyncio
async def test_mute_deletes_message_then_mutes_telegram_user():
    telegram_client = object()
    telegram_execution = SimpleNamespace(
        delete_message=AsyncMock(),
        mute_user=AsyncMock(),
    )
    executor = ActionExecutor(telegram_client, telegram_execution=telegram_execution)

    result = await executor.execute(
        action=ViolationAction.MUTE,
        chat_id=-100789,
        user_id=456,
        message_id=33,
        duration=600,
    )

    assert result.success is True
    assert result.action == ViolationAction.MUTE
    telegram_execution.delete_message.assert_awaited_once_with(
        telegram_client,
        -100789,
        33,
        source="guardian_moderation",
    )
    telegram_execution.mute_user.assert_awaited_once_with(
        telegram_client,
        -100789,
        456,
        600,
        source="guardian_moderation",
    )
