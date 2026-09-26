"""Read-only review reproductions: SQLite and mocked Telegram only.

Assertions cover regressions found during the production review.
"""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.account.models import AccountOperationConfig, AccountOutboundAttempt
from app.core.account.outbound_budget import AccountOutboundBudgetService, OutboundBudgetBlocked
from app.modules.acquisition.adaptive_frequency import FrequencyService, record_live_mute
from app.modules.acquisition.automation import AcquisitionAutomationService
from app.modules.acquisition.models import AdCampaign, AdDeliveryLog
from tests.unit.test_adaptive_group_frequency import NOW
from tests.unit.test_adaptive_group_frequency import setup as frequency_setup
from tests.unit.test_qualification_service import setup as qualification_setup

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("restriction_seconds", [None, 0, 96 * 3600, 48 * 3600, -60, "unknown"])
@pytest.mark.parametrize("via_worker", [False, True])
async def test_live_restriction_decides_exit_and_revocation(
    test_db, restriction_seconds, via_worker
):
    account, group, member, audit = await qualification_setup(test_db)
    test_db.add(
        AccountOperationConfig(
            account_id=account.id,
            enabled=True,
            auto_ads_enabled=True,
            dynamic_capacity_enabled=True,
            adaptive_ads_enabled=True,
        )
    )
    campaign = AdCampaign(name="offline-review", enabled=True)
    test_db.add(campaign)
    await test_db.flush()
    frequency = FrequencyService(test_db)
    state = await frequency.state(group.group_id, {"group_type": "supergroup"}, create=True)
    now = datetime.utcnow()
    log = AdDeliveryLog(
        account_id=account.id,
        group_id=group.id,
        ad_campaign_id=campaign.id,
        telegram_group_id=group.group_id,
        status="pending",
        created_at=now - timedelta(hours=1),
        qualification_context_json=json.dumps(
            {"group_type": "supergroup", "frequency": frequency.context(state)}
        ),
    )
    test_db.add(log)
    await test_db.commit()
    await record_live_mute(
        test_db,
        account.id,
        group.group_id,
        {
            "group_type": "supergroup",
            "permissions": {
                "member": True,
                "can_send_text": False,
                "restriction_evidence": "telegram_banned_rights",
                "permanent_send_restriction_verified": True,
            },
        },
    )
    assert state.status == "exit_pending", "reproduction setup must have genuine mute evidence"
    # The mute has since been lifted. The freshest audit and live permissions allow text.
    evidence = json.loads(audit.evidence_json)
    evidence.update(
        collected_at=now.isoformat(), permissions={"member": True, "can_send_text": True}
    )
    audit.evidence_json = json.dumps(evidence)
    audit.checked_at = now
    member.review_status = "exit_pending"
    await test_db.commit()
    can_speak = Obj(
        is_admin=False,
        is_creator=False,
        has_left=False,
        is_banned=False,
        send_messages=True,
        send_plain=True,
        participant=Obj(banned_rights=None),
    )
    expected_exit = restriction_seconds in (0, 96 * 3600)
    if restriction_seconds == "unknown":
        del can_speak.participant
    elif restriction_seconds is not None:
        from telethon.tl.types import ChatBannedRights

        until = (
            datetime(1970, 1, 1)
            if restriction_seconds == 0
            else now + timedelta(seconds=restriction_seconds)
        )
        from telethon.tl.custom.participantpermissions import ParticipantPermissions
        from telethon.tl.types import ChannelParticipantBanned, PeerUser

        can_speak = ParticipantPermissions(
            ChannelParticipantBanned(
                peer=PeerUser(account.id),
                kicked_by=123,
                date=now,
                banned_rights=ChatBannedRights(until_date=until, send_plain=True),
                left=False,
            ),
            chat=False,
        )
    client = Obj(get_permissions=AsyncMock(side_effect=[can_speak, Obj(has_left=True)]))
    lease = Obj(client=client)
    pool = Obj(acquire_by_id=AsyncMock(return_value=lease), release=AsyncMock())
    worker = AcquisitionAutomationService(test_db, account_pool=pool)
    from telethon.tl.types import Channel, ChatPhotoEmpty

    entity = Channel(
        id=group.group_id, title="offline", photo=ChatPhotoEmpty(), date=now, megagroup=True
    )
    worker._resolve_group_entity_for_leave = AsyncMock(return_value=(entity, None))
    worker.telegram_execution = Obj(leave_group=AsyncMock())
    worker.logger = Mock()
    if via_worker:
        from app.modules.acquisition.adaptive_frequency import run_frequency_exits

        outcome = await run_frequency_exits(worker)
        assert outcome["processed"] == 1
    else:
        outcome = await worker._leave_group(account.id, worker._discovered_group_from_model(group))
    if expected_exit:
        worker.telegram_execution.leave_group.assert_awaited_once()
        if via_worker:
            assert member.status == "left"
        return
    if restriction_seconds == "unknown":
        worker.telegram_execution.leave_group.assert_not_awaited()
        assert state.status == "exit_pending"
        return
    print(
        "RESTORED_PERMISSION_EXIT",
        {
            "result": outcome,
            "leave_calls": worker.telegram_execution.leave_group.await_count,
            "current_audit": audit.decision,
            "current_can_send_text": True,
        },
    )
    assert outcome != "group_identity_mismatch", "invalid reproduction entity"
    worker.telegram_execution.leave_group.assert_not_awaited()
    if not via_worker:
        assert outcome == "qualification_send_restriction_cleared"
    assert member.review_status == "initial_pending" and member.ad_status == "blocked"
    assert state.status == "active"
    assert await frequency.exit_reason(account.id, group, member) is None


