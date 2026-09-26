from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

from app.core.account.models import (
    AccountOperationConfig,
    AccountStatus,
    AccountType,
    TelegramAccount,
)
from app.core.account.risk_guard import AccountRiskAction, AccountRiskGuard, RiskDecision
from app.core.account.telegram_execution import TelegramExecutionError, TelegramExecutionService
from app.core.group.join_review import JOIN_REVIEW_APPROVED
from app.core.group.models import Group, GroupAccountMembership, GroupLevel
from app.core.redis import RedisCache
from app.modules.acquisition.automation import AcquisitionAutomationService
from app.modules.acquisition.models import AccountAdBinding, AdCampaign, GroupAdProfile
from app.modules.acquisition.probe_policy import in_ad_window_at, probe_day_start, write_probe_limit
from app.modules.acquisition.probe_queue import build_probe_queue


class ContentRedis:
    def __init__(self):
        self.values, self.lists, self.ttls = {}, {}, {}

    async def exists(self, key):
        return int(key in self.values)

    async def get(self, key):
        return self.values.get(key)

    async def setex(self, key, ttl, value):
        self.values[key] = value
        self.ttls[key] = ttl

    async def delete(self, key):
        self.values.pop(key, None)

    async def ttl(self, key):
        return self.ttls.get(key, 259200)

    async def lrange(self, key, start, end):
        return self.lists.get(key, [])[start : end + 1]

    async def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)

    async def ltrim(self, key, start, end):
        self.lists[key] = self.lists.get(key, [])[start : end + 1]

    async def expire(self, key, ttl):
        self.ttls[key] = ttl

    async def lrem(self, key, count, value):
        if value in self.lists.get(key, []):
            self.lists[key].remove(value)


@pytest.mark.asyncio
async def test_content_preview_is_read_only_and_release_keeps_other_reservations(test_db):
    cache = RedisCache(ContentRedis())
    guard = AccountRiskGuard(test_db, cache=cache)
    text = "Discussing practical deployment measurements in this community"
    assert (await guard.preview_content(1, AccountRiskAction.AD_PROBE, "group", 7, text)).allowed
    assert not cache.client.values and not cache.client.lists
    first = await guard._check_content_policy(
        1, AccountRiskAction.AD_PROBE, "group", 7, {"content": text}
    )
    blocked = await guard.preview_content(1, AccountRiskAction.AD_PROBE, "group", 7, text)
    assert blocked.allowed is False and blocked.retry_after_seconds == 259200
    await guard.release_content_reservation(first.content_reservation)
    assert (await guard.preview_content(1, AccountRiskAction.AD_PROBE, "group", 7, text)).allowed
    second = await guard._check_content_policy(
        1, AccountRiskAction.AD_PROBE, "group", 7, {"content": text}
    )
    await guard.release_content_reservation(first.content_reservation)
    assert (
        cache.client.values[second.content_reservation["account_key"]]
        == second.content_reservation["token"]
    )


@pytest.mark.asyncio
async def test_execution_propagates_exact_retry_without_sending():
    client = SimpleNamespace(send_message=AsyncMock())
    guard = SimpleNamespace(
        check_and_reserve=AsyncMock(return_value=RiskDecision(False, "ad_probe_cooldown", 3590))
    )
    service = TelegramExecutionService(guard)
    service._outbound_service = AsyncMock(return_value=None)
    with pytest.raises(TelegramExecutionError) as exc:
        await service.send_group_message(
            SimpleNamespace(client=client), 123, "message", source="ad_probe"
        )
    assert exc.value.retry_after_seconds == 3590
    client.send_message.assert_not_awaited()


