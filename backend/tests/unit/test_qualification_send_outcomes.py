"""Reservation ownership and ambiguous Telegram writes must never cause blind retries."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select
from telethon.errors import ChatWriteForbiddenError, FloodWaitError

from app.core.account.models import AccountStatus, TelegramAccount
from app.core.account.telegram_execution import (
    TelegramExecutionService,
    TelegramSendOutcomeUnknownError,
    TelegramSendPreflightError,
)
from app.core.group.models import Group, GroupAccountMembership
from app.core.settings_models import SystemSetting
from app.modules.acquisition import automation as automation_module
from app.modules.acquisition import qualification_actions, qualification_service
from app.modules.acquisition.automation import AcquisitionAutomationService
from app.modules.acquisition.group_qualification import POLICY_VERSION
from app.modules.acquisition.models import (
    AdCampaign,
    AdCreative,
    AdDeliveryLog,
    DeliveryStatus,
    GroupQualificationAudit,
)


@pytest.fixture(autouse=True)
def approved_rpc_boundary(request, monkeypatch):
    from app.core.account import rpc_governor
    monkeypatch.setattr(rpc_governor, "read_budget_state", AsyncMock(return_value={
        "usage": {}, "blocked_windows": [], "retry_after_seconds": 0, "emergency_cooldown_seconds": 0,
    }))
    # Only transport unit tests use a qualified stub. The real dispatcher test
    # below keeps the actual persisted send gate and profile/version checks.
    if request.node.name.startswith(("test_real_", "test_leave_")):
        return
    monkeypatch.setattr(qualification_service, "send_gate", AsyncMock(return_value=None))
    monkeypatch.setattr(qualification_actions, "validate_live_send", AsyncMock())
    monkeypatch.setattr(qualification_service, "current_authorization", AsyncMock(return_value=(
        Obj(id=1, evidence_hash="h", content_scope="text_profile", policy_version=POLICY_VERSION),
        Obj(group_id=42), Obj(id=1))))


def execution_fixture(*, failure=None, result=None):
    client = Obj(
        send_message=AsyncMock(side_effect=failure, return_value=result or Obj(id=123)),
        send_file=AsyncMock(side_effect=failure, return_value=result or Obj(id=124)),
    )
    account = Obj(account_id=2, client=client, record_message=Mock())
    risk = Obj(
        db=Obj(get=AsyncMock(return_value=None), scalar=AsyncMock(return_value=None)),
        check_and_reserve=AsyncMock(return_value=Obj(allowed=True)),
        record_success=AsyncMock(),
        record_failure=AsyncMock(),
    )
    return TelegramExecutionService(risk), account, risk


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError(), ConnectionError("connection lost")])
@pytest.mark.parametrize("media", [None, "https://asset.example/banner.png"])
async def test_transport_loss_after_rpc_becomes_unknown(failure, media):
    execution, account, risk = execution_fixture(failure=failure)
    attempted = []
    with pytest.raises(TelegramSendOutcomeUnknownError, match="send_outcome_unknown:"):
        await execution.send_ad(
            account,
            42,
            "hello",
            media_url=media,
            on_send_attempted=lambda: attempted.append(True),
        )
    assert attempted == [True]
    assert account.client.send_message.await_count + account.client.send_file.await_count == 1
    risk.record_success.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("message_id", [None, 0, "invalid"])
async def test_missing_or_invalid_message_id_is_unknown(message_id):
    execution, account, _ = execution_fixture(result=Obj(id=message_id))
    with pytest.raises(TelegramSendOutcomeUnknownError, match="message_id_missing"):
        await execution.send_ad(account, 42, "hello")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [ChatWriteForbiddenError(None), FloodWaitError(None, capture=15)]
)
async def test_definite_telegram_rejections_are_not_unknown(failure):
    execution, account, _ = execution_fixture(failure=failure)
    with pytest.raises(type(failure)) as caught:
        await execution.send_ad(account, 42, "hello")
    assert caught.value is failure
    assert not isinstance(caught.value, TelegramSendOutcomeUnknownError)


@pytest.mark.asyncio
async def test_risk_audit_failure_cannot_erase_unknown_rpc_outcome():
    execution, account, risk = execution_fixture(failure=TimeoutError())
    risk.record_failure.side_effect = RuntimeError("database unavailable")
    with pytest.raises(TelegramSendOutcomeUnknownError, match="send_outcome_unknown:TimeoutError"):
        await execution.send_ad(account, 42, "hello")


@pytest.mark.asyncio
async def test_post_send_audit_failure_preserves_message_id_for_reconciliation():
    execution, account, risk = execution_fixture()
    risk.record_success.side_effect = RuntimeError("database unavailable")
    with pytest.raises(TelegramSendOutcomeUnknownError) as caught:
        await execution.send_ad(account, 42, "hello")
    assert caught.value.telegram_message_id == 123
    account.client.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_exact_reservation_token_reaches_send_gate(monkeypatch):
    execution, account, _ = execution_fixture()
    gate = AsyncMock(return_value=None)
    monkeypatch.setattr(qualification_service, "policy", AsyncMock(return_value={"enabled": True}))
    monkeypatch.setattr(qualification_service, "send_gate", gate)
    monkeypatch.setattr(qualification_actions, "validate_live_send", AsyncMock())
    assert await execution.send_ad(account, 42, "hello", reservation_token="exact-token") == 123
    assert gate.await_args.kwargs["reservation_token"] == "exact-token"


@pytest.mark.asyncio
async def test_preflight_read_timeout_does_not_claim_a_send_occurred(monkeypatch):
    execution, account, _ = execution_fixture()
    monkeypatch.setattr(qualification_service, "policy", AsyncMock(return_value={"enabled": True}))
    monkeypatch.setattr(qualification_service, "send_gate", AsyncMock(return_value=None))
    monkeypatch.setattr(
        qualification_actions, "validate_live_send", AsyncMock(side_effect=TimeoutError())
    )
    with pytest.raises(TimeoutError):
        await execution.send_ad(account, 42, "hello", reservation_token="exact-token")
    account.client.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_client_is_definite_preflight_failure():
    execution, account, _ = execution_fixture()
    account.client = None
    with pytest.raises(TelegramSendPreflightError):
        await execution.send_ad(account, 42, "hello")


@pytest.mark.asyncio
async def test_both_ad_wrappers_forward_their_exact_reservation():
    execution, account, _ = execution_fixture()
    pool = Obj(acquire_by_id=AsyncMock(return_value=account), release=AsyncMock())
    service = AcquisitionAutomationService(AsyncMock(), account_pool=pool)
    service.telegram_execution = Obj(send_ad=AsyncMock(return_value=123))
    service._ad_send_target = AsyncMock(return_value=42)
    creative = Obj(content="hello", link_url=None, creative_type="text", media_url=None)
    await service._send_ad(2, 42, creative, reservation_token="campaign-token")
    await service._send_ad_text(2, 42, "hello", source="trial", reservation_token="trial-token")
    assert [
        call.kwargs["reservation_token"]
        for call in service.telegram_execution.send_ad.await_args_list
    ] == ["campaign-token", "trial-token"]


async def seed(db):
    now = datetime.utcnow()
    account = TelegramAccount(
        id=2,
        identifier="outcome-account",
        session_name="outcome-account",
        status=AccountStatus.ONLINE,
        is_active=True,
        risk_level="normal",
    )
    group = Group(id=40, group_id=1234567890, title="outcome group")
    member = GroupAccountMembership(
        id=60,
        group_id=40,
        telegram_group_id=group.group_id,
        account_id=2,
        status="joined",
        joined_at=now - timedelta(days=5),
        review_status="approved",
        ad_status="active",
    )
    review = GroupQualificationAudit(
        batch_id="outcome-baseline",
        membership_id=60,
        account_id=2,
        group_id=40,
        policy_version=POLICY_VERSION,
        content_scope="text_profile",
        state="completed",
        decision="allowed",
        membership_joined_at=member.joined_at,
        checked_at=now,
        expires_at=now + timedelta(hours=24),
        evidence_json=json.dumps(
            {
                "quality_status": "qualified",
                "profile_fingerprint": __import__('app.modules.acquisition.qualification_ai', fromlist=['profile_fingerprint']).profile_fingerprint(account),
                "evidence": [],
                "decision": "allowed",
                "account_id": 2,
                "group_id": 40,
                "telegram_group_id": group.group_id,
                "group_type": "supergroup",
                "policy_version": POLICY_VERSION,
                "content_scope": "text_profile",
            }
        ),
    )
    campaign = AdCampaign(
        name="outcome campaign",
        delivery_policy="growth",
        enabled=True,
        status="active",
        max_sends_per_account_per_day=1,
        max_sends_per_group_per_day=1,
    )
    creative = AdCreative(name="outcome creative", content="hello", enabled=True)
    db.add_all(
        [
            account,
            group,
            member,
            review,
            campaign,
            creative,
            SystemSetting(key="automation.ad_delivery_execution", value='{"enabled": true}'),
            SystemSetting(
                key=qualification_service.SETTING_KEY,
                value=json.dumps(
                    {"enabled": True, "account_ids": [2], "promotion_account_ids": [2], "rollout_accounts": {"2": {"phase": "dynamic"}}}
                ),
            ),
        ]
    )
    await db.commit()
    return account, group, member, campaign, creative


@pytest.mark.asyncio
async def test_real_dispatcher_keeps_unknown_write_pending_and_blocks_replay(test_db, monkeypatch):
    account, group, member, campaign, creative = await seed(test_db)
    service = AcquisitionAutomationService(test_db)
    binding = Obj(id=1, account_id=account.id, campaign=campaign)
    service._list_enabled_ad_bindings_for_account = AsyncMock(return_value=[binding])
    service._list_joined_groups_for_account = AsyncMock(
        return_value=[
            Obj(
                id=member.id,
                account_id=account.id,
                telegram_group_id=group.group_id,
                group=group,
                probe_status="success",
            ),
        ]
    )
    service._growth_ad_health_allowed = AsyncMock(return_value=True)
    service._ad_skip_reason = AsyncMock(return_value=None)
    service._choose_delivery_creative = AsyncMock(return_value=creative)
    service._is_owned_group_ad_domain_excluded = AsyncMock(return_value=False)
    service._claim_ad_schedule_state = AsyncMock(return_value=(1, "schedule-token", None))
    service._finish_ad_schedule_state = AsyncMock()
    service._release_ad_delivery_budget = AsyncMock()
    service._send_ad = AsyncMock(
        side_effect=TelegramSendOutcomeUnknownError("send_outcome_unknown:TimeoutError")
    )

    @asynccontextmanager
    async def chat_lock(*args):
        yield

    monkeypatch.setattr(automation_module, "telegram_chat_advisory_lock", chat_lock)
    monkeypatch.setattr(
        automation_module,
        "get_ad_delivery_execution_settings",
        AsyncMock(return_value={"job_lease_seconds": 300}),
    )
    budget = {"remaining": 1}
    result = await service._run_ad_delivery_for_account(
        account.id,
        binding_ids=[binding.id],
        dry_run=False,
        delivery_budget=budget,
        delivery_budget_lock=asyncio.Lock(),
        reserved_ad_targets=set(),
        ad_target_lock=asyncio.Lock(),
        max_deliveries_per_account=1,
        stop_after_success=False,
        stop_after_failure=False,
    )
    log = await test_db.scalar(select(AdDeliveryLog))
    assert result.failed == 1
    assert result.details[-1]["action"] == "delivery_reconciliation_required"
    assert budget["remaining"] == 0
    service._release_ad_delivery_budget.assert_not_awaited()
    assert log.status == DeliveryStatus.PENDING.value
    assert log.error == "send_outcome_unknown:TimeoutError"
    assert service._send_ad.await_args.kwargs["reservation_token"] == log.reservation_token
    assert await qualification_service.send_gate(
        test_db, account.id, group.group_id, "hello", None, reservation_token=log.reservation_token
    ) == ("qualification_delivery_reconciliation_required")
    second, reason = await service._claim_growth_campaign_daily_quota(
        campaign=campaign,
        account_id=account.id,
        group=group,
        creative=creative,
    )
    assert second is None
    assert reason == "campaign_account_daily_limit"


@pytest.mark.asyncio
@pytest.mark.parametrize("still_member", [False, True])
async def test_leave_timeout_reconciles_membership_before_reporting_result(
    monkeypatch, still_member
):
    client = Obj(
        get_permissions=AsyncMock(
            side_effect=[
                Obj(is_admin=False, is_creator=False, has_left=False),
                Obj(has_left=not still_member),
            ]
        )
    )
    account = Obj(client=client)
    pool = Obj(acquire_by_id=AsyncMock(return_value=account), release=AsyncMock())
    db = Obj(scalar=AsyncMock(side_effect=[Obj(id=40), Obj(id=60)]))
    service = AcquisitionAutomationService(db, account_pool=pool)
    service.telegram_execution = Obj(leave_group=AsyncMock(side_effect=TimeoutError()))
    from telethon.tl.types import ChatForbidden
    service._resolve_group_entity_for_leave = AsyncMock(return_value=(ChatForbidden(id=42, title="reviewed"), None))
    monkeypatch.setattr(qualification_service, "policy", AsyncMock(return_value={"enabled": True}))
    monkeypatch.setattr(
        qualification_service, "authorize_leave", AsyncMock(return_value=(True, "reject"))
    )
    monkeypatch.setattr(automation_module, "is_owned_group_target", AsyncMock(return_value=False))
    error = await service._leave_group(2, Obj(group_id=42))
    assert error == ("leave_outcome_unconfirmed:TimeoutError" if still_member else None)
    assert client.get_permissions.await_count == 2
    service.telegram_execution.leave_group.assert_awaited_once()


@pytest.mark.asyncio
async def test_leave_preflight_timeout_never_runs_post_leave_reconciliation(monkeypatch):
    client = Obj(get_permissions=AsyncMock(side_effect=TimeoutError()))
    account = Obj(client=client)
    pool = Obj(acquire_by_id=AsyncMock(return_value=account), release=AsyncMock())
    db = Obj(scalar=AsyncMock(side_effect=[Obj(id=40), Obj(id=60)]))
    service = AcquisitionAutomationService(db, account_pool=pool)
    service.telegram_execution = Obj(leave_group=AsyncMock())
    from telethon.tl.types import ChatForbidden
    service._resolve_group_entity_for_leave = AsyncMock(return_value=(ChatForbidden(id=42, title="reviewed"), None))
    monkeypatch.setattr(qualification_service, "policy", AsyncMock(return_value={"enabled": True}))
    monkeypatch.setattr(
        qualification_service, "authorize_leave", AsyncMock(return_value=(True, "reject"))
    )
    monkeypatch.setattr(automation_module, "is_owned_group_target", AsyncMock(return_value=False))
    error = await service._leave_group(2, Obj(group_id=42))
    assert error is not None
    assert not error.startswith("leave_outcome_unconfirmed:")
    assert client.get_permissions.await_count == 1
    service.telegram_execution.leave_group.assert_not_awaited()
