"""Verification writes require a fresh, addressed, verified-admin challenge."""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest

from app.core.account.models import AccountStatus, TelegramAccount
from app.core.group.models import Group, GroupAccountMembership
from app.core.settings_models import SystemSetting
from app.modules.acquisition.automation import (
    AcquisitionAutomationService,
    JoinVerificationSettings,
)
from app.modules.acquisition.group_qualification import POLICY_VERSION
from app.modules.acquisition.models import GroupQualificationAudit
from app.modules.acquisition.qualification_service import priority_account_ids
from app.modules.acquisition.qualification_verification import (
    run_verifications,
    targeted_recent_prompt,
)


def message(**changes):
    return Obj(
        id=91,
        date=datetime.utcnow(),
        message="@current 请点击验证，证明不是机器人",
        out=False,
        fwd_from=None,
        entities=[],
        sender_id=500,
        buttons=[[Obj(text="点击验证", data=b"verify-current")]],
        **changes,
    )


async def setup(db, *, enabled=True, sender_admin=True, messages=None):
    now = datetime.utcnow()
    account = TelegramAccount(
        id=2,
        identifier="verify-test",
        session_name="verify-test",
        status=AccountStatus.ONLINE,
        is_active=True,
        risk_level="normal",
    )
    group = Group(id=40, group_id=1234567890, username="Verify_Group")
    member = GroupAccountMembership(
        id=60,
        account_id=2,
        group_id=40,
        telegram_group_id=group.group_id,
        joined_at=now - timedelta(minutes=2),
        status="joined",
        review_status="review_2h",
        ad_status="warming",
    )
    row = GroupQualificationAudit(
        batch_id="verify",
        membership_id=60,
        account_id=2,
        group_id=40,
        policy_version=POLICY_VERSION,
        content_scope="text_profile",
        state="completed",
        decision="observe",
        membership_joined_at=member.joined_at,
        checked_at=now,
        expires_at=now + timedelta(hours=24),
        evidence_json=json.dumps({"raw_peer_id": group.group_id, "group_type": "supergroup"}),
    )
    db.add_all(
        [
            account,
            group,
            member,
            row,
            SystemSetting(
                key="automation.group_qualification",
                value=json.dumps(
                    {
                        "enabled": True,
                        "execute_verification": enabled,
                        "account_ids": [2],
                    }
                ),
            ),
        ]
    )
    await db.commit()
    client = Obj(
        get_entity=AsyncMock(return_value=Obj(id=group.group_id, megagroup=True)),
        get_me=AsyncMock(return_value=Obj(id=200, username="current")),
        get_permissions=AsyncMock(return_value=Obj(is_admin=sender_admin, is_creator=False)),
        get_messages=AsyncMock(return_value=message()),
        send_message=AsyncMock(),
        send_file=AsyncMock(),
    )
    wrapper = Obj(client=client)
    pool = Obj(
        add_account_from_db=AsyncMock(),
        acquire_by_id=AsyncMock(return_value=wrapper),
        release=AsyncMock(),
    )
    service = AcquisitionAutomationService(db)
    service.account_pool = pool
    service._join_verification_settings = AsyncMock(return_value=JoinVerificationSettings())
    service._telegram_entity_matches_group_id = lambda *_: True
    service._read_join_audit_snapshot = AsyncMock(
        return_value=(
            messages if messages is not None else [message()],
            set(),
            False,
            "account_send_restricted",
            None,
        )
    )
    service.telegram_execution = Obj(
        click_verification_button=AsyncMock(return_value=Obj(message="verified")),
        send_verification_answer=AsyncMock(return_value=Obj(id=92)),
    )
    service._leave_group = AsyncMock()
    return service, client, member, row


@pytest.mark.asyncio
async def test_verified_current_challenge_clicks_once_and_requeues_readonly_audit(test_db):
    service, client, member, row = await setup(test_db)
    result = await run_verifications(service)
    assert result["attempted"] == 1
    service.telegram_execution.click_verification_button.assert_awaited_once()
    client.send_message.assert_not_called()
    client.send_file.assert_not_called()
    service._leave_group.assert_not_called()
    assert row.state == "queued" and member.review_status == "initial_pending"
    record = await test_db.get(SystemSetting, f"qualification.verification.{member.id}")
    assert json.loads(record.value)["actions"][0]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_disabled_action_queue_does_not_even_acquire_client(test_db):
    service, _, _, _ = await setup(test_db, enabled=False)
    assert (await run_verifications(service))["attempted"] == 0
    service.account_pool.acquire_by_id.assert_not_called()


