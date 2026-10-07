"""Risk-controlled accounts drop their pending work instead of hoarding it."""

from datetime import datetime, timedelta

from sqlalchemy import select

from app.core.account.backlog_discard import (
    discard_account_backlog,
    discard_ineligible_account_backlog,
)
from app.core.account.models import TelegramAccount
from app.core.group.models import Group, GroupAccountMembership
from app.core.settings_models import SystemSetting
from app.modules.acquisition.models import (
    AdCampaign,
    AdDeliveryScheduleState,
    AutoJoinAttempt,
    GroupQualificationAudit,
)


async def _seed_account(test_db, *, account_id: int, risk_level: str, status: str = "online"):
    account = TelegramAccount(
        id=account_id,
        identifier=f"discard-{account_id}",
        session_name=f"discard-{account_id}",
        risk_level=risk_level,
        status=status,
    )
    test_db.add(account)
    return account


async def _seed_backlog(test_db, account: TelegramAccount, *, membership_id: int = 900):
    group = Group(group_id=700000000 + account.id, username=f"discard_group_{account.id}", title="discard")
    test_db.add(group)
    await test_db.flush()
    membership = GroupAccountMembership(
        id=membership_id + account.id,
        group_id=group.id,
        telegram_group_id=group.group_id,
        account_id=account.id,
        status="joined",
        review_status="review_2h",
    )
    test_db.add(membership)
    await test_db.flush()
    audit = GroupQualificationAudit(
        batch_id=f"batch-{account.id}",
        membership_id=membership.id,
        account_id=account.id,
        group_id=group.id,
        policy_version="pp-ai-qualification-v3",
        state="queued",
        next_retry_at=datetime.utcnow() - timedelta(minutes=5),
    )
    test_db.add(audit)
    await test_db.flush()
    test_db.add(
        SystemSetting(
            key=f"qualification.wait.{audit.id}",
            value='{"reason": "telegram_read_budget", "retry_at": "2026-10-04T14:42:07"}',
        )
    )
    reserved = AutoJoinAttempt(
        account_id=account.id,
        status="pending",
        reason="join_reserved",
        request_state="reserved",
        target_key="username:discard_target",
        reservation_expires_at=datetime.utcnow() + timedelta(hours=1),
        attempted_at=datetime.utcnow(),
    )
    sent = AutoJoinAttempt(
        account_id=account.id,
        status="pending",
        reason="join_request_pending_approval",
        telegram_action_attempted=True,
        request_state="sent",
        target_key="username:pending_group",
        reconciliation_next_at=datetime.utcnow() - timedelta(days=4),
        attempted_at=datetime.utcnow() - timedelta(days=4),
    )
    test_db.add_all([reserved, sent])
    campaign = AdCampaign(name=f"discard-campaign-{account.id}")
    test_db.add(campaign)
    await test_db.flush()
    test_db.add(
        AdDeliveryScheduleState(
            campaign_id=campaign.id,
            account_id=account.id,
            group_id=group.id,
            telegram_group_id=group.group_id,
            next_due_at=datetime.utcnow() + timedelta(hours=1),
            status="pending",
            lock_token="lease-token",
        )
    )
    await test_db.commit()
    return audit.id


async def test_quarantined_account_backlog_is_discarded(test_db):
    account = await _seed_account(test_db, account_id=501, risk_level="quarantined")
    audit_id = await _seed_backlog(test_db, account)

    stats = await discard_account_backlog(test_db, account.id, reason="platform_group_write_banned")

    assert stats["audits_cancelled"] == 1
    assert stats["review_waits_cleared"] == 1
    assert stats["joins_released"] == 1
    assert stats["joins_reconcile_stopped"] == 1
    assert stats["delivery_slots_reset"] == 1

    audit = await test_db.get(GroupQualificationAudit, audit_id)
    assert audit.state == "cancelled"
    assert audit.reason.startswith("account_backlog_discarded:")
    assert audit.next_retry_at is None
    assert await test_db.get(SystemSetting, f"qualification.wait.{audit_id}") is None

    attempts = (
        await test_db.execute(
            select(AutoJoinAttempt).where(AutoJoinAttempt.account_id == account.id)
        )
    ).scalars().all()
    by_state = {item.request_state: item for item in attempts}
    assert by_state["released"].status == "skipped"
    assert by_state["released"].reason.startswith("account_backlog_discarded:")
    assert by_state["sent"].reconciliation_status == "resolved"
    assert by_state["sent"].reconciliation_next_at is None

    slot = (
        await test_db.execute(
            select(AdDeliveryScheduleState).where(
                AdDeliveryScheduleState.account_id == account.id
            )
        )
    ).scalar_one()
    assert slot.status == "idle"
    assert slot.lock_token is None


async def test_discard_is_idempotent(test_db):
    account = await _seed_account(test_db, account_id=502, risk_level="quarantined")
    await _seed_backlog(test_db, account)

    await discard_account_backlog(test_db, account.id, reason="test")
    second = await discard_account_backlog(test_db, account.id, reason="test")

    assert second["audits_cancelled"] == 0
    assert second["review_waits_cleared"] == 0
    assert second["joins_released"] == 0
    assert second["joins_reconcile_stopped"] == 0
    assert second["delivery_slots_reset"] == 0


async def test_sweep_discards_only_risk_excluded_accounts(test_db):
    quarantined = await _seed_account(test_db, account_id=503, risk_level="quarantined")
    healthy = await _seed_account(test_db, account_id=504, risk_level="normal")
    await _seed_backlog(test_db, quarantined, membership_id=950)
    await _seed_backlog(test_db, healthy, membership_id=990)

    totals = await discard_ineligible_account_backlog(test_db)

    assert totals["accounts_swept"] == 1
    assert totals["audits_cancelled"] == 1
    kept = (
        await test_db.execute(
            select(GroupQualificationAudit).where(
                GroupQualificationAudit.account_id == healthy.id
            )
        )
    ).scalars().all()
    assert [item.state for item in kept] == ["queued"]
    dropped = (
        await test_db.execute(
            select(GroupQualificationAudit).where(
                GroupQualificationAudit.account_id == quarantined.id
            )
        )
    ).scalars().all()
    assert [item.state for item in dropped] == ["cancelled"]


async def test_paused_but_eligible_account_keeps_backlog(test_db):
    """A transient risk pause must not discard work; only hard risk levels do."""
    paused = TelegramAccount(
        id=505,
        identifier="discard-paused",
        session_name="discard-paused",
        risk_level="normal",
        status="online",
    )
    test_db.add(paused)
    await _seed_backlog(test_db, paused, membership_id=970)
    from datetime import datetime as dt

    paused.risk_pause_until = dt.utcnow() + timedelta(hours=2)
    await test_db.commit()

    totals = await discard_ineligible_account_backlog(test_db)

    kept = (
        await test_db.execute(
            select(GroupQualificationAudit).where(
                GroupQualificationAudit.account_id == paused.id
            )
        )
    ).scalars().all()
    assert [item.state for item in kept] == ["queued"]
    assert totals["accounts_swept"] == 0
