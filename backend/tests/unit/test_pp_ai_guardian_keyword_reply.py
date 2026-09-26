"""PP-AI Guardian scope, verification and durable send fencing."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.settings_models import SystemSetting
from app.modules.guardian import keyword_reply as replies

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def configured(test_db, monkeypatch):
    config = {
        "enabled": True, "bot_account_id": 32, "telegram_chat_id": -100123,
        "owned_group_asset_id": 7, "managed_binding_id": 8,
        "replies": {"群规": "请阅读置顶群规。", "help": "可发送：群规、教程、产品。"},
    }
    test_db.add(SystemSetting(key=replies.SETTING_KEY, value=json.dumps(config)))
    await test_db.commit()
    target = SimpleNamespace(
        allowed=True, is_owned_group=True, owned_group_asset_id=7,
        managed_binding_id=8, core_group_id=9,
    )
    monkeypatch.setattr(replies, "owned_group_governance_gate_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(replies, "resolve_governance_worker_target", AsyncMock(return_value=target))
    return target


def message(**overrides):
    value = {
        "message_id": 42, "chat": {"id": -100123, "type": "supergroup"},
        "from": {"id": 1234, "is_bot": False}, "text": "群规",
    }
    value.update(overrides)
    return value


async def call(db, execution, value=None, **kwargs):
    manager = SimpleNamespace(
        get_verification_config=AsyncMock(return_value=SimpleNamespace(enable_verification=True)),
        is_user_verified=AsyncMock(return_value=True),
    )
    return await replies.handle_pp_ai_keyword_reply(
        db, object(), bot_account_id=kwargs.pop("bot_account_id", 32),
        message=value or message(), update_kind=kwargs.pop("update_kind", "message"),
        verification_manager=kwargs.pop("verification_manager", manager),
        execution=execution, **kwargs,
    )


async def test_exact_keywords_and_commands():
    config = {"replies": {"help": "帮助", "群规": "规则"}}
    assert replies.select_reply(config, "/help@pp_ai_chat_guard_bot") == "帮助"
    assert replies.select_reply(config, "群规") == "规则"
    assert replies.select_reply(config, "有人讨论群规") is None
    assert replies.select_reply(config, "") is None


async def test_verified_target_sends_once_across_replay(test_db, configured):
    execution = SimpleNamespace(send_bot_message=AsyncMock(return_value=101))
    assert (await call(test_db, execution))["message_id"] == 101
    assert (await call(test_db, execution))["state"] == "duplicate"
    execution.send_bot_message.assert_awaited_once()
    assert execution.send_bot_message.call_args.kwargs["parse_mode"] == ""


@pytest.mark.parametrize("override", [
    {"chat": {"id": 1234, "type": "private"}},
    {"chat": {"id": -100456, "type": "supergroup"}},
    {"from": {"id": 1234, "is_bot": True}},
    {"sender_chat": {"id": -100123}},
    {"forward_origin": {"type": "user"}},
    {"text": "不是群规关键词"},
])
async def test_unrelated_updates_never_send(test_db, configured, override):
    execution = SimpleNamespace(send_bot_message=AsyncMock())
    await call(test_db, execution, message(**override))
    execution.send_bot_message.assert_not_awaited()


async def test_other_bot_or_edited_message_never_send(test_db, configured):
    execution = SimpleNamespace(send_bot_message=AsyncMock())
    await call(test_db, execution, bot_account_id=31)
    await call(test_db, execution, update_kind="edited_message")
    execution.send_bot_message.assert_not_awaited()


async def test_degraded_or_different_binding_never_send(test_db, configured):
    execution = SimpleNamespace(send_bot_message=AsyncMock())
    configured.allowed = False
    assert (await call(test_db, execution))["state"] == "target_not_managed"
    configured.allowed = True
    configured.managed_binding_id = 99
    assert (await call(test_db, execution))["state"] == "target_not_managed"
    execution.send_bot_message.assert_not_awaited()


async def test_unverified_member_never_send(test_db, configured):
    execution = SimpleNamespace(send_bot_message=AsyncMock())
    manager = SimpleNamespace(
        get_verification_config=AsyncMock(return_value=SimpleNamespace(enable_verification=True)),
        is_user_verified=AsyncMock(return_value=False),
    )
    assert (await call(test_db, execution, verification_manager=manager))["state"] == "unverified"
    execution.send_bot_message.assert_not_awaited()


@pytest.mark.parametrize("outcome", [None, TimeoutError("do not expose secret")])
async def test_unknown_outcome_remains_fenced(test_db, configured, outcome):
    send = AsyncMock(side_effect=outcome) if isinstance(outcome, Exception) else AsyncMock(return_value=outcome)
    execution = SimpleNamespace(send_bot_message=send)
    result = await call(test_db, execution)
    assert result["state"] == "unknown"
    assert "secret" not in json.dumps(result)
    assert (await call(test_db, execution))["state"] == "duplicate"
    send.assert_awaited_once()


async def test_feature_disabled_by_default(test_db):
    execution = SimpleNamespace(send_bot_message=AsyncMock())
    assert (await call(test_db, execution))["state"] == "disabled"
    execution.send_bot_message.assert_not_awaited()
