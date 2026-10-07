"""Daily frequency behavior with SQLite and mocked reads; no Telegram operations."""

import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.account.models import AccountOperationConfig, TelegramAccount
from app.modules.acquisition.adaptive_frequency import DAY
from app.modules.acquisition.automation import AcquisitionAutomationService
from app.modules.acquisition.daily_frequency import DailyFrequencyService
from app.modules.acquisition.models import GroupAdFrequencyEvent
from tests.unit.test_adaptive_group_frequency import NOW, TARGET, delivery, setup

pytestmark = pytest.mark.asyncio


def facts(log, *, exists=True):
    return {
        "errors": [],
        "group_accessible": True,
        "account_readable": True,
        "member": True,
        "can_send": True,
        "exists": exists,
        "ttl_period": 0,
        "is_own_message": True,
        "message_created_at": log.sent_at.isoformat(),
    }


async def start(db, quota=1, mature=False):
    account, config, campaign, frequency, state = await setup(db, quota, mature)
    state.epoch_started_at = NOW
    await db.commit()
    return account, campaign, frequency, state, DailyFrequencyService(db)


async def check(db, daily, log, at, **kwargs):
    claim = await daily.claim(TARGET, at)
    assert claim is not None and claim.log_id == log.id
    await db.commit()
    result = await daily.finish(claim, facts(log, **kwargs), None, at)
    await db.commit()
    return claim, result


async def test_first_ad_alone_promotes_at_24h_without_three_checkpoints(test_db):
    a, campaign, frequency, state, daily = await start(test_db)
    log = await delivery(test_db, a, campaign, state, NOW, status="pending")
    log.survived_two_minute_at = log.survived_one_hour_at = None
    await daily.note_sent(log)
    await test_db.commit()
    assert await daily.claim(TARGET, NOW + DAY - timedelta(seconds=1)) is None
    _, result = await check(test_db, daily, log, NOW + DAY)
    assert result == "daily_survived" and state.quota == 2 and state.mature
    assert log.survived_twenty_four_hour_at == NOW + DAY and log.survival_status == "survived"
    assert log.survived_two_minute_at is None and log.survived_one_hour_at is None
    assert (await frequency.readiness(TARGET, NOW + DAY))[0].reason is None


async def test_daily_doubling_reaches_30_in_six_cycles(test_db):
    a, campaign, frequency, state, daily = await start(test_db)
    for day, quota in enumerate((1, 2, 4, 8, 16)):
        assert state.quota == quota
        at = NOW + day * DAY
        for i in range(quota):
            last = await delivery(
                test_db,
                a,
                campaign,
                state,
                at + timedelta(seconds=i * 86400 / quota),
                status="pending",
            )
            await daily.note_sent(last)
        await test_db.commit()
        _, result = await check(test_db, daily, last, at + DAY)
        assert result == "daily_survived"
    assert state.quota == 30 and state.epoch == 6
    events = list((await test_db.scalars(select(GroupAdFrequencyEvent))).all())
    assert [(event.old_quota, event.new_quota) for event in events] == [
        (1, 2),
        (2, 4),
        (4, 8),
        (8, 16),
        (16, 30),
    ]


async def test_last_message_not_first_controls_daily_review_across_accounts(test_db):
    a, campaign, frequency, state, daily = await start(test_db, 2, True)
    first = await delivery(test_db, a, campaign, state, NOW, status="pending")
    last = await delivery(test_db, a, campaign, state, NOW + timedelta(hours=12), status="pending")
    # The other account shares the group quota. Its message remains the last-message target.
    other = TelegramAccount(
        identifier="daily-second",
        session_name="daily-second",
        account_type=a.account_type,
        status=a.status,
        is_active=True,
    )
    test_db.add(other)
    await test_db.flush()
    test_db.add(
        AccountOperationConfig(
            account_id=other.id,
            enabled=True,
            auto_ads_enabled=True,
            adaptive_ads_enabled=True,
            dynamic_capacity_enabled=True,
        )
    )
    last.account_id = other.id
    state.epoch_started_at = NOW
    await test_db.commit()
    plan = await daily.plan(state)
    assert plan.log.id == last.id and plan.log.id != first.id
    assert plan.due_at == NOW + DAY
    _, result = await check(test_db, daily, last, NOW + DAY)
    assert result == "daily_survived" and state.quota == 4
    assert last.sent_at + DAY > NOW + DAY and last.survived_twenty_four_hour_at is None


async def test_claim_and_delayed_callback_cannot_double_promote(test_db):
    a, campaign, frequency, state, daily = await start(test_db)
    log = await delivery(test_db, a, campaign, state, NOW)
    await test_db.commit()
    claim = await daily.claim(TARGET, NOW + DAY)
    await test_db.commit()
    assert await daily.claim(TARGET, NOW + DAY) is None
    assert await daily.finish(claim, facts(log), None, NOW + DAY) == "daily_survived"
    await test_db.commit()
    assert await daily.finish(claim, facts(log), None, NOW + DAY) == "daily_stale_claim"
    await frequency.observe(log, "survived", NOW + DAY)
    await test_db.commit()
    assert state.quota == 2 and state.epoch == 2
    assert await daily.claim(TARGET, NOW + 2 * DAY) is None


