"""Acceptance for configured limits, failed-send settlement and review progress."""
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest
from sqlalchemy import select, text

from app.core.account.models import AccountOperationConfig, AccountStatus
from app.core.account.outbound_budget import account_capacity_limits
from app.core.operating_time import operating_date
from app.core.settings_models import SystemSetting
from app.modules.acquisition.growth_controls import (
    control_snapshot,
    pause_repeated_permission_failures,
)
from app.modules.acquisition.models import AdDeliveryLog, GroupAdFrequency
from app.modules.acquisition.simple_limits import PREFIX, Deferred, revert_ad, take_ad, take_join
from tests.unit.test_simplified_growth import setup


@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.mark.parametrize("configured,expected", [(0, 0), (1, 1), (30, 30), (70, 70), (200, 144)])
def test_ad_limit_honors_zero_and_configured_ceiling(configured, expected):
    account = NS(is_active=True, status="online", risk_level="normal", risk_pause_until=None,
                 risk_recovery_until=None)
    config = NS(enabled=True, max_ads_per_day=configured, max_messages_per_day=150,
                adaptive_ads_enabled=True, message_interval_seconds=600, max_groups_per_day=20,
                join_interval_min_seconds=7200)
    limits = account_capacity_limits(account, config, datetime.utcnow())
    assert limits["ad"] == expected
    assert limits["join"] == 20
    assert limits["join_interval_seconds"] == 7200
    assert limits["total"] <= 150


def test_recovering_account_keeps_tighter_limits():
    account = NS(is_active=True, status="online", risk_level="watch", risk_pause_until=None,
                 risk_recovery_until=None)
    config = NS(enabled=True, max_ads_per_day=70, max_messages_per_day=150,
                adaptive_ads_enabled=True, message_interval_seconds=600, max_groups_per_day=20)
    limits = account_capacity_limits(account, config, datetime.utcnow())
    assert limits["ad"] == limits["join"] == 10
    assert limits["ad_interval_seconds"] == 1800
    assert limits["join_interval_seconds"] == 7200


async def test_final_join_allocator_uses_configured_limit_and_interval(redis):
    now = datetime.now(UTC).timestamp()
    await take_join(redis, 7, cap=20, durable_used=19, interval=7200)
    assert int(await redis.hget(PREFIX + "join:7:count", "used")) == 20
    assert float(await redis.get(PREFIX + "join:7:due")) >= now + 7198
    await redis.delete(PREFIX + "join:7:due")
    with pytest.raises(Deferred) as blocked:
        await take_join(redis, 7, cap=20, durable_used=20)
    assert blocked.value.reason == 'account_daily_cap'


async def test_join_cooldown_is_not_reported_as_redis_failure(redis):
    await take_join(redis, 7, cap=20)
    with pytest.raises(Deferred) as blocked:
        await take_join(redis, 7, cap=20)
    assert blocked.value.reason == 'account_cooldown'
    assert blocked.value.retry_after_seconds > 2800


async def test_join_allocator_never_exceeds_system_ceiling(redis):
    with pytest.raises(Deferred) as blocked:
        await take_join(redis, 7, cap=100, durable_used=30)
    assert blocked.value.reason == 'account_daily_cap'


async def test_failure_settlement_is_idempotent_and_keeps_longer_cooldown(redis):
    admitted = datetime.utcnow()
    await take_ad(redis, 7, 111, cap=10, account_interval=1800, group_interval=10800,
                  attempt_id="one")
    due = await redis.get(PREFIX + "ads:7:due")
    await revert_ad(redis, 7, cooldown_seconds=600, attempt_id="one", admitted_at=admitted)
    # A different successful operation must not be refunded by replaying this result.
    await redis.hset(PREFIX + "ads:7:count", "used", 1)
    shared = f"risk:account:7:daily:outbound_message:{operating_date(admitted):%Y%m%d}"
    await redis.set(shared, 1)
    await revert_ad(redis, 7, cooldown_seconds=600, attempt_id="one", admitted_at=admitted)
    assert await redis.hget(PREFIX + "ads:7:count", "used") == "1"
    assert await redis.get(shared) == "1"
    assert float(await redis.get(PREFIX + "ads:7:due")) >= float(due)


