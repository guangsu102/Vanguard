from datetime import datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from telethon.errors import FloodWaitError
from telethon.tl.types import InputPeerChannel

from app.core.account.models import TelegramAccount, AccountOperationConfig, AccountOutboundAttempt, AccountStatus
from app.core.account.telegram_execution import TelegramExecutionService, TelegramSendOutcomeUnknownError, TelegramSendPreflightError
from app.modules.acquisition import qualification_service, qualification_actions


async def setup(db, monkeypatch, failure=None):
    account = TelegramAccount(identifier="execution-boundary", session_name="execution-boundary",
        status=AccountStatus.ONLINE, is_active=True, risk_level="normal", registered_at=datetime.utcnow()-timedelta(days=365))
    db.add(account); await db.flush()
    db.add(AccountOperationConfig(account_id=account.id, enabled=True, dynamic_capacity_enabled=True,
        auto_ads_enabled=True, max_ads_per_day=30, max_messages_per_day=62, message_interval_seconds=600))
    await db.commit()
    gate = AsyncMock(return_value=None)
    monkeypatch.setattr(qualification_service, "send_gate", gate)
    monkeypatch.setattr(qualification_actions, "validate_live_send", AsyncMock())
    monkeypatch.setattr(qualification_service, "current_authorization", AsyncMock(return_value=(
        Obj(id=1, evidence_hash="h", policy_version="pp-ai-qualification-v2", content_scope="text_profile", evidence_json='{}'),
        Obj(group_id=-1001111222233), Obj(id=1))))
    risk = Obj(db=db, check_and_reserve=AsyncMock(return_value=Obj(allowed=True)), record_failure=AsyncMock(), record_success=AsyncMock())
    wrapper = Obj(account_id=account.id, client=Obj(
        get_input_entity=AsyncMock(return_value=InputPeerChannel(1111222233, 987)),
        send_message=AsyncMock(side_effect=failure, return_value=Obj(id=99))))
    return TelegramExecutionService(risk), wrapper, gate, risk


@pytest.mark.asyncio
async def test_resolution_failure_does_not_create_outbound_attempt(test_db, monkeypatch):
    execution, account, _, _ = await setup(test_db, monkeypatch)
    account.client.get_input_entity.side_effect = ValueError("missing entity")
    with pytest.raises(TelegramSendPreflightError, match="qualification_entity_unavailable"):
        await execution.send_ad(account, -1001111222233, "文字简介", reservation_token="unresolved")
    assert await test_db.scalar(select(AccountOutboundAttempt)) is None
    account.client.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_durable_unknown_blocks_same_event_and_target(test_db, monkeypatch):
    execution, account, _, _ = await setup(test_db, monkeypatch, TimeoutError())
    with pytest.raises(TelegramSendOutcomeUnknownError):
        await execution.send_ad(account, -1001111222233, "文字简介", reservation_token="original")
    row = await test_db.scalar(select(AccountOutboundAttempt))
    assert row.state == "unknown" and row.attempted_at
    with pytest.raises(TelegramSendOutcomeUnknownError):
        await execution.send_ad(account, -1001111222233, "文字简介", reservation_token="original")
    with pytest.raises(TelegramSendPreflightError):
        await execution.send_ad(account, -1001111222233, "另一文案", reservation_token="changed")
    assert account.client.send_message.await_count == 1


@pytest.mark.asyncio
async def test_switch_changes_after_reservation_no_rpc_and_quota_released(test_db, monkeypatch):
    execution, account, gate, _ = await setup(test_db, monkeypatch)
    gate.side_effect = [None, "ad_delivery_paused"]
    with pytest.raises(TelegramSendPreflightError, match="ad_delivery_paused"):
        await execution.send_ad(account, -1001111222233, "文字简介", reservation_token="paused")
    row = await test_db.scalar(select(AccountOutboundAttempt))
    assert row.state == "cancelled" and row.attempted_at is None
    account.client.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_flood_rejection_still_counted(test_db, monkeypatch):
    execution, account, _, _ = await setup(test_db, monkeypatch, FloodWaitError(None, capture=60))
    with pytest.raises(FloodWaitError):
        await execution.send_ad(account, -1001111222233, "文字简介", reservation_token="flood")
    row = await test_db.scalar(select(AccountOutboundAttempt))
    assert row.state == "failed" and row.attempted_at


@pytest.mark.asyncio
async def test_known_id_survives_post_rpc_audit_error(test_db, monkeypatch):
    execution, account, _, risk = await setup(test_db, monkeypatch)
    risk.record_success.side_effect = RuntimeError("audit db down")
    with pytest.raises(TelegramSendOutcomeUnknownError) as caught:
        await execution.send_ad(account, -1001111222233, "文字简介", reservation_token="known")
    assert caught.value.telegram_message_id == 99
    row = await test_db.scalar(select(AccountOutboundAttempt))
    assert row.state == "succeeded" and row.message_id == 99


@pytest.mark.asyncio
async def test_dynamic_legacy_paths_are_closed(test_db, monkeypatch):
    execution, account, _, _ = await setup(test_db, monkeypatch)
    with pytest.raises(TelegramSendPreflightError):
        await execution.send_group_message(account, -1001111222233, "probe", source="ad_probe")
    with pytest.raises(TelegramSendPreflightError):
        await execution.send_private_message(account, 12345, "广告")
    account.client.send_message.assert_not_awaited()