async def test_local_wait_holds_quota_but_allows_bounded_mature_continuation(test_db):
    a, campaign, frequency, state, daily = await start(test_db, 4, True)
    log = await delivery(test_db, a, campaign, state, NOW, status="pending")
    await test_db.commit()
    claim = await daily.claim(TARGET, NOW + DAY)
    await test_db.commit()
    assert (
        await daily.finish(claim, {}, "telegram_read_budget", NOW + DAY, retry_seconds=900)
        == "daily_review_retry"
    )
    await test_db.commit()
    assert state.quota == 4 and state.epoch == 1
    assert state.daily_review_retry_at == NOW + DAY + timedelta(minutes=15)
    # The normal checkpoint is not the blocker in this reproduction.
    log.survival_check_due_at = NOW + 2 * DAY
    await test_db.commit()
    ready, _ = await frequency.readiness(TARGET, NOW + DAY)
    assert ready.reason is None
    assert (await frequency.readiness(TARGET, NOW + 2 * DAY))[0].reason == "frequency_daily_review_unknown"
    assert await daily.claim(TARGET, NOW + DAY + timedelta(minutes=2)) is not None


@pytest.mark.parametrize("quota, expected", [(30, 15), (16, 8), (4, 2), (2, 1), (1, 1)])
async def test_confirmed_missing_last_message_halves_once(test_db, quota, expected):
    a, campaign, frequency, state, daily = await start(test_db, quota, True)
    log = await delivery(test_db, a, campaign, state, NOW, status="pending")
    await test_db.commit()
    claim = await daily.claim(TARGET, NOW + DAY)
    await test_db.commit()
    assert (
        await daily.finish(
            claim, facts(log, exists=False), "message_missing_cause_unconfirmed", NOW + DAY
        )
        == "daily_review_retry"
    )
    await test_db.commit()
    assert state.quota == quota
    again = NOW + DAY + timedelta(minutes=2)
    claim = await daily.claim(TARGET, again)
    await test_db.commit()
    assert (
        await daily.finish(
            claim, facts(log, exists=False), "message_missing_cause_unconfirmed", again
        )
        == "daily_deleted"
    )
    await test_db.commit()
    assert state.quota == expected and state.epoch == 2
    assert await daily.finish(claim, facts(log, exists=False), None, again) == "daily_stale_claim"
    if quota == 1:
        assert state.status == "exit_pending" and state.reason == "frequency_deleted_at_minimum"
        assert (await frequency.summary(TARGET, again))["daily_review_status"] == "blocked"
    else:
        assert state.status == "active"


async def test_rule_cap_and_top_quota_still_receive_daily_reviews(test_db):
    a, campaign, frequency, state, daily = await start(test_db, 16, True)
    log = await delivery(test_db, a, campaign, state, NOW, status="pending")
    context = json.loads(log.qualification_context_json)
    context["frequency_rule_quota"] = 20
    log.qualification_context_json = json.dumps(context)
    await test_db.commit()
    await check(test_db, daily, log, NOW + DAY)
    assert state.quota == 20
    state.quota = 30
    last = await delivery(test_db, a, campaign, state, NOW + DAY, status="pending")
    await daily.note_sent(last)
    await test_db.commit()
    await check(test_db, daily, last, NOW + 2 * DAY)
    assert state.quota == 30 and state.epoch == 3


async def test_unknown_send_prevents_review_even_when_last_known_message_survived(test_db):
    a, campaign, frequency, state, daily = await start(test_db)
    await delivery(test_db, a, campaign, state, NOW)
    unknown = await delivery(test_db, a, campaign, state, NOW + timedelta(hours=1))
    unknown.status = "reconciliation_required"
    unknown.sent_at = None
    await test_db.commit()
    assert await daily.claim(TARGET, NOW + DAY) is None
    assert (
        state.quota == 1
        and state.daily_review_error == "qualification_delivery_reconciliation_required"
    )


async def test_expired_claim_replacement_fences_old_worker(test_db):
    a, campaign, frequency, state, daily = await start(test_db)
    log = await delivery(test_db, a, campaign, state, NOW)
    await test_db.commit()
    old = await daily.claim(TARGET, NOW + DAY)
    await test_db.commit()
    later = NOW + DAY + timedelta(minutes=4)
    new = await daily.claim(TARGET, later)
    await test_db.commit()
    assert new.token != old.token
    assert await daily.finish(old, facts(log), None, later) == "daily_stale_claim"
    assert await daily.finish(new, facts(log), None, later) == "daily_survived"


