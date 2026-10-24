"""Roster retirement recovers real grants; changed exclusions never borrow them."""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest

from app.core.account.models import AccountStatus, TelegramAccount
from app.core.settings_models import SystemSetting
from app.modules.acquisition.automation import AcquisitionAutomationService
from app.modules.acquisition.models import AdCampaign, AdDeliveryScheduleState
from app.modules.acquisition.qualification_identity_refresh import (
    queue_identity_refreshes,
    reusable_identity_context,
)
from app.modules.acquisition.qualification_renewal import try_light_renewal
from app.modules.acquisition.qualification_service import (
    SETTING_KEY,
    policy,
    run_reviews,
    send_gate,
)
from app.modules.acquisition.qualification_system_identity import covered_system_ids
from tests.unit.test_qualification_service import setup

pytestmark = pytest.mark.asyncio


async def retired_roster(db):
    account, group, member, row = await setup(db, decision="trial")
    retired = TelegramAccount(
        id=3, identifier="retired", session_name="retired", status=AccountStatus.ONLINE
    )
    db.add(retired)
    setting = await db.get(SystemSetting, SETTING_KEY)
    config = json.loads(setting.value)
    config["system_account_user_ids"]["3"] = 300
    setting.value = json.dumps(config)
    await db.flush()
    _, old_fingerprint = await covered_system_ids(db, config, account_id=2)
    previous = json.loads(row.evidence_json)
    old = datetime.utcnow() - timedelta(days=2)
    previous.update(
        system_identity_fingerprint=old_fingerprint,
        collected_at=old.isoformat(),
        checked_at=old.isoformat(),
        advertising_audit={"ad_allowed": True, "policy_mode": "soft_ad_trial"},
    )
    row.evidence_json = json.dumps(previous)
    await db.commit()
    await db.delete(retired)
    await db.commit()
    pool = Obj(add_account_from_db=AsyncMock(), acquire_by_id=AsyncMock(), release=AsyncMock())
    return Obj(db=db, account_pool=pool), account, group, member, row, previous


async def schedule(
    db, account, group, *, reason="unknown:qualification_system_identity_unconfirmed"
):
    campaign = AdCampaign(name="identity-recovery", enabled=True, status="active")
    db.add(campaign)
    await db.flush()
    state = AdDeliveryScheduleState(
        campaign_id=campaign.id,
        account_id=account.id,
        group_id=group.id,
        telegram_group_id=group.group_id,
        status="retry",
        last_reason=reason,
        next_due_at=datetime.utcnow(),
    )
    db.add(state)
    await db.commit()
    return campaign, state


async def test_roster_deletion_queues_once_and_preserves_read_backoff(test_db):
    service, account, group, _, row, previous = await retired_roster(test_db)
    _, state = await schedule(test_db, account, group)
    old_checked, old_expiry = row.checked_at, row.expires_at
    start = datetime.utcnow()
    config = await policy(test_db, fresh=True)
    identity = await reusable_identity_context(test_db, config, account.id, previous)
    assert identity["basis"] == "retired_accounts_same_exclusions"
    assert await queue_identity_refreshes(test_db, config, account_id=account.id) == 1
    assert row.next_retry_at <= datetime.utcnow()
    assert row.checked_at == old_checked and row.expires_at == old_expiry
    assert (
        json.loads(row.evidence_json)["system_identity_fingerprint"]
        == previous["system_identity_fingerprint"]
    )
    await test_db.refresh(state)
    assert state.next_due_at >= start + timedelta(minutes=5)
    due = row.next_retry_at = datetime.utcnow() + timedelta(hours=6)
    await test_db.commit()
    assert await queue_identity_refreshes(test_db, config, account_id=account.id) == 0
    assert row.next_retry_at == due
    service.account_pool.acquire_by_id.assert_not_awaited()


async def test_verified_retired_roster_renews_and_wakes_blocked_ad_without_new_telegram_evidence(
    test_db,
):
    service, account, group, member, row, previous = await retired_roster(test_db)
    _, state = await schedule(test_db, account, group)
    old_checked, old_expiry = row.checked_at, row.expires_at
    assert (
        await send_gate(test_db, account.id, group.group_id, "check profile", None)
        == "qualification_system_identity_unconfirmed"
    )
    context, reason = await AcquisitionAutomationService(test_db)._qualified_ad_context(
        account.id, group.group_id, datetime.utcnow()
    )
    assert context is None and reason == "qualification_system_identity_unconfirmed"
    await queue_identity_refreshes(
        test_db, await policy(test_db, fresh=True), account_id=account.id
    )
    result = await try_light_renewal(
        service, account, group, member, row, json.loads(row.evidence_json), force_refresh=False
    )
    assert result.passed
    saved = json.loads(row.evidence_json)
    assert saved["collected_at"] == previous["collected_at"]
    assert saved["evidence"] == previous["evidence"]
    assert row.checked_at == old_checked and row.expires_at == old_expiry
    assert saved["system_user_ids"] == previous["system_user_ids"]
    assert saved["system_identity_managed_account_ids"] == [2]
    assert saved["system_identity_revalidation"]["basis"] == "retired_accounts_same_exclusions"
    assert "identity_refresh" not in saved
    assert await send_gate(test_db, account.id, group.group_id, "check profile", None) is None
    await test_db.refresh(state)
    assert state.next_due_at <= datetime.utcnow() and state.last_reason is None
    service.account_pool.acquire_by_id.assert_not_awaited()