async def test_failed_actual_send_preserves_account_pacing(test_db):
    account, config, campaign, frequency, state = await frequency_setup(test_db)
    test_db.add(
        AccountOutboundAttempt(
            account_id=account.id,
            attempt_key="offline-failed",
            category="ad",
            target_key="old-group",
            state="failed",
            attempted_at=NOW - timedelta(seconds=1),
            completed_at=NOW,
            error_code="ChatWriteForbiddenError",
            context_json="{}",
        )
    )
    await test_db.commit()
    budget = AccountOutboundBudgetService(test_db)
    info = await budget.snapshot(account.id, NOW)
    print(
        "FAILED_SEND_PACING",
        {
            "configured_interval": info["limits"]["ad_interval_seconds"],
            "next_allowed_at": info["next_allowed_at"],
            "probe_used": info["ad_lanes"]["probe"]["used_rolling_24h"],
        },
    )
    # Releasing daily quota is valid; removing the actual RPC attempt's pacing is not.
    assert info["ad_lanes"]["probe"]["used_rolling_24h"] == 0
    with pytest.raises(OutboundBudgetBlocked, match="outbound_ad_interval"):
        await budget._check(account.id, "ad", NOW, context={"frequency": frequency.context(state)})


@pytest.mark.parametrize(
    "left,view_blocked,expected_member",
    [(False, False, True), (True, False, False), (False, True, False)],
)
async def test_native_muted_member_is_distinct_from_expulsion(left, view_blocked, expected_member):
    from telethon.tl.custom.participantpermissions import ParticipantPermissions
    from telethon.tl.types import ChannelParticipantBanned, ChatBannedRights, PeerUser

    from app.modules.acquisition.qualification_exit_guard import live_restriction

    now = datetime.utcnow()
    permission = ParticipantPermissions(
        ChannelParticipantBanned(
            peer=PeerUser(1),
            kicked_by=2,
            date=now,
            banned_rights=ChatBannedRights(
                until_date=datetime(1970, 1, 1), send_plain=True, view_messages=view_blocked
            ),
            left=left,
        ),
        chat=False,
    )
    assert permission.is_banned is True
    result = live_restriction(Obj(default_banned_rights=None), permission, now)
    assert result["permissions"]["member"] is expected_member


async def test_deletion_event_locks_delivery_without_locking_nullable_joins(test_db, monkeypatch):
    from sqlalchemy.dialects.postgresql import dialect

    from app.modules.acquisition.adaptive_frequency import queue_deleted_observation
    from tests.unit.test_adaptive_group_frequency import TARGET, delivery

    account, config, campaign, frequency, state = await frequency_setup(test_db)
    log = await delivery(test_db, account, campaign, state, NOW)
    await test_db.commit()
    original = test_db.scalars
    statements = []

    async def checked(statement, *args, **kwargs):
        sql = str(statement.compile(dialect=dialect()))
        statements.append(sql)
        assert "LEFT OUTER JOIN" in sql
        assert "FOR UPDATE OF ad_delivery_log" in sql
        return await original(statement, *args, **kwargs)

    monkeypatch.setattr(test_db, "scalars", checked)
    await queue_deleted_observation(
        test_db, account.id, Obj(chat_id=TARGET, deleted_ids=[log.telegram_message_id])
    )
    assert len(statements) == 1
    assert (
        log.survival_status == "pending"
        and log.survival_error == "deletion_event_requires_confirmation"
    )