async def test_disabled_account_during_read_cannot_promote(test_db):
    a, campaign, frequency, state, daily = await start(test_db)
    log = await delivery(test_db, a, campaign, state, NOW)
    await test_db.commit()
    claim = await daily.claim(TARGET, NOW + DAY)
    await test_db.commit()
    config = await test_db.scalar(
        select(AccountOperationConfig).where(AccountOperationConfig.account_id == a.id)
    )
    config.auto_ads_enabled = False
    await test_db.commit()
    assert await daily.finish(claim, facts(log), None, NOW + DAY) == "daily_review_retry"
    assert state.quota == 1 and state.epoch == 1 and state.daily_review_token is None


async def test_wrong_sender_or_timestamp_is_unknown(test_db):
    a, campaign, frequency, state, daily = await start(test_db)
    log = await delivery(test_db, a, campaign, state, NOW)
    await test_db.commit()
    claim = await daily.claim(TARGET, NOW + DAY)
    await test_db.commit()
    observed = facts(log)
    observed["is_own_message"] = False
    assert await daily.finish(claim, observed, None, NOW + DAY) == "daily_review_retry"
    await test_db.commit()
    claim = await daily.claim(TARGET, NOW + DAY + timedelta(minutes=2))
    await test_db.commit()
    observed = facts(log)
    observed["message_created_at"] = (log.sent_at + timedelta(minutes=2)).isoformat()
    assert (
        await daily.finish(claim, observed, None, NOW + DAY + timedelta(minutes=2))
        == "daily_review_retry"
    )
    assert state.quota == 1


async def test_confirmed_automatic_expiry_also_reduces_frequency(test_db):
    a, campaign, frequency, state, daily = await start(test_db, 4, True)
    log = await delivery(test_db, a, campaign, state, NOW)
    await test_db.commit()
    missing = facts(log, exists=False)
    missing["ttl_period"] = 3600
    claim = await daily.claim(TARGET, NOW + DAY)
    await test_db.commit()
    assert (
        await daily.finish(claim, missing, "message_missing_auto_delete_possible", NOW + DAY)
        == "daily_review_retry"
    )
    await test_db.commit()
    later = NOW + DAY + timedelta(minutes=2)
    claim = await daily.claim(TARGET, later)
    await test_db.commit()
    assert (
        await daily.finish(claim, missing, "message_missing_auto_delete_possible", later)
        == "daily_deleted"
    )
    assert state.quota == 2


async def test_legacy_cycle_adopts_latest_message_without_replaying_older_days(test_db):
    a, campaign, frequency, state, daily = await start(test_db)
    for i in range(3):
        last = await delivery(test_db, a, campaign, state, NOW + i * DAY)
        context = json.loads(last.qualification_context_json)
        context["frequency"]["version"] = 1
        last.qualification_context_json = json.dumps(context)
        await test_db.flush()
    plan = await daily.plan(state)
    assert plan.log.id == last.id and plan.due_at == NOW + 3 * DAY
    await test_db.commit()
    await check(test_db, daily, last, NOW + 3 * DAY)
    assert state.quota == 2


async def test_finish_locks_log_before_group_without_nullable_join_lock(test_db, monkeypatch):
    from sqlalchemy.dialects.postgresql import dialect

    a, campaign, frequency, state, daily = await start(test_db)
    log = await delivery(test_db, a, campaign, state, NOW)
    await test_db.commit()
    claim = await daily.claim(TARGET, NOW + DAY)
    await test_db.commit()
    scalar = test_db.scalar
    statements = []

    async def recording(statement, *args, **kwargs):
        statements.append(str(statement.compile(dialect=dialect())))
        return await scalar(statement, *args, **kwargs)

    monkeypatch.setattr(test_db, "scalar", recording)
    await daily.finish(claim, facts(log), None, NOW + DAY)
    assert "FOR UPDATE OF ad_delivery_log" in statements[0]
    assert "group_ad_frequency" in statements[1] and "FOR UPDATE" in statements[1]


async def test_worker_runs_daily_review_before_regular_checks(test_db, monkeypatch):
    a, campaign, frequency, state, daily = await start(test_db)
    state.epoch_started_at = NOW - 10 * DAY
    log = await delivery(test_db, a, campaign, state, NOW - 9 * DAY, status="pending")
    await test_db.commit()
    service = AcquisitionAutomationService(test_db, account_pool=SimpleNamespace())
    service._sync_account_pool = AsyncMock()
    service._read_survival_facts = AsyncMock(return_value=facts(log))
    service._check_one_ad_survival = AsyncMock(return_value="not_due")
    result = await service.check_ad_survival(limit=1)
    assert result["daily_processed"] == 1 and result["daily_survived"] == 1
    assert state.quota == 2
    service._read_survival_facts.assert_awaited_once()
    service._check_one_ad_survival.assert_not_awaited()