@pytest.mark.asyncio
async def test_nonadmin_prompt_never_runs_solver(test_db):
    service, _, _, _ = await setup(test_db, sender_admin=False)
    assert (await run_verifications(service))["attempted"] == 0
    service.telegram_execution.click_verification_button.assert_not_called()


@pytest.mark.parametrize(
    "change",
    [
        {"message": "@someoneelse 请点击验证"},
        {"date": datetime.utcnow() - timedelta(hours=1)},
        {"fwd_from": Obj(from_id=42)},
        {"out": True},
        {"message": "@current 今日天气不错"},
    ],
)
def test_old_untargeted_or_unrelated_messages_never_authorize_verification(change):
    item = message()
    for key, value in change.items():
        setattr(item, key, value)
    assert not targeted_recent_prompt(
        item,
        Obj(id=200, username="current"),
        datetime.utcnow() - timedelta(minutes=2),
        datetime.utcnow(),
    )


@pytest.mark.asyncio
async def test_unknown_click_result_is_not_repeated_and_never_leaves(test_db):
    service, _, member, row = await setup(test_db)
    service.telegram_execution.click_verification_button = AsyncMock(side_effect=TimeoutError())
    assert (await run_verifications(service))["attempted"] == 1
    service._leave_group.assert_not_called()
    row.state = "completed"
    record = await test_db.get(SystemSetting, f"qualification.verification.{member.id}")
    ledger = json.loads(record.value)
    assert ledger["actions"][0]["status"] == "unknown"
    ledger["next_check_at"] = (datetime.utcnow() - timedelta(seconds=1)).isoformat()
    record.value = json.dumps(ledger)
    await test_db.commit()
    assert (await run_verifications(service))["attempted"] == 0
    assert service.telegram_execution.click_verification_button.await_count == 1


@pytest.mark.asyncio
async def test_no_prompt_does_not_run_unknown_fallback(test_db):
    service, _, _, _ = await setup(test_db, messages=[])
    service._handle_join_verification = AsyncMock()
    assert (await run_verifications(service))["attempted"] == 0
    service._handle_join_verification.assert_not_called()
    service._leave_group.assert_not_called()


@pytest.mark.asyncio
async def test_verification_account_scope_excludes_owner_without_stopping_reviews(test_db):
    service, _, _, _ = await setup(test_db)
    setting = await test_db.get(SystemSetting, "automation.group_qualification")
    config = json.loads(setting.value)
    config["account_ids"] = [2, 4]
    config["verification_account_ids"] = [4]
    setting.value = json.dumps(config)
    await test_db.commit()
    assert (await run_verifications(service))["attempted"] == 0
    service.account_pool.acquire_by_id.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_verification_scope_fails_closed(test_db):
    service, _, _, _ = await setup(test_db)
    setting = await test_db.get(SystemSetting, "automation.group_qualification")
    config = json.loads(setting.value)
    config["verification_account_ids"] = "2"
    setting.value = json.dumps(config)
    await test_db.commit()
    assert (await run_verifications(service))["attempted"] == 0
    service.account_pool.acquire_by_id.assert_not_called()


@pytest.mark.asyncio
async def test_verification_priority_is_current_version_scoped_and_bounded(test_db):
    _, _, _, row = await setup(test_db)
    setting = await test_db.get(SystemSetting, "automation.group_qualification")
    config = json.loads(setting.value)
    assert await priority_account_ids(test_db, config, datetime.utcnow()) == {2}

    row.policy_version = "pp-ai-qualification-v2"
    await test_db.commit()
    assert await priority_account_ids(test_db, config, datetime.utcnow()) == set()

    row.policy_version = POLICY_VERSION
    row.checked_at = datetime.utcnow() - timedelta(minutes=3)
    await test_db.commit()
    assert await priority_account_ids(test_db, config, datetime.utcnow()) == set()

    row.checked_at = datetime.utcnow()
    config["verification_account_ids"] = [4]
    await test_db.commit()
    assert await priority_account_ids(test_db, config, datetime.utcnow()) == set()