def test_probe_capacity_and_cross_midnight_window():
    now = datetime(2026, 9, 20, 14)
    capacity = {"max_new_ad_groups_per_day": 10, "window_start_hour": 9, "window_end_hour": 2}
    account = SimpleNamespace(
        risk_level="normal",
        created_at=now - timedelta(days=15),
        risk_pause_until=None,
        risk_recovery_until=None,
    )
    assert write_probe_limit(capacity, account, now) == 10
    account.created_at = now - timedelta(days=12)
    assert write_probe_limit(capacity, account, now) == 10
    account.risk_level = "watch"
    assert write_probe_limit(capacity, account, now) == 2
    account.risk_level = "normal"
    account.risk_recovery_until = now + timedelta(hours=1)
    assert write_probe_limit(capacity, account, now) == 2
    account.risk_recovery_until = None
    account.risk_pause_until = now + timedelta(hours=1)
    assert write_probe_limit(capacity, account, now) == 2
    account.risk_pause_until = None
    assert write_probe_limit({"max_new_ad_groups_per_day": 100}, account, now) == 10
    assert write_probe_limit({"max_new_ad_groups_per_day": 0}, account, now) == 0
    assert probe_day_start(datetime(2026, 9, 20, 17), capacity) == datetime(2026, 9, 20, 1)
    assert in_ad_window_at(datetime(2026, 9, 20, 19), capacity) == datetime(2026, 9, 21, 1)


async def target(db, now, count=1):
    account = TelegramAccount(
        identifier="pipeline",
        session_name="pipeline",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
        created_at=now - timedelta(days=20),
        risk_level="normal",
    )
    campaign = AdCampaign(name="pipeline", enabled=True, status="active", delivery_policy="growth")
    db.add_all([account, campaign])
    await db.flush()
    config = AccountOperationConfig(account_id=account.id, enabled=True, auto_ads_enabled=True)
    db.add_all(
        [config, AccountAdBinding(account_id=account.id, ad_campaign_id=campaign.id, enabled=True)]
    )
    memberships = []
    for i in range(count):
        group = Group(
            group_id=990000 + i, title=f"Pipeline {i}", level=GroupLevel.A, status="active"
        )
        db.add(group)
        await db.flush()
        m = GroupAccountMembership(
            group_id=group.id,
            telegram_group_id=group.group_id,
            account_id=account.id,
            status="joined",
            review_status=JOIN_REVIEW_APPROVED,
            joined_at=now - timedelta(days=10),
            probe_status="not_started",
            ad_status="warming",
            warmup_status="joined_pending_test",
        )
        db.add_all(
            [
                m,
                GroupAdProfile(
                    group_id=group.id, telegram_group_id=group.group_id, ad_policy_mode="unknown"
                ),
            ]
        )
        memberships.append(m)
    await db.commit()
    return account, config, memberships


@pytest.mark.asyncio
async def test_quota_wait_is_persisted_and_backlog_stops_only_growth(test_db, monkeypatch):
    now = datetime(2026, 9, 20, 14)
    account, config, memberships = await target(test_db, now, 8)
    service = AcquisitionAutomationService(test_db)
    result = await service._write_probe_backlog(config, now)
    assert result["join_paused"] and result["pending"] == 8
    config.operation_mode = "ad_only"
    assert not (await service._write_probe_backlog(config, now))["join_paused"]
    service._new_ad_group_quota_skip_reason = AsyncMock(return_value="new_ad_group_daily_quota")
    service._send_ad_probe = AsyncMock()
    reason = await service._ad_warmup_skip_reason(account.id, memberships[0], now, dry_run=False)
    # Return to Growth because dedicated operation deliberately bypasses neutral warmup.
    assert reason is None
    config.operation_mode = "growth"
    await test_db.commit()
    reason = await service._ad_warmup_skip_reason(account.id, memberships[0], now, dry_run=False)
    assert reason == "new_ad_group_daily_quota"
    assert memberships[0].probe_status == "scheduled"
    assert memberships[0].probe_due_at == datetime(2026, 9, 21, 1, 0, 30)
    service._send_ad_probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_future_queue_head_does_not_block_due_group(test_db, monkeypatch):
    from app.modules.acquisition import automation as mod

    now = datetime.utcnow()
    account, config, memberships = await target(test_db, now, 2)
    memberships[0].probe_status = "scheduled"
    memberships[0].probe_due_at = now + timedelta(hours=3)
    memberships[1].probe_status = "scheduled"
    memberships[1].probe_due_at = now - timedelta(minutes=1)
    await test_db.commit()
    monkeypatch.setattr(
        mod, "get_group_ai_interaction_settings", AsyncMock(return_value={"enabled": False})
    )
    service = AcquisitionAutomationService(test_db)
    service._sync_account_pool = AsyncMock()
    service._group_can_receive_ads = AsyncMock(return_value=True)
    service._ad_account_risk_skip_reason = AsyncMock(return_value=None)
    service._ad_warmup_skip_reason = AsyncMock(return_value="ad_probe_success_wait")
    result = await service._advance_disabled_group_warmup_to_write_probe(now, dry_run=False)
    assert result["succeeded"] == 1
    assert service._ad_warmup_skip_reason.call_args.args[1].id == memberships[1].id


