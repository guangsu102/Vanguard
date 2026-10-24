"""Account-scoped daily jobs must discover first-cycle real send receipts."""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.core.account.models import AccountOperationConfig, TelegramAccount
from app.core.account.rpc_governor import RpcDeferred
from app.modules.acquisition import automation, daily_frequency
from app.modules.acquisition.adaptive_frequency import DAY
from app.modules.acquisition.automation import AcquisitionAutomationService
from app.modules.acquisition.daily_frequency import run_daily_frequency_reviews
from app.modules.acquisition.models import GroupAdFrequencyEvent
from tests.unit.test_adaptive_group_frequency import NOW, TARGET, delivery
from tests.unit.test_daily_group_frequency import facts, start

pytestmark = pytest.mark.asyncio


def service_at(db, monkeypatch, at, log):
    class Frozen(datetime):
        @classmethod
        def utcnow(cls):
            return at

    monkeypatch.setattr(daily_frequency, "datetime", Frozen)
    monkeypatch.setattr(automation, "_now", lambda: at)
    service = AcquisitionAutomationService(db, account_pool=SimpleNamespace())
    service._sync_account_pool = AsyncMock()
    service._read_survival_facts = AsyncMock(return_value=facts(log))
    return service


async def another_account(db, original):
    account = TelegramAccount(
        identifier="daily-other",
        session_name="daily-other",
        account_type=original.account_type,
        status=original.status,
        is_active=True,
    )
    db.add(account)
    await db.flush()
    db.add(AccountOperationConfig(
        account_id=account.id,
        enabled=True,
        auto_ads_enabled=True,
        adaptive_ads_enabled=True,
        dynamic_capacity_enabled=True,
    ))
    await db.commit()
    return account


@pytest.mark.parametrize("raw_alias", [False, True])
async def test_account_worker_reviews_first_receipt_without_history_events(test_db, monkeypatch, raw_alias):
    account, campaign, frequency, state, daily = await start(test_db)
    log = await delivery(test_db, account, campaign, state, NOW, status="pending")
    if raw_alias:
        log.telegram_group_id = 123  # Context retains the authoritative marked channel identity.
    await daily.note_sent(log)
    await test_db.commit()
    assert await test_db.scalar(select(func.count()).select_from(GroupAdFrequencyEvent)) == 0
    assert log.survival_status == "not_required" and log.survival_stage == "daily"
    service = service_at(test_db, monkeypatch, NOW + DAY, log)
    result = await service.check_ad_survival(account_id=account.id, limit=1)
    assert result["daily_processed"] == 1 and result["daily_survived"] == 1
    assert state.mature and state.quota == 2 and state.epoch == 2
    assert (await frequency.readiness(TARGET, NOW + DAY))[0].reason is None
    service._read_survival_facts.assert_awaited_once()
    assert service._read_survival_facts.await_args.args[0].id == log.id
    assert await test_db.scalar(select(func.count()).select_from(GroupAdFrequencyEvent)) == 1
    repeated = await service.check_ad_survival(account_id=account.id, limit=1)
    assert repeated["daily_processed"] == 0
    service._read_survival_facts.assert_awaited_once()
    assert state.quota == 2


async def test_first_account_job_waits_until_actual_24h_due(test_db, monkeypatch):
    account, campaign, _, state, daily = await start(test_db)
    log = await delivery(test_db, account, campaign, state, NOW, status="pending")
    await daily.note_sent(log)
    await test_db.commit()
    service = service_at(test_db, monkeypatch, NOW + DAY - timedelta(seconds=1), log)
    result = await service.check_ad_survival(account_id=account.id, limit=1)
    assert result["daily_processed"] == 0 and state.quota == 1 and not state.mature
    service._read_survival_facts.assert_not_awaited()


async def test_shared_group_is_reviewed_by_last_sender_only(test_db, monkeypatch):
    account, campaign, _, state, daily = await start(test_db, quota=2, mature=True)
    other = await another_account(test_db, account)
    first = await delivery(test_db, account, campaign, state, NOW, status="pending")
    last = await delivery(test_db, other, campaign, state, NOW + timedelta(hours=12), status="pending")
    await daily.note_sent(first)
    await daily.note_sent(last)
    await test_db.commit()
    service = service_at(test_db, monkeypatch, NOW + DAY, last)
    foreign = await run_daily_frequency_reviews(service, account_id=account.id, limit=1)
    assert foreign["daily_processed"] == 0 and state.daily_review_token is None
    service._read_survival_facts.assert_not_awaited()
    owned = await run_daily_frequency_reviews(service, account_id=other.id, limit=1)
    assert owned["daily_processed"] == 1 and owned["daily_survived"] == 1
    assert service._read_survival_facts.await_args.args[0].id == last.id
    assert state.quota == 4


