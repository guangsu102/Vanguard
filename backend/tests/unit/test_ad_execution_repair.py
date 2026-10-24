"""Events preempt maintenance, while real retry timers remain authoritative."""

import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest

from app.core.account.models import AccountOutboundAttempt
from app.core.account.outbound_budget import OutboundBudgetBlocked
from app.core.account.rpc_governor import deferred_error
from app.core.account.send_receipts import _close_abandoned_pending
from app.core.account.telegram_execution import TelegramSendDeferredError
from app.core.scheduler import growth_dispatch
from app.core.settings_models import SystemSetting
from app.modules.acquisition import qualification_recovery, qualification_service
from app.modules.acquisition.models import AdCampaign, AdDeliveryLog, AdDeliveryScheduleState
from app.modules.acquisition.qualification_events import (
    acknowledge,
    pending,
    queue_due_gaps,
    request,
    retire_legacy_synthetic_gaps,
)
from app.modules.acquisition.review_waits import PERIODIC_RENEWAL, record
from tests.unit.test_qualification_service import setup


async def periodic(db, *, legacy=False):
    account, group, member, audit = await setup(db, decision="trial")
    now = datetime.utcnow()
    audit.attempts, audit.reason = 5, "ordinary_member_ads_verified"
    audit.next_retry_at = now + timedelta(days=14)
    member.review_next_at = audit.next_retry_at
    if not legacy:
        await record(db, audit.id, PERIODIC_RENEWAL, 0, audit.next_retry_at, now)
    await request(db, member, "rules", message_ids=[99])
    await db.commit()
    return account, group, member, audit


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_event_wakes_periodic_horizon_and_keeps_new_generation(test_db, legacy):
    _, _, member, audit = await periodic(test_db, legacy=legacy)
    now = datetime.utcnow()
    assert await queue_due_gaps(test_db, member.account_id) == 1
    assert audit.state == "queued" and audit.next_retry_at <= datetime.utcnow()
    assert audit.next_retry_at >= now
    await test_db.commit()
    assert member.account_id in (await growth_dispatch.account_sets(test_db, datetime.utcnow()))["review"]
    old = await pending(test_db, member)
    await request(test_db, member, "membership")
    await test_db.flush()
    assert not await acknowledge(test_db, member, old)
    latest = await pending(test_db, member)
    assert not latest["queued"] and latest["token"] != old["token"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["telegram_read_budget", "telegram_flood_wait", "review_exception:TimeoutError",
                                        "account_row_lock_busy", "qualification_slowmode_wait"])
async def test_event_keeps_explicit_backoff_even_with_old_periodic_reason(test_db, reason):
    _, _, member, audit = await periodic(test_db)
    now = datetime.utcnow()
    audit.next_retry_at = now + timedelta(minutes=5)
    await record(test_db, audit.id, reason, 1, audit.next_retry_at, now)
    await test_db.commit()
    original = audit.next_retry_at
    assert await queue_due_gaps(test_db, member.account_id) == 0
    assert audit.next_retry_at == original
    assert not (await pending(test_db, member))["queued"]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["running", "reviewing_ai", "waiting_ai"])
async def test_event_does_not_reclaim_active_reader_or_ai(test_db, state):
    _, _, member, audit = await periodic(test_db)
    audit.state = state
    await test_db.commit()
    assert await queue_due_gaps(test_db, member.account_id) == 0
    assert audit.state == state


@pytest.mark.asyncio
async def test_unlabelled_short_retry_and_legacy_budget_remain_conservative(test_db):
    _, _, member, audit = await periodic(test_db, legacy=True)
    audit.next_retry_at = datetime.utcnow() + timedelta(minutes=5)
    await test_db.commit()
    assert await queue_due_gaps(test_db, member.account_id) == 0
    audit.next_retry_at = datetime.utcnow() + timedelta(days=14)
    saved = json.loads(audit.evidence_json)
    saved["renewal_wait_reason"] = "telegram_read_budget"
    audit.evidence_json = json.dumps(saved)
    await test_db.commit()
    assert await queue_due_gaps(test_db, member.account_id) == 0


@pytest.mark.asyncio
async def test_local_recovery_finds_events_without_existing_review_or_budget_work(test_db, monkeypatch):
    _, _, member, audit = await periodic(test_db)
    cache = Obj(set=AsyncMock(return_value=True))
    monkeypatch.setattr("app.core.account.event_inbox.recover_safe_events", AsyncMock(return_value=0))
    result = await qualification_recovery.repair(test_db, cache, datetime.utcnow())
    assert result["event_reviews"] == 1 and result["budget_wakeups"] == 0
    assert audit.state == "queued" and (await pending(test_db, member))["queued"]


