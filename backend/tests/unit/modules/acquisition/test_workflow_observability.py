import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

import app.modules.acquisition.automation as acquisition_automation
from app.core.account.models import (
    AccountOperationConfig,
    AccountStatus,
    AccountType,
    TelegramAccount,
)
from app.core.group.models import Group, GroupAccountMembership, GroupLevel
from app.modules.acquisition.automation import AcquisitionAutomationService
from app.modules.acquisition.auto_reply.semantic_reply import SemanticGroupReplyEngine
from app.modules.acquisition.keyword_trigger.actions import ActionExecutor, TriggerActionType
from app.modules.acquisition.keyword_trigger.handler import TriggerHandler
from app.modules.acquisition.models import (
    AcquisitionMessage,
    AcquisitionTracking,
    MessageType,
    TriggerAction,
)


@pytest.mark.asyncio
async def test_unmatched_messages_do_not_consume_keyword_rate_limit():
    handler = TriggerHandler.__new__(TriggerHandler)
    handler._handler_lock = asyncio.Lock()
    handler.keyword_matcher = SimpleNamespace(match=AsyncMock(return_value=[]))
    handler._check_rate_limit = AsyncMock(return_value=False)
    handler.logger = MagicMock()

    result = await handler.handle_message("ordinary message", 1, 2, 3)

    assert result == []
    handler._check_rate_limit.assert_not_awaited()


@pytest.mark.asyncio
async def test_matched_messages_still_enforce_keyword_rate_limit():
    handler = TriggerHandler.__new__(TriggerHandler)
    handler._handler_lock = asyncio.Lock()
    handler.keyword_matcher = SimpleNamespace(match=AsyncMock(return_value=[MagicMock()]))
    handler._check_rate_limit = AsyncMock(return_value=False)
    handler.logger = MagicMock()

    result = await handler.handle_message("matched message", 1, 2, 3)

    assert result == []
    handler._check_rate_limit.assert_awaited_once_with(1, 2)


@pytest.mark.asyncio
async def test_semantic_reply_is_persisted_for_workflow_audit(test_db):
    account = TelegramAccount(
        phone="+15550004001",
        identifier="+15550004001",
        session_name="semantic_audit",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    test_db.add(account)
    await test_db.commit()
    await test_db.refresh(account)

    engine = SemanticGroupReplyEngine.__new__(SemanticGroupReplyEngine)
    engine.db = test_db
    engine.logger = MagicMock()
    await engine._record_sent_reply(
        account_id=account.id,
        group_id=-1001234567890,
        reply="这是一次可追踪的语义回复",
        sent_id=901,
    )

    row = (await test_db.execute(select(AcquisitionMessage))).scalar_one()
    assert row.account_id == account.id
    assert row.group_id == -1001234567890
    assert row.message_type == MessageType.QA
    assert row.message_id == 901


@pytest.mark.asyncio
async def test_capacity_cleanup_leaves_only_old_zero_conversion_group(test_db, monkeypatch):
    now = datetime.utcnow()
    account = TelegramAccount(
        phone="+15550004002",
        identifier="+15550004002",
        session_name="cleanup_audit",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    config = AccountOperationConfig(account=account, max_groups_total=1, enabled=True)
    zero_conversion_group = Group(group_id=940001, title="Zero Conversion", level=GroupLevel.C, status="active")
    converted_group = Group(group_id=940002, title="Converted", level=GroupLevel.C, status="active")
    test_db.add_all([account, config, zero_conversion_group, converted_group])
    await test_db.flush()
    zero_membership = GroupAccountMembership(
        group_id=zero_conversion_group.id,
        telegram_group_id=zero_conversion_group.group_id,
        account_id=account.id,
        status="joined",
        joined_at=now - timedelta(days=31),
    )
    converted_membership = GroupAccountMembership(
        group_id=converted_group.id,
        telegram_group_id=converted_group.group_id,
        account_id=account.id,
        status="joined",
        joined_at=now - timedelta(days=31),
    )
    conversion = AcquisitionTracking(
        tracking_code="cleanup-protected-conversion",
        group_id=converted_group.group_id,
        converted=True,
        converted_at=now - timedelta(days=1),
        external_user_id="xboard-cleanup-user",
    )
    test_db.add_all([zero_membership, converted_membership, conversion])
    await test_db.commit()

    monkeypatch.setattr(
        acquisition_automation,
        "get_auto_join_scheduler_settings",
        AsyncMock(
            return_value={
                "group_capacity_cleanup": {
                    "enabled": True,
                    "no_conversion_days": 30,
                    "min_join_age_days": 30,
                    "max_cleanup_per_run": 15,
                }
            }
        ),
    )
    service = AcquisitionAutomationService(test_db)
    service._leave_group = AsyncMock(return_value=None)
    service.group_manager.update_group = AsyncMock()

    result = await service._cleanup_account_group_capacity(config, current_group_count=2)

    await test_db.refresh(zero_membership)
    await test_db.refresh(converted_membership)
    assert result["left"] == 1
    assert zero_membership.status == "left"
    assert "capacity_cleanup_no_recent_conversion" in zero_membership.note
    assert converted_membership.status == "joined"


@pytest.mark.asyncio
async def test_group_reply_leaves_repeated_write_forbidden_group_without_sending():
    account = SimpleNamespace(account_id=41)
    risk_guard = SimpleNamespace(
        should_leave_group_after_write_forbidden=AsyncMock(return_value=True),
        mark_group_write_forbidden_group_left=AsyncMock(return_value=True),
    )
    executor = ActionExecutor(SimpleNamespace(), risk_guard=risk_guard)
    executor._group_ai_reply_allowed = AsyncMock(return_value=True)
    executor.telegram_execution = SimpleNamespace(
        leave_group_by_id=AsyncMock(),
        send_group_message=AsyncMock(return_value=99),
    )

    result = await executor.send_group_reply(account, -100123, "hello", reply_to=8)

    assert result is None
    executor.telegram_execution.leave_group_by_id.assert_awaited_once_with(
        account,
        -100123,
        source="keyword_trigger_write_forbidden",
    )
    executor.telegram_execution.send_group_message.assert_not_awaited()
    risk_guard.mark_group_write_forbidden_group_left.assert_awaited_once_with(
        account, -100123
    )


@pytest.mark.asyncio
async def test_unsent_keyword_reply_is_not_recorded_as_success(monkeypatch):
    account = SimpleNamespace(account_id=42)
    pool = SimpleNamespace(
        acquire=AsyncMock(return_value=account),
        release=AsyncMock(),
    )
    handler = TriggerHandler.__new__(TriggerHandler)
    handler.db = MagicMock()
    handler.account_pool = pool
    handler.action_executor = SimpleNamespace(send_group_reply=AsyncMock(return_value=None))
    handler._generate_reply = AsyncMock(return_value="generated reply")
    handler._record_trigger = AsyncMock()
    handler.logger = MagicMock()
    trigger = SimpleNamespace(id=1, action=TriggerAction.REPLY_TEMPLATE)
    match = SimpleNamespace(keyword_text="promo")
    monkeypatch.setattr(
        "app.modules.acquisition.keyword_trigger.handler.get_group_ai_interaction_settings",
        AsyncMock(return_value={"allowKeywordTriggeredReply": True}),
    )

    result = await handler._execute_reply(
        trigger,
        match,
        "promo",
        user_id=10,
        group_id=-100123,
        message_id=11,
        context=None,
    )

    assert result.success is False
    assert result.action_taken == TriggerActionType.REPLY
    assert result.error == "Group reply was not sent"
    handler._record_trigger.assert_not_awaited()
    pool.release.assert_awaited_once_with(account)
