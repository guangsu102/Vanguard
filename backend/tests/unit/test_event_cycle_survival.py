"""Real DB transitions with synthetic Telegram facts; never send or leave live."""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.core.account.listener_facts import apply_fact
from app.core.group.models import Group, GroupAccountMembership
from app.modules.acquisition.adaptive_frequency import (
    DAY,
    queue_deleted_observation,
    run_frequency_exits,
)
from app.modules.acquisition.automation import AcquisitionAutomationService
from app.modules.acquisition.daily_frequency import DailyFrequencyService
from app.modules.acquisition.mature_survival import retire_mature_checkpoints
from app.modules.acquisition.models import GroupAdFrequencyEvent
from tests.unit.test_adaptive_group_frequency import NOW, TARGET, delivery, setup
from tests.unit.test_daily_group_frequency import facts

pytestmark = pytest.mark.asyncio


async def scenario(db, *, quota=1, mature=False):
    account, config, campaign, frequency, state = await setup(db, quota, mature)
    group = Group(group_id=TARGET, title="cycle")
    db.add(group)
    await db.flush()
    member = GroupAccountMembership(
        group_id=group.id,
        telegram_group_id=TARGET,
        account_id=account.id,
        status="joined",
        review_status="approved",
        ad_status="active",
        joined_at=NOW - DAY,
    )
    db.add(member)
    state.epoch_started_at = NOW
    log = await delivery(db, account, campaign, state, NOW, status="pending")
    log.group_id = group.id
    log.survived_two_minute_at = log.survived_one_hour_at = None
    await DailyFrequencyService(db).note_sent(log)
    await db.commit()
    return account, config, frequency, state, member, log


async def test_trial_worker_reads_once_at_next_cycle(test_db, monkeypatch):
    from app.modules.acquisition import automation
    from app.modules.acquisition import daily_frequency as module

    account, _, frequency, state, member, log = await scenario(test_db)
    assert log.survival_stage == "daily" and log.survival_check_due_at is None
    assert json.loads(log.qualification_context_json)["survival_policy"] == "probe_cycle"
    clock = NOW

    class Frozen(datetime):
        @classmethod
        def utcnow(cls):
            return clock

    monkeypatch.setattr(module, "datetime", Frozen)
    monkeypatch.setattr(automation, "_now", lambda: clock)
    service = AcquisitionAutomationService(test_db, account_pool=SimpleNamespace())
    service._sync_account_pool = AsyncMock()
    service._read_survival_facts = AsyncMock(return_value=facts(log))
    for elapsed in [timedelta(minutes=2), timedelta(hours=1), DAY - timedelta(seconds=1)]:
        clock = NOW + elapsed
        await service.check_ad_survival(limit=1)
        service._read_survival_facts.assert_not_awaited()
        assert state.quota == 1 and not state.mature
    clock = NOW + DAY
    result = await service.check_ad_survival(limit=1)
    assert result["daily_survived"] == 1 and state.quota == 2 and state.mature
    service._read_survival_facts.assert_awaited_once()
    assert log.survived_two_minute_at is None and log.survived_one_hour_at is None
    assert log.survived_twenty_four_hour_at == clock


async def test_exact_trial_deletion_queues_one_exit_without_reading(test_db, monkeypatch):
    account, _, frequency, state, member, log = await scenario(test_db)
    fact = {"kind": "deleted", "peer": TARGET, "message_id": log.telegram_message_id}
    await apply_fact(test_db, account.id, fact)
    await apply_fact(test_db, account.id, fact)
    assert state.status == "exit_pending" and member.ad_status == "blocked"
    assert state.reason == "frequency_probe_deleted" and log.survival_status == "deleted"
    assert log.survival_check_due_at is None
    assert await test_db.scalar(select(func.count()).select_from(GroupAdFrequencyEvent)) == 1
    service = AcquisitionAutomationService(test_db, account_pool=SimpleNamespace())
    service._leave_group = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "app.modules.acquisition.qualification_service.authorize_leave",
        AsyncMock(return_value=(True, None)),
    )
    await run_frequency_exits(service)
    await run_frequency_exits(service)
    service._leave_group.assert_awaited_once()
    assert member.status == "left"


@pytest.mark.parametrize(
    "kind,extra",
    [
        ("ad_gap", {}),
        ("deletion_candidates", {"overflow": True, "ids": []}),
    ],
)
async def test_gap_is_not_a_deletion_or_exit(test_db, kind, extra):
    account, _, frequency, state, member, log = await scenario(test_db)
    await apply_fact(test_db, account.id, {"kind": kind, "peer": TARGET, **extra})
    assert state.status == "active" and member.status == "joined"
    assert log.survival_error == "deletion_event_requires_confirmation"
    assert (await frequency.readiness(TARGET, NOW))[0].reason == "frequency_group_daily_cap"
    assert await test_db.scalar(select(func.count()).select_from(GroupAdFrequencyEvent)) == 0
    version = log.survival_version
    log.survival_claim_token = "active-read-claim"
    await test_db.commit()
    await apply_fact(test_db, account.id, {"kind": kind, "peer": TARGET, **extra})
    assert log.survival_claim_token == "active-read-claim" and log.survival_version == version