async def test_same_raw_chat_id_does_not_select_channel_cycle(test_db, monkeypatch):
    account, campaign, _, state, _ = await start(test_db)
    log = await delivery(test_db, account, campaign, state, NOW, status="pending")
    log.telegram_group_id = -123
    context = json.loads(log.qualification_context_json)
    context["telegram_group_id"] = -123
    log.qualification_context_json = json.dumps(context)
    state.daily_review_due_at = NOW + DAY
    await test_db.commit()
    service = service_at(test_db, monkeypatch, NOW + DAY, log)
    result = await run_daily_frequency_reviews(service, account_id=account.id, limit=1)
    assert result["daily_processed"] == 0
    assert state.daily_review_error is None and state.quota == 1
    service._read_survival_facts.assert_not_awaited()


async def test_first_cycle_read_budget_retry_never_invents_success(test_db, monkeypatch):
    account, campaign, _, state, daily = await start(test_db)
    log = await delivery(test_db, account, campaign, state, NOW, status="pending")
    await daily.note_sent(log)
    await test_db.commit()
    service = service_at(test_db, monkeypatch, NOW + DAY, log)
    service._read_survival_facts.side_effect = RpcDeferred("telegram_read_budget", retry_after_seconds=900)
    result = await run_daily_frequency_reviews(service, account_id=account.id, limit=1)
    assert result["daily_processed"] == 1 and result["daily_review_retry"] == 1
    assert state.daily_review_error == "telegram_read_budget"
    assert state.daily_review_token is None and state.quota == 1 and not state.mature
    assert await test_db.scalar(select(func.count()).select_from(GroupAdFrequencyEvent)) == 0
    service._read_survival_facts.side_effect = None
    result = await run_daily_frequency_reviews(service, account_id=account.id, limit=1)
    assert result["daily_survived"] == 1 and state.quota == 2


async def test_missing_first_cycle_message_keeps_sends_blocked(test_db, monkeypatch):
    account, campaign, frequency, state, daily = await start(test_db)
    log = await delivery(test_db, account, campaign, state, NOW, status="pending")
    await daily.note_sent(log)
    await test_db.commit()
    service = service_at(test_db, monkeypatch, NOW + DAY, log)
    service._read_survival_facts.return_value = facts(log, exists=False)
    result = await run_daily_frequency_reviews(service, account_id=account.id, limit=1)
    assert result["daily_processed"] == 1 and result["daily_review_retry"] == 1
    assert state.quota == 1 and not state.mature
    assert (await frequency.readiness(TARGET, NOW + DAY))[0].reason == "frequency_daily_review_unknown"
    assert await test_db.scalar(select(func.count()).select_from(GroupAdFrequencyEvent)) == 0


async def test_foreign_last_senders_cannot_fill_batch_and_starve_owned_cycle(test_db, monkeypatch):
    account, campaign, frequency, _, daily = await start(test_db, quota=2, mature=True)
    other = await another_account(test_db, account)
    owned = None
    for index in range(22):
        target = TARGET - index - 1
        state = await frequency.state(target, create=True, lock=True, now=NOW)
        state.quota, state.mature = 2, True
        for sender, delta in ((account, timedelta()), (other, timedelta(hours=1))):
            if index == 21 and sender is other:
                continue
            log = await delivery(test_db, sender, campaign, state, NOW + delta, status="pending")
            log.telegram_group_id = target
            context = json.loads(log.qualification_context_json)
            context["telegram_group_id"] = target
            log.qualification_context_json = json.dumps(context)
            await daily.note_sent(log)
            if index == 21:
                owned = log
        # Make the last group's due time later than the 21 foreign-owned cycles.
        state.daily_review_due_at = NOW + DAY + timedelta(seconds=index)
    await test_db.commit()
    service = service_at(test_db, monkeypatch, NOW + DAY + timedelta(minutes=1), owned)
    result = await run_daily_frequency_reviews(service, account_id=account.id, limit=1)
    assert result["daily_processed"] == 1 and result["daily_survived"] == 1
    service._read_survival_facts.assert_awaited_once()
    assert service._read_survival_facts.await_args.args[0].id == owned.id
