"""Real image CAPTCHA generation, session identity, replay and timeout behaviour."""
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.core.settings_models import SystemSetting
from app.integrations.telegram.client import TelegramClient, TelegramConfig
from app.modules.guardian.models import (
    GroupVerificationConfig,
    VerificationSession,
    VerificationState,
    VerificationType,
)
from app.modules.guardian.verification import runtime
from app.modules.guardian.verification.verification_mgr import VerificationManager

pytestmark = pytest.mark.asyncio


@asynccontextmanager
async def risk(*args, **kwargs):
    yield


@pytest.fixture
async def setup_runtime(test_db, monkeypatch):
    test_db.add(SystemSetting(key=runtime.SETTING_KEY, value=json.dumps({
        "enabled": True, "bot_account_id": 32, "telegram_chat_id": -100123,
        "owned_group_asset_id": 7, "managed_binding_id": 8,
    })))
    test_db.add(GroupVerificationConfig(
        group_id=9, enable_verification=True, verification_type=VerificationType.CAPTCHA,
        timeout_minutes=5, max_attempts=3, whitelist_bypass=True,
        auto_kick_unverified=True, kick_after_minutes=10,
        welcome_message="欢迎 {username}！",
    ))
    await test_db.commit()
    target = SimpleNamespace(
        allowed=True, is_owned_group=True, owned_group_asset_id=7,
        managed_binding_id=8, core_group_id=9, telegram_chat_id=-100123,
    )
    monkeypatch.setattr(runtime, "owned_group_governance_gate_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(runtime, "resolve_governance_worker_target", AsyncMock(return_value=target))
    send = AsyncMock(return_value=777)
    monkeypatch.setattr(runtime, "TelegramExecutionService", lambda guard: SimpleNamespace(send_bot_message=send))
    client = SimpleNamespace(
        get_chat_member=AsyncMock(return_value={"status": "member"}),
        get_chat_permissions=AsyncMock(return_value={
            "can_send_messages": True, "can_send_photos": True, "can_invite_users": False,
        }),
        restrict_chat_member=AsyncMock(return_value=True),
        send_verification_photo=AsyncMock(return_value=SimpleNamespace(message_id=601)),
        delete_message=AsyncMock(return_value=True),
        ban_chat_member=AsyncMock(return_value=True),
        unban_chat_member=AsyncMock(return_value=True),
        _risk_operation=risk,
    )
    return client, send, target


async def join(db, client, user_id=1234, source=90):
    return await runtime.handle_join(
        db, client, bot_account_id=32, chat_id=-100123,
        member={"id": user_id, "first_name": "测试用户"}, source_message_id=source,
    )


async def record(db, user_id=1234):
    row = await db.get(SystemSetting, f"{runtime.PREFIX}32.-100123.{user_id}")
    return row, json.loads(row.value)


def message(text, *, user_id=1234, message_id=100, chat_id=-100123):
    return {"chat": {"id": chat_id, "type": "supergroup"}, "from": {"id": user_id},
            "text": text, "message_id": message_id}


async def answer(db, client, text, **kwargs):
    return await runtime.handle_answer(db, client, bot_account_id=32,
                                      message=message(text, **kwargs), update_kind="message")


async def test_real_png_challenge_and_duplicate_join(test_db, setup_runtime):
    client, _, _ = setup_runtime
    assert await join(test_db, client)
    assert await join(test_db, client)
    client.send_verification_photo.assert_awaited_once()
    args = client.send_verification_photo.call_args.args
    assert args[1].startswith(b"\x89PNG\r\n\x1a\n")
    _, payload = await record(test_db)
    session = await VerificationManager(test_db).get_session(payload["session_id"])
    assert session.captcha_code not in args[2]
    assert "/verify" in args[2]
    assert payload["challenge_state"] == "sent"
    rights = client.restrict_chat_member.call_args.args[2]
    assert rights["can_send_messages"] and not rights["can_send_photos"]


async def test_only_own_session_answer_then_restore_current_defaults(test_db, setup_runtime):
    client, send, _ = setup_runtime
    await join(test_db, client)
    _, payload = await record(test_db)
    session = await VerificationManager(test_db).get_session(payload["session_id"])
    assert not await answer(test_db, client, "/verify " + session.captcha_code, user_id=5555)
    assert not await answer(test_db, client, "/verify " + session.captcha_code, chat_id=-100456)
    client.get_chat_permissions.return_value = {"can_send_messages": True, "can_send_photos": False}
    assert await answer(test_db, client, "/verify " + session.captcha_code)
    _, payload = await record(test_db)
    assert payload["state"] == "passed"
    assert not client.restrict_chat_member.call_args.args[2]["can_send_photos"]
    assert not client.restrict_chat_member.call_args.args[2]["can_invite_users"]
    send.assert_awaited_once()
    assert await VerificationManager(test_db).is_user_verified(1234, 9)


async def test_chatter_deleted_without_spending_attempts_and_answer_replay(test_db, setup_runtime):
    client, _, _ = setup_runtime
    await join(test_db, client)
    assert await answer(test_db, client, "普通聊天")
    _, payload = await record(test_db)
    session = await VerificationManager(test_db).get_session(payload["session_id"])
    assert session.attempt_count == 0
    await answer(test_db, client, "/verify WRONG", message_id=101)
    await answer(test_db, client, "/verify WRONG", message_id=101)
    await test_db.refresh(session)
    assert session.attempt_count == 1
    assert client.delete_message.await_count == 3


async def test_three_failures_mute_then_kick_after_configured_delay(test_db, setup_runtime):
    client, _, _ = setup_runtime
    await join(test_db, client)
    for number in range(3):
        await answer(test_db, client, "/verify WRONG", message_id=200 + number)
    row, payload = await record(test_db)
    session = await VerificationManager(test_db).get_session(payload["session_id"])
    assert session.state == VerificationState.FAILED
    assert not any(client.restrict_chat_member.call_args.args[2].values())
    await runtime.maintain(test_db, client, bot_account_id=32)
    client.ban_chat_member.assert_not_awaited()
    payload["joined_at"] = (datetime.utcnow() - timedelta(minutes=11)).isoformat()
    row.value = json.dumps(payload)
    await test_db.commit()
    await runtime.maintain(test_db, client, bot_account_id=32)
    client.ban_chat_member.assert_awaited_once_with(-100123, 1234)
    client.unban_chat_member.assert_awaited_once_with(-100123, 1234, only_if_banned=True)
    assert (await record(test_db))[1]["state"] == "kicked"


async def test_timeout_cycle_without_updates_mutes_then_kicks(test_db, setup_runtime):
    client, _, _ = setup_runtime
    await join(test_db, client)
    row, payload = await record(test_db)
    session = await VerificationManager(test_db).get_session(payload["session_id"])
    session.expires_at = datetime.utcnow() - timedelta(seconds=1)
    payload["joined_at"] = (datetime.utcnow() - timedelta(minutes=6)).isoformat()
    row.value = json.dumps(payload)
    await test_db.commit()
    await runtime.maintain(test_db, client, bot_account_id=32)
    assert session.state == VerificationState.EXPIRED
    client.ban_chat_member.assert_not_awaited()
    assert not any(client.restrict_chat_member.call_args.args[2].values())


async def test_kick_unknown_reconciles_member_before_repeat(test_db, setup_runtime):
    client, _, _ = setup_runtime
    await join(test_db, client)
    row, payload = await record(test_db)
    session = await VerificationManager(test_db).get_session(payload["session_id"])
    session.state = VerificationState.EXPIRED
    payload.update(state="kick_pending", joined_at=(datetime.utcnow() - timedelta(minutes=11)).isoformat())
    row.value = json.dumps(payload)
    await test_db.commit()
    client.get_chat_member.return_value = {"status": "kicked"}
    await runtime.maintain(test_db, client, bot_account_id=32)
    client.ban_chat_member.assert_not_awaited()
    client.unban_chat_member.assert_awaited_once()


async def test_empty_captcha_and_old_pass_cannot_bypass(test_db):
    manager = VerificationManager(test_db)
    first = VerificationSession(
        session_id="old", user_id=1, chat_id=9, verify_type=VerificationType.CAPTCHA,
        state=VerificationState.PASSED, expires_at=datetime.utcnow()+timedelta(minutes=5),
    )
    second = VerificationSession(
        session_id="new", user_id=1, chat_id=9, verify_type=VerificationType.CAPTCHA,
        state=VerificationState.PENDING, expires_at=datetime.utcnow()+timedelta(minutes=5),
    )
    test_db.add_all([first, second])
    await test_db.commit()
    assert not await manager.is_user_verified(1, 9)
    assert not (await manager.verify_answer("new", "")).success


async def test_photo_transport_is_multipart_and_no_retry():
    captured = []
    async def handler(request):
        captured.append(request)
        raise httpx.ReadTimeout("unknown", request=request)
    client = TelegramClient(TelegramConfig(bot_token="test-only"))
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(Exception, match="outcome unknown"):
            await client.send_verification_photo(-100123, b"\x89PNG\r\n\x1a\ncontent", "识别图片")
        assert len(captured) == 1
        assert captured[0].headers["content-type"].startswith("multipart/form-data")
    finally:
        await client.close()