@pytest.mark.asyncio
async def test_restricted_account_never_performs_verification_write(test_db):
    service, _, _, _ = await setup(test_db)
    account = await test_db.get(TelegramAccount, 2)
    account.status = AccountStatus.RESTRICTED
    await test_db.commit()
    assert (await run_verifications(service))["attempted"] == 0
    service.account_pool.acquire_by_id.assert_not_called()
    assert account.status == AccountStatus.RESTRICTED


async def setup_second_hop(db, *, is_bot=True, replies=None, button_url=False):
    prompt = message()
    prompt.message = "@current 请验证后发言 https://t.me/VerifyGuardBot?start=join42"
    prompt.buttons = []
    if button_url:
        prompt.message = "@current 请点击下面按钮验证后发言"
        prompt.buttons = [[Obj(text="点击验证", url="https://t.me/VerifyGuardBot?start=join42")]]
    service, client, member, row = await setup(db, messages=[prompt])
    bot = Obj(id=900, bot=is_bot)
    group_entity = Obj(id=1234567890, megagroup=True)

    async def get_entity(value):
        return bot if value == "VerifyGuardBot" else group_entity

    reply_batches = list(replies if replies is not None else [
        [Obj(id=101, message="验证通过", out=False, buttons=[])]
    ])

    async def get_messages(entity, *, ids=None, limit=None):
        if ids is not None:
            return prompt
        if entity is bot:
            return reply_batches.pop(0) if reply_batches else []
        return []

    client.get_entity = AsyncMock(side_effect=get_entity)
    client.get_messages = AsyncMock(side_effect=get_messages)
    service._join_verification_settings = AsyncMock(
        return_value=JoinVerificationSettings(allow_second_hop_bots=True)
    )
    service.telegram_execution.send_verification_bot_text = AsyncMock(return_value=Obj(id=100))
    return service, client, member, row, bot


@pytest.mark.asyncio
async def test_second_hop_uses_budgeted_bot_start_and_rechecks_group(test_db):
    service, client, member, row, bot = await setup_second_hop(test_db)
    result = await run_verifications(service)
    assert result["attempted"] == 1
    assert result["details"][0]["status"] == "verified"
    service.telegram_execution.send_verification_bot_text.assert_awaited_once()
    args, kwargs = service.telegram_execution.send_verification_bot_text.await_args
    assert args[1] is bot and args[2] == "/start join42"
    assert kwargs["group_id"] == member.telegram_group_id
    assert row.state == "queued" and member.review_status == "initial_pending"
    client.send_message.assert_not_called()
    record = await test_db.get(SystemSetting, f"qualification.verification.{member.id}")
    actions = json.loads(record.value)["actions"]
    assert len(actions) == 1 and actions[0]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_second_hop_group_url_button_uses_budgeted_bot_start(test_db):
    service, client, member, row, bot = await setup_second_hop(test_db, button_url=True)
    result = await run_verifications(service)
    assert result["attempted"] == 1
    assert result["details"][0]["status"] == "verified"
    args, _ = service.telegram_execution.send_verification_bot_text.await_args
    assert args[1] is bot and args[2] == "/start join42"
    service.telegram_execution.click_verification_button.assert_not_called()
    assert row.state == "queued" and member.review_status == "initial_pending"
    client.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_second_hop_non_bot_defers_without_external_write(test_db):
    service, client, member, row, _ = await setup_second_hop(test_db, is_bot=False)
    result = await run_verifications(service)
    assert result["attempted"] == 0
    assert result["details"][0]["reason"] == "second_hop_target_not_bot"
    service.telegram_execution.send_verification_bot_text.assert_not_called()
    client.send_message.assert_not_called()
    record = await test_db.get(SystemSetting, f"qualification.verification.{member.id}")
    assert json.loads(record.value)["actions"][0]["status"] == "read_deferred"


