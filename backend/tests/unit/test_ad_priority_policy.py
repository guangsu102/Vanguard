"""Ad-priority behavior, evidence adoption, and resource isolation without Telegram."""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace as Obj

import pytest
from sqlalchemy import select

from app.core.account import rpc_governor as rpc
from app.core.account.models import AccountOperationConfig
from app.modules.acquisition.adaptive_frequency import FrequencyService
from app.modules.acquisition.daily_frequency import DailyFrequencyService
from app.modules.acquisition.established_frequency import adopt_established
from app.modules.acquisition.mature_survival import retire_mature_checkpoints
from app.modules.acquisition.models import GroupAdFrequencyEvent
from app.modules.acquisition.qualification_continuity import restore_own_authorization
from tests.unit.test_ad_read_reserve import mock_usage
from tests.unit.test_adaptive_group_frequency import NOW, TARGET, delivery, setup
from tests.unit.test_daily_group_frequency import facts
from tests.unit.test_qualification_continuity import proven

pytestmark = pytest.mark.asyncio


async def test_strict_own_evidence_adopts_once_without_rewriting_receipt_or_ledger(test_db):
    account, group, member, _, log = await proven(test_db)
    assert (await restore_own_authorization(test_db, member.id, apply=True))["restored"]
    config = await test_db.scalar(
        select(AccountOperationConfig).where(AccountOperationConfig.account_id == account.id)
    )
    if config is None:
        config = AccountOperationConfig(account_id=account.id, enabled=True)
        test_db.add(config)
    config.adaptive_ads_enabled = config.dynamic_capacity_enabled = config.auto_ads_enabled = True
    await test_db.commit()
    original = log.qualification_context_json
    preview = await adopt_established(test_db, member.id)
    assert preview.get("eligible"), preview
    assert (await adopt_established(test_db, member.id, apply=True))["applied"]
    await test_db.commit()
    state = await FrequencyService(test_db).state(
        group.group_id, json.loads(log.qualification_context_json)
    )
    assert state.mature and state.quota == 2
    assert log.qualification_context_json == original
    assert state.daily_review_checked_at == log.survived_twenty_four_hour_at
    assert (await adopt_established(test_db, member.id, apply=True))["reason"] == "already_mature"
    assert len(list((await test_db.scalars(select(GroupAdFrequencyEvent))).all())) == 1


@pytest.mark.parametrize("change", ["negative", "rejoined", "unknown", "paused"])
async def test_maturity_adoption_preserves_negative_scope_and_unknown_guards(test_db, change):
    account, group, member, _, log = await proven(test_db)
    await restore_own_authorization(test_db, member.id, apply=True)
    config = await test_db.scalar(
        select(AccountOperationConfig).where(AccountOperationConfig.account_id == account.id)
    )
    if config is None:
        config = AccountOperationConfig(account_id=account.id, enabled=True)
        test_db.add(config)
    config.adaptive_ads_enabled = config.dynamic_capacity_enabled = config.auto_ads_enabled = True
    if change == "negative":
        log.survival_status = "deleted"
    if change == "rejoined":
        member.joined_at = datetime.utcnow()
    if change == "unknown":
        log.status = "unknown"
    if change == "paused":
        config.auto_ads_enabled = False
    await test_db.commit()
    assert not (await adopt_established(test_db, member.id, apply=True)).get("eligible")


async def test_mature_send_and_legacy_jobs_have_only_daily_review(test_db):
    account, _, campaign, frequency, state = await setup(test_db, 4, True)
    state.epoch_started_at = NOW
    daily = DailyFrequencyService(test_db)
    first = await delivery(test_db, account, campaign, state, NOW, status="pending")
    first.survival_stage = "two_minute"
    latest = await delivery(
        test_db, account, campaign, state, NOW + timedelta(hours=6), status="pending"
    )
    await daily.note_sent(latest)
    assert latest.survival_stage == "daily" and latest.survival_check_due_at is None
    await retire_mature_checkpoints(test_db, NOW + timedelta(hours=7))
    await test_db.commit()
    assert first.survival_stage == "daily"
    assert latest.survived_twenty_four_hour_at is None
    plan = await daily.plan(state)
    assert plan.log.id == latest.id and plan.due_at == NOW + timedelta(days=1)
    assert await daily.claim(TARGET, NOW + timedelta(hours=23)) is None
    claim = await daily.claim(TARGET, NOW + timedelta(days=1))
    assert claim.log_id == latest.id
    await daily.finish(claim, facts(latest), None, NOW + timedelta(days=1))
    assert state.quota == 8
    assert latest.survived_twenty_four_hour_at is None  # only 18h old, no fabricated 24h proof


