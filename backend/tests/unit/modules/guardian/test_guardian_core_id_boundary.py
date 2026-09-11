from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.modules.guardian.main import GuardianBot


def _guardian_bot() -> GuardianBot:
    bot = GuardianBot.__new__(GuardianBot)
    bot._context = SimpleNamespace(
        verification_manager=SimpleNamespace(
            is_user_verified=AsyncMock(),
            get_verification_config=AsyncMock(),
            handle_new_member=AsyncMock(),
        ),
        spam_detector=SimpleNamespace(check_all=AsyncMock()),
        rule_engine=SimpleNamespace(evaluate_message=AsyncMock()),
        campaign_runner=SimpleNamespace(trigger_for_event=AsyncMock()),
    )
    bot.logger = SimpleNamespace(
        warning=lambda *_args, **_kwargs: None,
        error=lambda *_args, **_kwargs: None,
    )
    return bot


@pytest.mark.asyncio
async def test_group_message_without_core_id_is_skipped_before_policy_lookup():
    bot = _guardian_bot()

    processed = await bot.handle_message(
        message_id=1,
        chat_id=-1007201,
        user_id=7201,
        username="member",
        text="hello",
    )

    assert processed is False
    bot._context.verification_manager.is_user_verified.assert_not_awaited()
    bot._context.verification_manager.get_verification_config.assert_not_awaited()
    bot._context.spam_detector.check_all.assert_not_awaited()
    bot._context.rule_engine.evaluate_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_member_without_core_id_is_skipped_before_governance_handlers():
    bot = _guardian_bot()

    response = await bot.handle_new_member(
        chat_id=-1007202,
        user_id=7202,
        username="new-member",
    )

    assert response is None
    bot._context.verification_manager.handle_new_member.assert_not_awaited()
    bot._context.campaign_runner.trigger_for_event.assert_not_awaited()