@pytest.mark.parametrize(
    "change",
    [
        "new_account",
        "mapped_new_account",
        "mapping_swap",
        "extra_identity",
        "missing_coverage",
        "unknown_hash",
        "malformed_roster",
        "expired",
    ],
)
async def test_changed_or_unproved_scope_cannot_reuse_retired_roster(test_db, change):
    service, account, group, member, row, previous = await retired_roster(test_db)
    config = await policy(test_db, fresh=True)
    if change in {"new_account", "mapped_new_account"}:
        test_db.add(
            TelegramAccount(id=4, identifier="new", session_name="new", status=AccountStatus.ONLINE)
        )
        if change == "mapped_new_account":
            config["system_account_user_ids"]["4"] = 400
    if change == "mapping_swap":
        config["system_account_user_ids"] = {"2": 300, "3": 200}
    if change == "extra_identity":
        config["system_user_ids"].append(900)
    if change == "missing_coverage":
        previous["system_identity_coverage"] = False
    if change == "unknown_hash":
        previous["system_identity_fingerprint"] = "unproved"
    if change == "malformed_roster":
        previous["system_identity_managed_account_ids"] = [True, 3]
    if change == "expired":
        row.expires_at = datetime.utcnow() - timedelta(seconds=1)
    setting = await test_db.get(SystemSetting, SETTING_KEY)
    setting.value = json.dumps(config)
    row.evidence_json = json.dumps(previous)
    await test_db.commit()
    result = await try_light_renewal(
        service, account, group, member, row, previous, force_refresh=False
    )
    assert result is None
    assert (
        json.loads(row.evidence_json)["system_identity_fingerprint"]
        == previous["system_identity_fingerprint"]
    )
    service.account_pool.acquire_by_id.assert_not_awaited()


async def test_changed_exclusions_revoke_an_event_grant_until_full_review(test_db):
    _, account, group, _, row, previous = await retired_roster(test_db)
    previous["authorization_mode"] = "events"
    row.evidence_json = json.dumps(previous)
    config = await policy(test_db, fresh=True)
    config["system_user_ids"].append(900)
    setting = await test_db.get(SystemSetting, SETTING_KEY)
    setting.value = json.dumps(config)
    await test_db.commit()
    assert await queue_identity_refreshes(test_db, config, account_id=account.id) == 1
    saved = json.loads(row.evidence_json)
    assert saved["identity_refresh"]["mode"] == "full" and saved["invalidated_at"]
    assert (
        await send_gate(test_db, account.id, group.group_id, "check profile", None)
        == "qualification_expired"
    )


async def test_identity_backoff_follows_review_deadline(test_db):
    _, account, group, _, row, _ = await retired_roster(test_db)
    campaign, state = await schedule(test_db, account, group)
    now = datetime.utcnow()
    deadline = row.next_retry_at = now + timedelta(hours=6)
    state.status, state.lock_token = "sending", "claim"
    await test_db.commit()
    await AcquisitionAutomationService(test_db)._finish_ad_schedule_state(
        state.id,
        "claim",
        campaign=campaign,
        succeeded=False,
        reason="unknown:qualification_system_identity_unconfirmed",
        completed_at=now,
    )
    await test_db.refresh(state)
    assert state.status == "retry" and state.next_due_at == deadline
    assert state.lock_token is None and state.lease_expires_at is None


async def test_dispatcher_automatically_recovers_roster_when_telegram_budget_is_exhausted(
    test_db, monkeypatch
):
    service, account, group, _, row, previous = await retired_roster(test_db)
    from app.core.account.rpc_governor import RpcDeferred

    monkeypatch.setattr(
        "app.core.account.read_schedule.check_read_ready",
        AsyncMock(side_effect=RpcDeferred("telegram_read_budget", 3600)),
    )
    actor = AcquisitionAutomationService(test_db, account_pool=service.account_pool)
    outcome = await run_reviews(actor, account_id=account.id, limit=2)
    assert outcome["processed"] == 1
    assert row.state == "completed" and row.next_retry_at is None
    saved = json.loads(row.evidence_json)
    assert saved["system_identity_fingerprint"] != previous["system_identity_fingerprint"]
    assert saved["collected_at"] == previous["collected_at"]
    assert await send_gate(test_db, account.id, group.group_id, "check profile", None) is None
    service.account_pool.acquire_by_id.assert_not_awaited()


@pytest.mark.parametrize(
    "hold", ["paused", "manual_required", "exit_pending", "owned_group_excluded"]
)
async def test_identity_queue_preserves_membership_holds(test_db, hold):
    _, account, _, member, row, previous = await retired_roster(test_db)
    if hold == "paused":
        member.ad_status = hold
    else:
        member.review_status = hold
    await test_db.commit()
    assert (
        await queue_identity_refreshes(
            test_db, await policy(test_db, fresh=True), account_id=account.id
        )
        == 0
    )
    assert json.loads(row.evidence_json) == previous and row.next_retry_at is None