async def test_grace_send_is_retained_in_next_daily_cycle(test_db):
    account, _, campaign, frequency, state = await setup(test_db, 4, True)
    state.epoch_started_at = NOW
    daily = DailyFrequencyService(test_db)
    first = await delivery(test_db, account, campaign, state, NOW, status="pending")
    await daily.note_sent(first)
    await test_db.commit()
    state.daily_review_error = "telegram_read_budget"
    later = NOW + timedelta(days=1, hours=1)
    second = await delivery(test_db, account, campaign, state, later, status="pending")
    await daily.note_sent(second)
    await test_db.commit()
    claim = await daily.claim(TARGET, later)
    assert claim.log_id == first.id
    assert await daily.finish(claim, facts(first), None, later) == "daily_survived"
    await test_db.commit()
    assert (await daily.plan(state)).log.id == second.id


async def test_borrowing_preserves_other_protected_lane_and_total(monkeypatch, test_db):
    # Ordinary work already consumed the entire 50%; only minimum guarantees remain.
    mock_usage(monkeypatch, {"hour": (120, 900), "day": (1200, 9000)})
    current = await rpc.snapshot(test_db, 2, datetime.utcnow())
    assert current["lanes"]["ad"]["remaining"] == 84
    assert current["lanes"]["survival"]["remaining"] == 31
    # Advertisement borrowing consumes ordinary capacity, never survival's minimum.
    mock_usage(monkeypatch, {"hour": (204, 900), "ad_hour": (204, 900)})
    with pytest.raises(rpc.RpcDeferred):
        await rpc.check_read_ready(test_db, 2, purpose="ad_delivery")
    assert (await rpc.check_read_ready(test_db, 2, purpose="ad_survival_check"))["lanes"][
        "survival"
    ]["remaining"] == 31


async def test_stalled_review_credit_does_not_hide_replenishment(test_db):
    from app.modules.acquisition.ad_output_plan import expansion_plan
    from app.modules.acquisition.capacity import inventory_snapshot
    from tests.unit.test_qualification_service import setup as member_setup

    account, _, _, row = await member_setup(test_db, decision="technical_wait")
    row.reason = "telegram_read_budget"
    row.created_at = datetime.utcnow() - timedelta(days=3)
    row.next_retry_at = datetime.utcnow() + timedelta(hours=1)
    await test_db.commit()
    stock = await inventory_snapshot(test_db, account.id)
    assert stock["probe_active_backlog"] == stock["probe_stalled_backlog"] == 1
    assert stock["probe_review_credit"] == 0
    plan = expansion_plan(
        20, stock, ad_due=0, survival_due=0, unresolved=0, unavailable_reason=None, material_count=1
    )
    assert plan["group_deficit"] == 30 and plan["join_blocker"] is None


async def test_missing_observation_cannot_be_downgraded_to_local_grace(test_db):
    from app.modules.acquisition.mature_survival import may_continue

    account, _, campaign, _, state = await setup(test_db, 4, True)
    state.epoch_started_at = NOW
    daily = DailyFrequencyService(test_db)
    log = await delivery(test_db, account, campaign, state, NOW, status="pending")
    await daily.note_sent(log)
    await test_db.commit()
    due = NOW + timedelta(days=1)
    claim = await daily.claim(TARGET, due)
    missing = {**facts(log), "exists": False}
    await daily.finish(claim, missing, "message_missing", due)
    await test_db.commit()
    later = due + timedelta(minutes=3)
    claim = await daily.claim(TARGET, later)
    assert claim and not may_continue(state, due, later, log)
    await daily.finish(claim, {}, "telegram_read_budget", later)
    assert state.daily_review_error == "message_missing"
    assert not may_continue(state, due, later, log)


async def test_old_random_success_timer_is_clipped_but_interval_retained(test_db, monkeypatch):
    from datetime import UTC
    from unittest.mock import AsyncMock

    from app.modules.acquisition import automation as module

    account, _, campaign, _, state = await setup(test_db, 4, True)
    await delivery(test_db, account, campaign, state, NOW)
    await test_db.commit()
    service = module.AcquisitionAutomationService(test_db, account_pool=Obj())
    monkeypatch.setattr(
        module,
        "get_ad_delivery_throttle_settings",
        AsyncMock(
            return_value={
                "enabled": True,
                "growth_min_interval_seconds": 600,
                "growth_max_interval_seconds": 1800,
            }
        ),
    )
    monkeypatch.setattr(
        service,
        "_get_ad_delivery_cooldown_until",
        AsyncMock(return_value=(NOW + timedelta(minutes=30)).replace(tzinfo=UTC).timestamp()),
    )
    config = Obj(account_id=account.id, message_interval_seconds=600)
    assert (await service._ad_account_throttle_readiness(config, NOW + timedelta(minutes=9))).reason
    assert (
        await service._ad_account_throttle_readiness(config, NOW + timedelta(minutes=10))
    ).reason is None
    config.message_interval_seconds = 1200
    assert (
        await service._ad_account_throttle_readiness(config, NOW + timedelta(minutes=10))
    ).reason