async def test_previous_day_failure_cannot_decrement_new_day(redis):
    now = datetime.utcnow()
    today = int((now.replace(tzinfo=UTC).timestamp() + 28800) // 86400)
    await redis.hset(PREFIX + "ads:7:count", mapping={"day": today, "used": 3})
    await redis.hset(PREFIX + "admission:7:yesterday", mapping={"state": "admitted", "day": today - 1})
    old = now - timedelta(days=1)
    old_shared = f"risk:account:7:daily:outbound_message:{operating_date(old):%Y%m%d}"
    new_shared = f"risk:account:7:daily:outbound_message:{operating_date(now):%Y%m%d}"
    await redis.set(old_shared, 2)
    await redis.set(new_shared, 3)
    await revert_ad(redis, 7, attempt_id="yesterday", admitted_at=old, cooldown_seconds=600)
    assert await redis.hget(PREFIX + "ads:7:count", "used") == "3"
    assert await redis.get(old_shared) == "1"
    assert await redis.get(new_shared) == "3"


async def test_total_cap_keeps_durable_ad_usage_when_redis_was_lost(redis):
    with pytest.raises(Deferred, match="account_total_cap"):
        await take_ad(redis, 7, 111, cap=70, account_interval=600, group_interval=10800,
                      total_cap=5, durable_ads=3, durable_used=2)
    assert await redis.get(PREFIX + "ads:7:due") is None


async def test_unknown_send_remains_counted_after_redis_restart(test_db, monkeypatch, redis):
    svc, account, member, campaign, creative, client = await setup(test_db, monkeypatch, redis)
    config = await test_db.scalar(select(AccountOperationConfig).where(AccountOperationConfig.account_id == account.id))
    config.max_ads_per_day = 1
    test_db.add(AdDeliveryLog(account_id=account.id, group_id=member.group_id,
                            telegram_group_id=member.telegram_group_id, ad_campaign_id=campaign.id,
                            status="unknown", created_at=datetime.utcnow()))
    await test_db.commit()
    with pytest.raises(Deferred, match="account_daily_cap"):
        await svc.send(account.id, member.id, campaign.id, creative.id, member.telegram_group_id)
    client.send_message.assert_not_awaited()


async def test_chat_restriction_refunds_once_and_does_not_freeze_account(test_db, monkeypatch, redis):
    svc, account, member, campaign, creative, client = await setup(test_db, monkeypatch, redis)
    class ChatRestrictedError(Exception):
        pass
    client.send_message.side_effect = ChatRestrictedError("restricted from sending messages in this chat")
    outcome = await svc.send(account.id, member.id, campaign.id, creative.id, member.telegram_group_id)
    assert outcome["outcome"] == "permission_changed"
    assert account.status == AccountStatus.ONLINE
    assert account.risk_pause_until is None
    assert member.status == "joined" and member.review_status == "approved" and member.ad_failure_streak == 1
    assert await redis.hget(PREFIX + f"ads:{account.id}:count", "used") == "0"
    assert await redis.ttl(PREFIX + f"ads:{account.id}:due") >= 599
    client.send_message.assert_awaited_once()


async def test_cross_group_failures_pause_and_enqueue_diagnosis(test_db, monkeypatch, redis):
    _svc, account, member, campaign, _creative, _client = await setup(test_db, monkeypatch, redis)
    enqueue = AsyncMock()
    monkeypatch.setattr("app.modules.account_spam.service.enqueue_automatic_spam_check", enqueue)
    now = datetime.utcnow()
    for target in (111, 222, 333):
        test_db.add(AdDeliveryLog(account_id=account.id, telegram_group_id=target,
                                ad_campaign_id=campaign.id, status="permission_changed", created_at=now))
    await test_db.commit()
    await pause_repeated_permission_failures(test_db, account.id, now)
    config = await test_db.scalar(select(AccountOperationConfig).where(AccountOperationConfig.account_id == account.id))
    assert config.auto_ads_enabled and config.auto_join_enabled and config.enabled
    assert account.status == AccountStatus.ONLINE
    enqueue.assert_awaited_once()
    await pause_repeated_permission_failures(test_db, account.id, now)
    enqueue.assert_awaited_once()


async def test_repeated_errors_in_one_group_do_not_claim_account_restriction(test_db, monkeypatch, redis):
    _svc, account, member, campaign, _creative, _client = await setup(test_db, monkeypatch, redis)
    for _ in range(4):
        test_db.add(AdDeliveryLog(account_id=account.id, telegram_group_id=111,
                                ad_campaign_id=campaign.id, status="permission_changed"))
    await test_db.commit()
    await pause_repeated_permission_failures(test_db, account.id, datetime.utcnow())
    config = await test_db.scalar(select(AccountOperationConfig).where(AccountOperationConfig.account_id == account.id))
    assert config.auto_ads_enabled


async def test_gap_revokes_permission_but_duplicate_join_does_not(test_db, monkeypatch, redis):
    from app.modules.acquisition.simple_runtime import apply_fact
    _svc, account, member, *_ = await setup(test_db, monkeypatch, redis)
    fact = {"kind": "membership", "peer": member.telegram_group_id,
            "at": datetime.now(UTC).timestamp(), "member": True, "blocked": False}
    await apply_fact(test_db, account.id, fact)
    assert member.review_status == "approved" and member.can_send_text is True
    await apply_fact(test_db, account.id, {**fact, "kind": "gap"})
    assert member.review_status == "approved" and member.can_send_text is True



async def test_runtime_exposes_effective_limits_and_durable_usage(test_db, monkeypatch, redis):
    _svc, account, member, campaign, creative, client = await setup(test_db, monkeypatch, redis)
    config = await test_db.scalar(select(AccountOperationConfig).where(AccountOperationConfig.account_id == account.id))
    config.max_ads_per_day, config.max_messages_per_day = 70, 150
    config.message_interval_seconds = 600
    config.adaptive_ads_enabled = True
    await test_db.commit()
    snapshot = await control_snapshot(test_db, account.id, datetime.utcnow())
    assert snapshot["effective"]["ad_daily"] == 70
    assert snapshot["configured"]["ad_daily"] == 70
    assert snapshot["execution"]["ads"]["used"] == 0
    assert snapshot["execution"]["ads"]["remaining"] == 70
    assert snapshot["reset_at"]["ads"].hour == 16
    assert snapshot["reset_at"]["join"].hour == 0
    client.send_message.assert_not_awaited()


async def test_observation_limit_survives_recovery_expiry_without_changing_business_config(test_db, monkeypatch, redis):
    from app.core.account.outbound_budget import effective_capacity_limits
    _svc, account, _member, *_ = await setup(test_db, monkeypatch, redis)
    config = await test_db.scalar(select(AccountOperationConfig).where(AccountOperationConfig.account_id == account.id))
    config.max_ads_per_day, config.max_groups_per_day = 70, 20
    config.message_interval_seconds = 600
    config.adaptive_ads_enabled = True
    account.risk_level, account.risk_recovery_until = "normal", None
    test_db.add(SystemSetting(key=f"growth.canary.account.{account.id}", value=
        '{"enabled":true,"ad_daily":10,"join_daily":10,"ad_interval_seconds":1800,"join_interval_seconds":7200}'))
    await test_db.commit()
    limits = await effective_capacity_limits(test_db, account, config, datetime.utcnow())
    assert limits["ad"] == limits["join"] == 10
    assert limits["ad_interval_seconds"] == 1800
    assert config.max_ads_per_day == 70 and config.max_groups_per_day == 20



async def test_authenticated_runtime_api_releases_only_structured_reads(test_db, client, monkeypatch, redis):
    from app.core.security import create_access_token
    await setup(test_db, monkeypatch, redis)
    await test_db.execute(text("create table admin_user (id integer primary key, username text, role text, email text, avatar text, created_at timestamp, is_active boolean)"))
    await test_db.execute(text("insert into admin_user(id,username,role,is_active) values(1,'test-admin','admin',true)"))
    await test_db.commit()
    monkeypatch.setattr("app.core.account.rpc_governor.snapshot", AsyncMock(return_value={"reason": None}))
    token = create_access_token({"sub": "1"})
    response = await client.get('/api/automation/group-reviews/runtime?account_id=1',
                                headers={"Authorization": "Bearer " + token})
    assert response.status_code == 200
    assert response.json()["data"]["controls"]["available"] is True


@pytest.mark.parametrize('matched', [False, True, 'database_error'])
async def test_sdk_deletion_callback_filters_noise_but_keeps_facts_on_error(monkeypatch, matched):
    from app.workers import telegram_worker as workers
    from app.workers.telegram_worker import TelegramWorker
    @asynccontextmanager
    async def session():
        yield object()
    monkeypatch.setattr(workers, 'get_db_session', session)
    relevant = AsyncMock(return_value=matched) if matched != 'database_error' else AsyncMock(side_effect=RuntimeError('db unavailable'))
    enqueue = AsyncMock()
    monkeypatch.setattr('app.modules.acquisition.adaptive_frequency.relevant_ad_deletion', relevant)
    monkeypatch.setattr('app.core.account.event_inbox.enqueue', enqueue)
    client = NS(add_event_handler=MagicMock())
    worker = NS(_growth_ingress_policy={})
    TelegramWorker._attach_growth_event_handlers(worker, NS(client=client, account_id=2, session_name='test'))
    callback = next(args[0] for args, _kwargs in client.add_event_handler.call_args_list if args[0].__name__ == 'handle_ad_deleted')
    event = NS(chat_id=-100123, deleted_ids=[42, 777])
    await callback(event)
    relevant.assert_awaited_once()
    assert enqueue.await_count == (0 if matched is False else 1)
    if matched is False:
        assert worker._growth_irrelevant_deletions == 1
    else:
        assert enqueue.await_args.args[-1] is event