@pytest.mark.asyncio
async def test_second_hop_unknown_start_is_not_resent(test_db):
    service, client, member, row, _ = await setup_second_hop(test_db)
    service.telegram_execution.send_verification_bot_text.side_effect = TimeoutError()
    first = await run_verifications(service)
    assert first["attempted"] == 1
    record = await test_db.get(SystemSetting, f"qualification.verification.{member.id}")
    assert json.loads(record.value)["actions"][0]["status"] == "unknown"
    row.state = "completed"
    await test_db.commit()
    second = await run_verifications(service)
    assert second["attempted"] == 0
    assert service.telegram_execution.send_verification_bot_text.await_count == 1
    client.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_second_hop_bot_button_uses_verification_callback(test_db):
    button = Obj(text="点击验证", data=b"bot-verify")
    replies = [
        [Obj(id=101, message="请点击验证", out=False, buttons=[[button]])],
        [Obj(id=103, message="验证通过", out=False, buttons=[])],
    ]
    service, client, member, _, bot = await setup_second_hop(test_db, replies=replies)
    result = await run_verifications(service)
    assert result["attempted"] == 1
    assert result["details"][0]["status"] == "verified"
    service.telegram_execution.click_verification_button.assert_awaited_once()
    args, kwargs = service.telegram_execution.click_verification_button.await_args
    assert args[1] is bot and args[2] == 101 and args[3] == b"bot-verify"
    assert kwargs["risk_target_type"] == "verification_bot"
    record = await test_db.get(SystemSetting, f"qualification.verification.{member.id}")
    assert [item["step"] for item in json.loads(record.value)["actions"]] == ["start", "button"]
    client.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_verification_bot_text_uses_durable_verification_budget():
    from telethon.tl.types import User

    from app.core.account.telegram_execution import TelegramExecutionService

    bot = User(id=900, bot=True)
    client = Obj(get_entity=AsyncMock(return_value=bot), send_message=AsyncMock())
    service = TelegramExecutionService()
    service._get_client = lambda _account: client
    service._verification_preflight = AsyncMock()
    service._bot_verification_preflight = AsyncMock()
    service._budgeted_write = AsyncMock(return_value=Obj(id=100))
    result = await service.send_verification_bot_text(
        Obj(account_id=2), bot, "/start join42",
        challenge_key="account-group-message-start", group_id=1234567890,
    )
    assert result.id == 100
    kwargs = service._budgeted_write.await_args.kwargs
    assert kwargs["category"] == "verification"
    assert kwargs["require_budget"] is True
    assert kwargs["risk_target_type"] == "verification_bot"
    assert kwargs["attempt_key"].startswith("verification_bot:")
    client.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_second_hop_toggle_blocks_bot_text_before_budget(test_db):
    from app.core.account.telegram_execution import (
        TelegramExecutionService,
        TelegramSendPreflightError,
    )

    test_db.add(SystemSetting(
        key="automation.auto_join_scheduler",
        value=json.dumps({"join_verification": {"allow_second_hop_bots": False}}),
    ))
    await test_db.commit()
    service = TelegramExecutionService()
    service.risk_guard = Obj(db=test_db)
    service._verification_preflight = AsyncMock()
    service._get_client = lambda _account: Obj()
    service._budgeted_write = AsyncMock()
    with pytest.raises(TelegramSendPreflightError, match="qualification_second_hop_paused"):
        await service.send_verification_bot_text(
            Obj(account_id=2), Obj(id=900, bot=True), "/start",
            challenge_key="chall", group_id=1234567890,
        )
    service._budgeted_write.assert_not_called()


@pytest.mark.asyncio
async def test_verification_bot_text_rejects_nonbot_before_budget():
    from app.core.account.telegram_execution import (
        TelegramExecutionService,
        TelegramSendPreflightError,
    )

    service = TelegramExecutionService()
    service._get_client = lambda _account: Obj()
    service._budgeted_write = AsyncMock()
    with pytest.raises(TelegramSendPreflightError):
        await service.send_verification_bot_text(
            Obj(account_id=2), Obj(id=900, bot=False), "/start",
            challenge_key="chall", group_id=1234567890,
        )
    service._budgeted_write.assert_not_called()