@pytest.mark.parametrize("wrong", ["account", "group", "message"])
async def test_foreign_deletion_cannot_match_a_receipt(test_db, wrong):
    account, _, frequency, state, member, log = await scenario(test_db)
    await queue_deleted_observation(
        test_db,
        account.id + (wrong == "account"),
        SimpleNamespace(
            chat_id=TARGET - (wrong == "group"),
            deleted_ids=[log.telegram_message_id + (wrong == "message")],
        ),
        confirmed=True,
    )
    assert state.status == "active" and log.survival_status != "deleted"


@pytest.mark.parametrize("stale", ["epoch", "rejoin"])
async def test_historical_deletion_records_fact_without_penalizing_current_group(test_db, stale):
    account, _, frequency, state, member, log = await scenario(test_db)
    if stale == "epoch":
        state.epoch += 1
        state.mature, state.quota = True, 4
    else:
        member.joined_at = NOW + timedelta(minutes=1)
    await test_db.commit()
    epoch, quota = state.epoch, state.quota
    await apply_fact(
        test_db,
        account.id,
        {"kind": "deleted", "peer": TARGET, "message_id": log.telegram_message_id},
    )
    assert state.status == "active" and (state.epoch, state.quota) == (epoch, quota)
    assert member.ad_status == "active" and log.survival_status == "deleted"
    assert json.loads(log.qualification_context_json)["deletion_event"]["applied_to_cycle"] is False


async def test_event_fences_reader_that_was_already_running(test_db):
    account, _, frequency, state, member, log = await scenario(test_db)
    daily = DailyFrequencyService(test_db)
    claim = await daily.claim(TARGET, NOW + DAY)
    await test_db.commit()
    await apply_fact(
        test_db,
        account.id,
        {"kind": "deleted", "peer": TARGET, "message_id": log.telegram_message_id},
    )
    assert await daily.finish(claim, facts(log), None, NOW + DAY) == "daily_stale_claim"
    assert state.status == "exit_pending" and not state.mature


async def test_mature_event_halves_once_and_never_forces_trial_exit(test_db):
    account, _, frequency, state, member, log = await scenario(test_db, quota=4, mature=True)
    fact = {
        "kind": "deletion_candidates",
        "peer": TARGET,
        "ids": [log.telegram_message_id],
        "overflow": False,
    }
    await apply_fact(test_db, account.id, fact)
    await apply_fact(test_db, account.id, fact)
    assert state.quota == 2 and state.epoch == 2 and state.status == "active"
    assert member.status == "joined" and member.ad_status == "active"


async def test_retire_old_trial_tasks_preserves_evidence_and_active_claims(test_db):
    account, _, frequency, state, member, log = await scenario(test_db)
    log.survival_stage, log.survival_status = "two_minute", "pending"
    log.survival_check_due_at = NOW + timedelta(minutes=2)
    log.survival_claim_token, log.survival_claim_expires_at = "active", NOW + timedelta(minutes=3)
    await test_db.commit()
    assert await retire_mature_checkpoints(test_db, NOW, include_probes=True) == 0
    assert (
        await retire_mature_checkpoints(test_db, NOW + timedelta(minutes=4), include_probes=True)
        == 1
    )
    assert log.survival_stage == "daily" and log.survived_two_minute_at is None
    assert log.survived_one_hour_at is None and log.survived_twenty_four_hour_at is None


async def test_unknown_send_never_becomes_cycle_success(test_db):
    account, _, frequency, state, member, log = await scenario(test_db)
    log.status, log.survival_status = "reconciliation_required", "pending"
    await test_db.commit()
    assert await retire_mature_checkpoints(test_db, NOW + DAY, include_probes=True) == 0
    await apply_fact(
        test_db,
        account.id,
        {"kind": "deleted", "peer": TARGET, "message_id": log.telegram_message_id},
    )
    assert state.status == "active" and log.status == "reconciliation_required"
    assert await DailyFrequencyService(test_db).claim(TARGET, NOW + DAY) is None


async def test_cycle_review_waits_for_an_active_legacy_reader(test_db):
    account, _, frequency, state, member, log = await scenario(test_db)
    log.survival_status, log.survival_stage = "pending", "twenty_four_hour"
    log.survival_claim_token = "legacy-reader"
    log.survival_claim_expires_at = NOW + DAY + timedelta(minutes=3)
    await test_db.commit()
    daily = DailyFrequencyService(test_db)
    assert await daily.claim(TARGET, NOW + DAY) is None
    assert await retire_mature_checkpoints(test_db, NOW + DAY, include_probes=True) == 0
    later = NOW + DAY + timedelta(minutes=4)
    assert await retire_mature_checkpoints(test_db, later, include_probes=True) == 1
    assert await daily.claim(TARGET, later) is not None


async def test_retirement_preserves_non_adaptive_advertisements(test_db):
    account, config, frequency, state, member, log = await scenario(test_db)
    log.survival_status, log.survival_stage = "pending", "two_minute"
    config.adaptive_ads_enabled = False
    await test_db.commit()
    assert await retire_mature_checkpoints(test_db, NOW, include_probes=True) == 0
    config.adaptive_ads_enabled = True
    log.qualification_context_json = "{}"
    await test_db.commit()
    assert await retire_mature_checkpoints(test_db, NOW, include_probes=True) == 0