@pytest.mark.asyncio
async def test_local_recovery_releases_stale_terminal_ai_claim(test_db):
    _, _, member, audit = await setup(test_db, decision="reject")
    now = datetime.utcnow()
    audit.state = "running"
    audit.reason = "group_rules_ai_unresolved_conflict"
    audit.attempts = 1025
    audit.next_retry_at = now - timedelta(minutes=3)
    evidence = json.loads(audit.evidence_json)
    evidence.update({"ai_final": True, "ai_decision": "fail"})
    audit.evidence_json = json.dumps(evidence)
    await test_db.commit()

    recovered = await qualification_recovery._recover_stale_claims(
        test_db, now=now, limit=10
    )

    assert recovered == 1
    assert audit.state == "completed"
    assert audit.next_retry_at is None
    assert member.review_next_at is None
    assert json.loads(audit.evidence_json)["review_recovery"][-1]["action"] == "close_terminal_ai_reject"


@pytest.mark.asyncio
async def test_legacy_reconnect_gap_is_retired_without_reclaiming_valid_grant(test_db):
    account, _, member, audit = await setup(test_db, decision="trial")
    legacy_generation = "a" * 32
    marker = SystemSetting(
        key=f"qualification.event.{member.id}",
        value=json.dumps({
            "joined_at": member.joined_at.isoformat(),
            "kinds": ["gap"],
            "queued": True,
            "account_generation": legacy_generation,
        }),
    )
    audit.state = "queued"
    audit.next_retry_at = datetime.utcnow()
    member.review_next_at = audit.next_retry_at
    test_db.add(marker)
    await test_db.commit()

    assert await retire_legacy_synthetic_gaps(test_db, account.id) == 1
    assert audit.state == "completed"
    assert audit.next_retry_at is None
    assert member.review_next_at is None
    assert await pending(test_db, member) is None
    await test_db.commit()
    assert await retire_legacy_synthetic_gaps(test_db, account.id) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", [
    "source", "message", "rules", "unqueued", "new_generation",
    "new_account_gap", "expired", "revoked", "rejoined", "paused",
])
async def test_legacy_repair_keeps_observed_changes_and_invalid_grants(test_db, condition):
    account, _, member, audit = await setup(test_db, decision="trial")
    event = {
        "joined_at": member.joined_at.isoformat(),
        "kinds": ["gap"], "queued": True, "account_generation": "a" * 32,
    }
    if condition == "source":
        event["sources"] = {"gap": "telegram-update"}
    elif condition == "message":
        event["message_ids"] = [99]
    elif condition == "rules":
        event["kinds"].append("rules")
    elif condition == "unqueued":
        event["queued"] = False
    elif condition == "new_generation":
        event["account_generation"] = "gap:" + "b" * 32
    elif condition == "new_account_gap":
        test_db.add(SystemSetting(
            key=f"qualification.continuity.{account.id}", value="gap:" + "b" * 32,
        ))
    elif condition == "expired":
        audit.expires_at = datetime.utcnow() - timedelta(seconds=1)
    elif condition == "revoked":
        evidence = json.loads(audit.evidence_json)
        evidence["invalidated_at"] = datetime.utcnow().isoformat()
        audit.evidence_json = json.dumps(evidence)
    elif condition == "rejoined":
        member.joined_at += timedelta(hours=1)
    elif condition == "paused":
        member.ad_status = "paused"
    marker = SystemSetting(key=f"qualification.event.{member.id}", value=json.dumps(event))
    test_db.add(marker)
    audit.state = "queued"
    audit.next_retry_at = member.review_next_at = datetime.utcnow()
    await test_db.commit()
    original = (marker.value, audit.evidence_json, audit.next_retry_at, member.review_next_at)

    assert await retire_legacy_synthetic_gaps(test_db, account.id) == 0
    assert audit.state == "queued"
    assert (marker.value, audit.evidence_json, audit.next_retry_at, member.review_next_at) == original
    if condition in {"source", "message", "rules", "unqueued", "new_generation", "new_account_gap"}:
        assert await pending(test_db, member)


@pytest.mark.asyncio
async def test_event_is_assessed_instead_of_graduated_again(test_db, monkeypatch):
    _, _, member, audit = await periodic(test_db)
    await queue_due_gaps(test_db, member.account_id)
    await test_db.commit()
    monkeypatch.setattr("app.core.account.rpc_governor.check_read_ready", AsyncMock(return_value={}))
    monkeypatch.setattr("app.core.account.read_schedule.read_wait", AsyncMock(return_value=None))
    assess = AsyncMock(side_effect=RuntimeError("controlled review failure"))
    monkeypatch.setattr(qualification_service, "assess", assess)
    result = await qualification_service.run_reviews(Obj(db=test_db), account_id=2, limit=1)
    assess.assert_awaited_once()
    assert result["failed"] == 1
    assert audit.next_retry_at < datetime.utcnow() + timedelta(minutes=16)
    assert await pending(test_db, member)