@pytest.mark.asyncio
async def test_queue_keeps_permission_and_observation_separate(test_db):
    now = datetime(2026, 9, 20, 14)
    account, config, memberships = await target(test_db, now, 2)
    profile = (
        await test_db.execute(
            select(GroupAdProfile).where(GroupAdProfile.group_id == memberships[0].group_id)
        )
    ).scalar_one()
    profile.ad_policy_mode = "approval_required"
    memberships[1].probe_status = "success"
    memberships[1].first_ad_allowed_at = now + timedelta(days=2)
    memberships[1].ad_eligible_after = now + timedelta(hours=24)
    await test_db.commit()
    result = await build_probe_queue(test_db, now)
    assert result["counts"] == {"approval_required": 1, "observing": 1}
    assert result["items"][1]["earliest_action_at"] == "2026-09-22T14:00:00Z"


@pytest.mark.asyncio
async def test_probe_chooses_available_content_and_preserves_retry_after(test_db, monkeypatch):
    from app.modules.acquisition import automation as mod

    now = datetime(2026, 9, 20, 14)
    account, config, memberships = await target(test_db, now)
    runtime = SimpleNamespace(record_message=Mock())
    pool = SimpleNamespace(acquire_by_id=AsyncMock(return_value=runtime), release=AsyncMock())
    service = AcquisitionAutomationService(test_db, account_pool=pool)
    service._ad_send_target = AsyncMock(return_value=990000)
    service.risk_guard.preview_content = AsyncMock(
        side_effect=[RiskDecision(False, "content_repeat_account", 18000), RiskDecision(True)]
    )
    service.telegram_execution.send_group_message = AsyncMock(
        side_effect=TelegramExecutionError(
            "risk_guard_blocked:ad_probe_cooldown", retry_after_seconds=3590
        )
    )
    monkeypatch.setattr(mod, "_now", lambda: now)
    monkeypatch.setattr(mod.random, "shuffle", lambda values: None)
    group = await test_db.get(Group, memberships[0].group_id)
    result = await service._send_ad_probe_locked(account.id, memberships[0], group)
    assert result == "ad_probe_risk_guard_skipped"
    assert (
        service.telegram_execution.send_group_message.call_args.args[2] == mod.AD_PROBE_MESSAGES[1]
    )
    assert memberships[0].probe_due_at == now + timedelta(seconds=3620)
    assert memberships[0].probe_status == "scheduled"


@pytest.mark.asyncio
async def test_dedicated_pacing_fits_nine_admitted_groups_into_daily_cap(test_db):
    import json

    now = datetime(2026, 9, 20, 14)
    account, config, memberships = await target(test_db, now, 9)
    config.operation_mode = "ad_only"
    config.max_messages_per_day = 300
    campaign = (await test_db.execute(select(AdCampaign))).scalar_one()
    campaign.delivery_policy = "ad_only"
    campaign.interval_minutes = 10
    campaign.max_sends_per_account_per_day = 300
    campaign.max_sends_per_group_per_day = 300
    campaign.target_group_ids = json.dumps([m.group_id for m in memberships])
    for m in memberships:
        m.join_method = "manual_link_join"
        m.ad_status = "active"
        group = await test_db.get(Group, m.group_id)
        group.ad_delivery_account_id = account.id
        profile = (
            await test_db.execute(
                select(GroupAdProfile).where(GroupAdProfile.group_id == m.group_id)
            )
        ).scalar_one()
        profile.ad_policy_mode = "soft_ad_allowed"
        profile.ad_policy_confidence = 100
        profile.ad_tier = "trial"
    await test_db.commit()
    service = AcquisitionAutomationService(test_db)
    assert await service._ad_only_capacity_interval_seconds(campaign, account.id, now) == 1836
    for m in memberships[1:]:
        m.ad_status = "blocked"
    await test_db.commit()
    assert await service._ad_only_capacity_interval_seconds(campaign, account.id, now) == 600