@pytest.mark.asyncio
async def test_cancelled_review_releases_running_claim_without_acknowledging_event(test_db, monkeypatch):
    _, _, member, audit = await periodic(test_db)
    await queue_due_gaps(test_db, member.account_id)
    await test_db.commit()
    monkeypatch.setattr("app.core.account.rpc_governor.check_read_ready", AsyncMock(return_value={}))
    monkeypatch.setattr("app.core.account.read_schedule.read_wait", AsyncMock(return_value=None))
    monkeypatch.setattr(qualification_service, "assess", AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await qualification_service.run_reviews(Obj(db=test_db), account_id=2, limit=1)
    assert audit.state == "completed"
    assert audit.next_retry_at < datetime.utcnow() + timedelta(seconds=61)
    wait = await test_db.get(SystemSetting, f"qualification.wait.{audit.id}")
    assert json.loads(wait.value)["reason"] == "review_cancelled"
    assert await pending(test_db, member)


@pytest.mark.asyncio
@pytest.mark.parametrize("state,attempted,receipt,closed", [
    ("cancelled", False, None, 1), ("cancelled", True, None, 0),
    ("attempted", True, None, 0), ("unknown", True, None, 0),
    ("succeeded", True, 99, 0), ("reserved", False, None, 0),
])
async def test_pending_delivery_closes_only_with_authoritative_no_write_proof(test_db, state, attempted, receipt, closed):
    account, group, _, _ = await setup(test_db)
    now = datetime.utcnow()
    campaign = AdCampaign(name="deadline-cancel")
    test_db.add(campaign)
    await test_db.flush()
    log = AdDeliveryLog(account_id=account.id, group_id=group.id, telegram_group_id=group.group_id,
        ad_campaign_id=campaign.id, status="pending", reservation_token="deadline-cancel", created_at=now)
    test_db.add_all([log, AccountOutboundAttempt(account_id=account.id, attempt_key="ad:deadline-cancel",
        category="ad", target_key=str(group.group_id), state=state,
        attempted_at=now if attempted else None, message_id=receipt)])
    await test_db.commit()
    assert await _close_abandoned_pending(test_db, now, account.id, 2) == closed
    assert log.status == ("failed" if closed else "pending")


@pytest.mark.asyncio
async def test_pending_delivery_closes_expired_reserved_ledger_before_write(test_db):
    """An expired reservation with no attempt evidence did not reach Telegram."""
    account, group, _, _ = await setup(test_db)
    now = datetime.utcnow()
    campaign = AdCampaign(name="expired-reservation")
    test_db.add(campaign)
    await test_db.flush()
    log = AdDeliveryLog(
        account_id=account.id,
        group_id=group.id,
        telegram_group_id=group.group_id,
        ad_campaign_id=campaign.id,
        status="pending",
        reservation_token="expired-reservation",
        created_at=now - timedelta(hours=2),
    )
    attempt = AccountOutboundAttempt(
        account_id=account.id,
        attempt_key="ad:expired-reservation",
        category="ad",
        target_key=str(group.group_id),
        state="reserved",
        created_at=now - timedelta(hours=2),
        lease_expires_at=now - timedelta(minutes=1),
    )
    test_db.add_all([log, attempt])
    await test_db.commit()

    assert await _close_abandoned_pending(test_db, now, account.id, 10) == 1
    await test_db.refresh(log)
    await test_db.refresh(attempt)
    assert log.status == "failed"
    assert log.error.startswith("send_abandoned_before_attempt")
    assert attempt.state == "cancelled"
    assert attempt.completed_at == now
    assert attempt.lease_expires_at is None
    assert attempt.error_code == "reservation_expired_before_write"


def test_wrapped_outbound_wait_keeps_a_typed_deferred_outcome():
    wrapped = TelegramSendDeferredError("outbound_ad_interval", 496)
    assert deferred_error(wrapped) == ("outbound_ad_interval", 496)
    assert deferred_error(TelegramSendDeferredError("outbound_ad_interval", 1))
    # A real budget exception remains recognizable if a caller has not wrapped it.
    assert deferred_error(OutboundBudgetBlocked("outbound_ad_interval", 496)) == (
        "outbound_ad_interval", 496
    )


@pytest.mark.asyncio
async def test_qualification_event_is_queued_even_when_ad_schedule_is_future(test_db):
    account, group, member, audit = await setup(test_db, decision="trial")
    campaign = AdCampaign(name="future-schedule")
    test_db.add(campaign)
    await test_db.flush()
    test_db.add(AdDeliveryScheduleState(
        account_id=account.id,
        group_id=group.id,
        campaign_id=campaign.id,
        telegram_group_id=group.group_id,
        status="idle",
        next_due_at=datetime.utcnow() + timedelta(days=1),
    ))
    await request(test_db, member, "rules", message_ids=[101])
    await test_db.commit()
    assert await queue_due_gaps(test_db, account.id) == 1
    assert audit.state == "queued"