@pytest.mark.asyncio
async def test_probe_cache_failure_defers_without_telegram_or_group_block(test_db, monkeypatch):
    from app.modules.acquisition import automation as mod

    now = datetime(2026, 9, 20, 14)
    account, config, memberships = await target(test_db, now)
    runtime = SimpleNamespace(record_message=Mock())
    pool = SimpleNamespace(acquire_by_id=AsyncMock(return_value=runtime), release=AsyncMock())
    service = AcquisitionAutomationService(test_db, account_pool=pool)
    service._ad_send_target = AsyncMock(return_value=990000)
    service.risk_guard.preview_content = AsyncMock(side_effect=ConnectionError("cache unavailable"))
    service.telegram_execution.send_group_message = AsyncMock()
    monkeypatch.setattr(mod, "_now", lambda: now)
    group = await test_db.get(Group, memberships[0].group_id)
    result = await service._send_ad_probe_locked(account.id, memberships[0], group)
    assert result == "ad_probe_content_waiting"
    assert memberships[0].probe_status == "scheduled"
    assert memberships[0].ad_status == "warming"
    assert memberships[0].probe_due_at == now + timedelta(minutes=5)
    service.telegram_execution.send_group_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_dynamic_v3_join_ignores_legacy_probe_backlog(test_db):
    now = datetime(2026, 9, 20, 14)
    _, config, _ = await target(test_db, now, 8)
    config.dynamic_capacity_enabled = True
    await test_db.commit()
    service = AcquisitionAutomationService(test_db)
    service._qualification_workflow_enabled = AsyncMock(return_value=True)
    result = await service._write_probe_backlog(config, now)
    assert result == {"pending": 0, "daily_throughput": 0.0, "wait_days": 0.0, "join_paused": False}
    service._qualification_workflow_enabled.return_value = False
    assert (await service._write_probe_backlog(config, now))["join_paused"]

@pytest.mark.asyncio
async def test_ten_probe_capacity_counts_successes_and_keeps_backlog_paused(test_db, monkeypatch):
    from app.api.automation import AdCapacityUpdate
    from app.core.automation_settings import normalize_ad_capacity_settings

    capacity = normalize_ad_capacity_settings(AdCapacityUpdate(max_new_ad_groups_per_day=10).model_dump())
    now = datetime(2026, 9, 20, 14)
    account, config, memberships = await target(test_db, now, 21)
    account.created_at = now - timedelta(days=12)
    monkeypatch.setattr(
        "app.modules.acquisition.automation.get_ad_capacity_settings",
        AsyncMock(return_value=capacity),
    )
    service = AcquisitionAutomationService(test_db)
    for membership in memberships[:9]:
        membership.probe_status = "success"
        membership.last_probe_at = now
    await test_db.commit()
    assert await service._new_ad_group_quota_skip_reason(account.id, now) is None
    assert (await service._write_probe_backlog(config, now))["join_paused"]
    memberships[9].probe_status = "success"
    memberships[9].last_probe_at = now
    await test_db.commit()
    assert await service._new_ad_group_quota_skip_reason(account.id, now) == "new_ad_group_daily_quota"
    assert (await service._write_probe_backlog(config, now))["join_paused"]

@pytest.mark.asyncio
async def test_action_cooldown_preview_is_read_only_and_separate(test_db):
    cache = RedisCache(ContentRedis())
    guard = AccountRiskGuard(test_db, cache=cache)
    deadline = int(datetime.utcnow().timestamp()) + 3600
    cache.client.values['risk:account:7:cooldown:ad_probe'] = str(deadline)
    before = dict(cache.client.values)
    assert 3598 <= await guard.peek_action_cooldown(7, AccountRiskAction.AD_PROBE) <= 3600
    assert await guard.peek_join_cooldown(7) == 0
    assert cache.client.values == before


@pytest.mark.asyncio
@pytest.mark.parametrize('unavailable', [False, True])
async def test_probe_wait_prevents_client_acquisition(test_db, monkeypatch, unavailable):
    from app.modules.acquisition import automation as mod
    now = datetime(2026, 9, 21, 5)
    account, _, memberships = await target(test_db, now)
    pool = SimpleNamespace(acquire_by_id=AsyncMock(), release=AsyncMock())
    service = AcquisitionAutomationService(test_db, account_pool=pool)
    service.risk_guard.peek_action_cooldown = AsyncMock(
        side_effect=ConnectionError('cache unavailable') if unavailable else None,
        return_value=1800,
    )
    service.telegram_execution.send_group_message = AsyncMock()
    monkeypatch.setattr(mod, '_now', lambda: now)
    group = await test_db.get(Group, memberships[0].group_id)
    assert await service._send_ad_probe_locked(account.id, memberships[0], group) == 'ad_probe_risk_guard_skipped'
    assert memberships[0].probe_due_at == now + timedelta(seconds=300 if unavailable else 1830)
    assert memberships[0].ad_status == 'warming'
    assert memberships[0].probe_status == 'scheduled'
    pool.acquire_by_id.assert_not_awaited()
    service.telegram_execution.send_group_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduler_waits_once_for_whole_account_and_resumes_when_due(test_db, monkeypatch):
    from app.modules.acquisition import automation as mod
    now = datetime.utcnow()
    account, _, memberships = await target(test_db, now, 3)
    monkeypatch.setattr(mod, 'get_group_ai_interaction_settings', AsyncMock(return_value={'enabled': False}))
    service = AcquisitionAutomationService(test_db)
    service.risk_guard.peek_action_cooldown = AsyncMock(return_value=1800)
    service._sync_account_pool = AsyncMock()
    service._group_can_receive_ads = AsyncMock(return_value=True)
    service._ad_account_risk_skip_reason = AsyncMock(return_value=None)
    service._ad_warmup_skip_reason = AsyncMock(return_value='ad_probe_success_wait')
    result = await service._advance_disabled_group_warmup_to_write_probe(now, dry_run=False)
    assert result['skipped'] == 1 and result['processed'] == 0
    assert result['details'][0]['retry_after_seconds'] == 1800
    service.risk_guard.peek_action_cooldown.assert_awaited_once_with(account.id, AccountRiskAction.AD_PROBE)
    service._sync_account_pool.assert_not_awaited()
    service._ad_warmup_skip_reason.assert_not_awaited()
    service.risk_guard.peek_action_cooldown.return_value = 0
    result = await service._advance_disabled_group_warmup_to_write_probe(now, dry_run=False)
    assert result['succeeded'] == 1
    service._sync_account_pool.assert_awaited_once()


@pytest.mark.asyncio
async def test_queue_includes_account_cooldown_but_preserves_permission_gate(test_db, monkeypatch):
    now = datetime(2026, 9, 21, 5)
    account, _, memberships = await target(test_db, now, 2)
    profile = (await test_db.execute(select(GroupAdProfile).where(GroupAdProfile.group_id == memberships[0].group_id))).scalar_one()
    profile.ad_policy_mode = 'approval_required'
    await test_db.commit()
    peek = AsyncMock(return_value=1800)
    monkeypatch.setattr(AccountRiskGuard, 'peek_action_cooldown', peek)
    result = await build_probe_queue(test_db, now)
    assert result['counts'] == {'approval_required': 1, 'probe_wait': 1}
    assert result['items'][1]['earliest_action_at'] == '2026-09-21T05:30:30Z'
    peek.assert_awaited_once_with(account.id, AccountRiskAction.AD_PROBE)
    peek.side_effect = ConnectionError('cache unavailable')
    result = await build_probe_queue(test_db, now)
    assert result['items'][1]['earliest_action_at'] is None
    assert result['accounts'][0]['cooldown_seconds'] is None
