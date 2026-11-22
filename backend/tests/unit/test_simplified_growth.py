"""Behavioral acceptance for writable membership growth, using real Lua scripts."""
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
from sqlalchemy import event, func, select

from app.core.account.models import (
    AccountOperationConfig,
    AccountStatus,
    AccountType,
    TelegramAccount,
)
from app.core.config import settings
from app.core.group.models import Group
from app.core.group.models import GroupAccountMembership as Member
from app.core.settings_models import SystemSetting
from app.modules.acquisition import simple_growth
from app.modules.acquisition.automation import AcquisitionAutomationService
from app.modules.acquisition.member_permissions import apply_permission, inspect_permission
from app.modules.acquisition.models import (
    AccountAdBinding,
    AdCampaign,
    AdCreative,
    AdDeliveryLog,
    GroupAdFrequency,
)
from app.modules.acquisition.simple_growth import GrowthService, campaign_matches
from app.modules.acquisition.simple_limits import PREFIX, Deferred, lease, take_ad, take_join


@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


def test_writable_has_no_precedent_or_rules_requirement():
    now = datetime.utcnow()
    fact = inspect_permission(NS(left=False, default_banned_rights=None),
                              NS(is_admin=False, is_creator=False, participant=NS()), now)
    assert fact.writable is True
    member = NS(next_ad_allowed_at=None, verification_status="complete")
    apply_permission(member, fact, now)
    assert member.review_status == "approved"
    assert member.next_ad_allowed_at == now


@pytest.mark.parametrize("level", ["unrated", "A", "B", "C"])
def test_permission_campaign_ignores_old_level_filter_but_keeps_explicit_targets(level):
    now = datetime.utcnow()
    campaign = AdCampaign(enabled=True, delivery_policy="growth", target_group_levels='["A"]')
    group = Group(id=12, group_id=123, level=level)
    assert campaign_matches(campaign, group, now)
    campaign.target_group_ids = '[13]'
    assert not campaign_matches(campaign, group, now)
    campaign.target_group_ids = '[12]'
    assert campaign_matches(campaign, group, now)
    campaign.end_at = now
    assert not campaign_matches(campaign, group, now)


@pytest.mark.parametrize("until,long", [(None, True), (timedelta(days=4), True), (timedelta(hours=3), False)])
def test_platform_mute_is_not_ad_permission(until, long):
    now = datetime.utcnow()
    rights = NS(send_messages=True, until_date=now + until if until else None)
    fact = inspect_permission(NS(left=False), NS(is_admin=False, participant=NS(banned_rights=rights)), now)
    assert fact.writable is False
    assert fact.exit_required is long


@pytest.mark.asyncio
async def test_join_concurrency_cooldown_and_no_refund(redis):
    outcomes = await asyncio.gather(*(take_join(redis, 1) for _ in range(20)), return_exceptions=True)
    assert sum(isinstance(v, datetime) for v in outcomes) == 1
    assert await redis.hget(PREFIX + "join:1:count", "used") == "1"
    assert all(isinstance(v, (datetime, Deferred)) for v in outcomes)


@pytest.mark.asyncio
async def test_daily_cap_and_utc_rollover(redis):
    key = PREFIX + "join:1:count"
    day = int(datetime.now(UTC).timestamp()) // 86400
    await redis.hset(key, mapping={"day": day, "used": 30})
    with pytest.raises(Deferred):
        await take_join(redis, 1)
    await redis.hset(key, "day", day - 1)
    await take_join(redis, 1)
    assert await redis.hget(key, "used") == "1"


@pytest.mark.asyncio
async def test_cross_account_group_spacing(redis):
    await take_ad(redis, 1, -1000000000123, cap=30, account_interval=1, group_interval=86400)
    with pytest.raises(Deferred):
        await take_ad(redis, 2, -1000000000123, cap=30, account_interval=1, group_interval=86400)
    assert await redis.hget(PREFIX + "ads:2:count", "used") is None


@pytest.mark.asyncio
async def test_lock_release_cannot_delete_new_owner(redis):
    async with lease(redis, "test"):
        with pytest.raises(Deferred):
            async with lease(redis, "test"):
                pass
        await redis.set(PREFIX + "lock:test", "other")
    assert await redis.get(PREFIX + "lock:test") == "other"


@pytest.mark.asyncio
async def test_redis_failure_never_admits_rpc():
    redis = NS(eval=AsyncMock(side_effect=ConnectionError))
    with pytest.raises(Deferred, match="redis_unavailable"):
        await take_join(redis, 1)


async def setup(db, monkeypatch, redis):
    monkeypatch.setattr(settings, "GROWTH_SIMPLIFIED_ENABLED", True)
    monkeypatch.setattr(settings, "TELEGRAM_RPC_PACING_ENABLED", True)
    monkeypatch.setattr(simple_growth, "get_redis", AsyncMock(return_value=redis))
    monkeypatch.setattr("app.core.redis.get_redis", AsyncMock(return_value=redis))
    now = datetime.utcnow()
    account = TelegramAccount(identifier="simple-test", session_name="simple-test", account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE, risk_level="normal", is_active=True)
    group = Group(group_id=-1000000000123, title="测试群", username="simple_group", status="active", level="A")
    campaign = AdCampaign(name="simple", enabled=True, interval_minutes=0, max_sends_per_group_per_day=30)
    creative = AdCreative(name="one text", content="这是一条纯文字广告", enabled=True, weight=1)
    db.add_all([account, group, campaign, creative])
    await db.flush()
    config = AccountOperationConfig(account_id=account.id, enabled=True, auto_ads_enabled=True, auto_join_enabled=True)
    member = Member(group_id=group.id, telegram_group_id=group.group_id, account_id=account.id,
        status="joined", joined_at=now-timedelta(days=4), review_status="approved", ad_status="active",
        can_send_text=True, permission_checked_at=now, verification_status="complete", next_ad_allowed_at=now)
    db.add_all([config, member, AccountAdBinding(account_id=account.id, ad_campaign_id=campaign.id,
        creative_id=creative.id, enabled=True), SystemSetting(key="automation.ad_delivery_execution", value='{"enabled":true}')])
    db.add(SystemSetting(key="automation.group_qualification", value=
        '{"enabled":true,"execute_reviews":true,"execute_verification":true,"execute_exits":true}'))
    await db.commit()
    client = NS(get_input_entity=AsyncMock(return_value=group.group_id),
                send_message=AsyncMock(return_value=NS(id=123, ttl_period=None)))
    wrapper = NS(client=client, account_id=account.id)
    pool = NS(acquire_by_id=AsyncMock(return_value=wrapper), release=AsyncMock())
    service = AcquisitionAutomationService(db, account_pool=pool)
    return GrowthService(service), account, member, campaign, creative, client


@pytest.mark.asyncio
async def test_permission_and_exit_business_pause_prevents_rpc(test_db, monkeypatch, redis):
    svc, account, member, *_ = await setup(test_db, monkeypatch, redis)
    control = await test_db.get(SystemSetting, "automation.group_qualification")
    control.value = '{"enabled":false,"execute_reviews":true,"execute_verification":true,"execute_exits":true}'
    member.review_status = "pending"
    member.review_next_at = member.leave_requested_at = member.leave_retry_at = datetime.utcnow()
    member.leave_error = "permanent_mute"
    await test_db.commit()
    assert (await svc.review(account.id))["reason"] == "permission_review_paused"
    assert await svc.exits(account.id, 2) == 0
    svc.pool.acquire_by_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_has_no_pre_rpc_write_and_one_result_commit(test_db, monkeypatch, redis):
    svc, account, member, campaign, creative, client = await setup(test_db, monkeypatch, redis)
    commits = []
    def committed(_session):
        commits.append(1)
    event.listen(test_db.sync_session, "after_commit", committed)
    async def sent(*args, **kwargs):
        assert not test_db.in_transaction()
        assert not test_db.new and not test_db.dirty
        commits.clear()  # Read-only snapshots may commit before SDK entry.
        return NS(id=123)
    client.send_message.side_effect = sent
    try:
        outcome = await svc.send(account.id, member.id, campaign.id, creative.id, -1000000000123)
    finally:
        event.remove(test_db.sync_session, "after_commit", committed)
    assert outcome["succeeded"] == 1
    assert len(commits) == 1
    assert client.send_message.await_count == 1
    assert await test_db.scalar(select(func.count(AdDeliveryLog.id))) == 1
    state = await test_db.get(GroupAdFrequency, -1000000000123)
    assert state is None or state.daily_review_due_at is None


@pytest.mark.asyncio
async def test_unknown_send_is_not_replayed(test_db, monkeypatch, redis):
    svc, account, member, campaign, creative, client = await setup(test_db, monkeypatch, redis)
    client.send_message.side_effect = TimeoutError()
    args = account.id, member.id, campaign.id, creative.id, -1000000000123
    outcome = await svc.send(*args)
    assert outcome["outcome"] == "unknown" and outcome["succeeded"] == 0
    await svc.send(*args)
    assert client.send_message.await_count == 1


@pytest.mark.asyncio
async def test_committed_send_retries_only_result_transaction(test_db, monkeypatch, redis):
    svc, account, member, campaign, creative, client = await setup(test_db, monkeypatch, redis)
    commit = test_db.commit
    failed = False
    async def flaky():
        nonlocal failed
        if client.send_message.await_count and not failed:
            failed = True
            raise RuntimeError("database_unavailable")
        await commit()
    monkeypatch.setattr(test_db, "commit", flaky)
    outcome = await svc.send(account.id, member.id, campaign.id, creative.id, -1000000000123)
    assert outcome["message_id"] == 123 and client.send_message.await_count == 1
    assert await test_db.scalar(select(func.count(AdDeliveryLog.id))) == 1


@pytest.mark.asyncio
async def test_persistent_commit_failure_recovers_result_without_sending(test_db, monkeypatch, redis):
    svc, account, member, campaign, creative, client = await setup(test_db, monkeypatch, redis)
    args = account.id, member.id, campaign.id, creative.id, -1000000000123
    account_id = account.id
    commit = test_db.commit
    async def unavailable():
        if client.send_message.await_count:
            raise RuntimeError("database_unavailable")
        await commit()
    monkeypatch.setattr(test_db, "commit", unavailable)
    with pytest.raises(RuntimeError, match="database_unavailable"):
        await svc.send(*args)
    assert await redis.hlen(PREFIX + f"results:{account_id}") == 1
    monkeypatch.setattr(test_db, "commit", commit)
    await svc.recover_results(account_id)
    assert await redis.hlen(PREFIX + f"results:{account_id}") == 0
    assert await test_db.scalar(select(AdDeliveryLog.telegram_message_id)) == 123
    await svc.send(*args)
    assert client.send_message.await_count == 1


@pytest.mark.asyncio
async def test_result_recovery_does_not_require_enabled_account_or_session(test_db, monkeypatch, redis):
    from app.modules.acquisition.simple_runtime import recover_result_journals
    svc, account, member, campaign, creative, client = await setup(test_db, monkeypatch, redis)
    account_id = account.id
    persist = svc.persist_ad_result
    monkeypatch.setattr(svc, "persist_ad_result", AsyncMock(side_effect=RuntimeError("db_down")))
    with pytest.raises(RuntimeError, match="db_down"):
        await svc.send(account_id, member.id, campaign.id, creative.id, -1000000000123)
    config = await test_db.scalar(select(AccountOperationConfig).where(AccountOperationConfig.account_id == account_id))
    config.enabled = False
    account.status = AccountStatus.ERROR
    await test_db.commit()
    monkeypatch.setattr(svc, "persist_ad_result", persist)
    # SCAN may return an empty page. Several bounded ticks visit the full keyspace.
    for _ in range(50):
        await recover_result_journals(test_db, redis)
        if not await redis.hlen(PREFIX + f"results:{account_id}"):
            break
    assert await redis.hlen(PREFIX + f"results:{account_id}") == 0
    assert await test_db.scalar(select(AdDeliveryLog.telegram_message_id)) == 123
    assert client.send_message.await_count == 1


@pytest.mark.asyncio
async def test_result_scan_retains_oversized_page_tail(test_db, monkeypatch, redis):
    from app.modules.acquisition.simple_runtime import recover_result_journals

    keys = [PREFIX + f"results:{account}" for account in range(1, 6)]
    monkeypatch.setattr(redis, "scan", AsyncMock(return_value=(0, keys)))
    recovered = AsyncMock()
    monkeypatch.setattr(GrowthService, "recover_results", recovered)
    for _ in range(3):
        await recover_result_journals(test_db, redis)
    assert [call.args[0] for call in recovered.await_args_list] == [1, 2, 3, 4, 5]
    redis.scan.assert_awaited_once()


@pytest.mark.asyncio
async def test_join_scans_past_filtered_window_without_network(test_db, monkeypatch, redis):
    from app.modules.acquisition.join_budget import JoinRequestBudgetService

    svc, account, *_ = await setup(test_db, monkeypatch, redis)
    monkeypatch.setattr(JoinRequestBudgetService, "eligibility_reason", AsyncMock(return_value=None))
    groups = [Group(group_id=-1000000010000-i, title="English only", username=f"candidate_{i}",
                    status="pending_join", level_score=100-i) for i in range(30)]
    good = Group(group_id=-1000000020000, title="可加入的群", username="candidate_good",
                 status="pending_join", level_score=0)
    test_db.add_all([*groups, good])
    await test_db.commit()
    first = await svc.join(account.id)
    assert first["reason"] == "candidate_window_filtered" and first["examined"] == 30
    second = await svc.join(account.id, dry_run=True)
    assert second["candidate_id"] == good.id
    svc.pool.acquire_by_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_pause_blocks_final_send(test_db, monkeypatch, redis):
    svc, account, member, campaign, creative, client = await setup(test_db, monkeypatch, redis)
    member.ad_status = "paused"
    await test_db.commit()
    outcome = await svc.send(account.id, member.id, campaign.id, creative.id, -1000000000123)
    assert outcome["reason"] == "final_gate_denied"
    client.send_message.assert_not_awaited()


def test_writable_does_not_bypass_incomplete_verification():
    now = datetime.utcnow()
    fact = inspect_permission(NS(left=False), NS(is_admin=False, participant=NS()), now)
    member = NS(next_ad_allowed_at=None, verification_status="pending")
    apply_permission(member, fact, now)
    assert member.review_status == "pending" and member.ad_status == "blocked"
    assert member.permission_reason == "verification_pending"


@pytest.mark.asyncio
async def test_lock_loss_cancels_work_and_does_not_release_other_owner(redis):
    with pytest.raises(Deferred, match="operation_lock_lost"):
        async with lease(redis, "lost", ttl=1):
            await redis.set(PREFIX + "lock:lost", "new-owner")
            await asyncio.wait_for(asyncio.Future(), timeout=2)
    assert await redis.get(PREFIX + "lock:lost") == "new-owner"


@pytest.mark.asyncio
async def test_cancelled_send_keeps_full_group_cooldown(test_db, monkeypatch, redis):
    svc, account, member, campaign, creative, client = await setup(test_db, monkeypatch, redis)
    client.send_message.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await svc.send(account.id, member.id, campaign.id, creative.id, -1000000000123)
    assert await redis.ttl(PREFIX + "group:-1000000000123:due") >= 10798
    assert await redis.get(PREFIX + "lock:group:-1000000000123") is None
    assert await test_db.scalar(select(AdDeliveryLog.status)) == "unknown"


@pytest.mark.asyncio
async def test_new_permission_event_wins_over_inflight_read(test_db, monkeypatch, redis):
    from app.modules.acquisition.simple_runtime import apply_fact
    svc, account, member, _campaign, _creative, client = await setup(test_db, monkeypatch, redis)
    member.review_status, member.review_next_at = "pending", datetime.utcnow()
    await test_db.commit()
    client.get_entity = AsyncMock(return_value=NS(id=123, megagroup=True, left=False))
    async def permissions(*_args):
        assert not test_db.in_transaction()
        await apply_fact(test_db, account.id, {"kind": "membership", "peer": member.telegram_group_id,
                        "at": datetime.now(UTC).timestamp(), "member": True, "blocked": True})
        await test_db.commit()
        return NS(is_admin=False, participant=NS())
    client.get_permissions = AsyncMock(side_effect=permissions)
    await svc.review(account.id)
    assert member.review_status == "pending" and member.can_send_text is None


@pytest.mark.asyncio
@pytest.mark.parametrize("namespace", ["channel", "chat"])
async def test_legacy_positive_id_requires_live_identity_before_selection(test_db, monkeypatch, redis, namespace):
    from telethon.tl.types import ChatEmpty

    svc, account, member, campaign, creative, client = await setup(test_db, monkeypatch, redis)
    group = await test_db.get(Group, member.group_id)
    group.group_id = member.telegram_group_id = 123
    await test_db.commit()
    svc.send = AsyncMock(return_value={"selected": True})
    assert (await svc.ads(account.id))["reason"] == "no_due_eligible_target"
    svc.send.assert_not_awaited()
    assert member.review_status == "pending" and member.can_send_text is None

    client.get_entity = AsyncMock(return_value=(
        NS(id=123, megagroup=True, left=False) if namespace == "channel" else ChatEmpty(123)))
    client.get_permissions = AsyncMock(return_value=NS(is_admin=False, participant=NS()))
    assert (await svc.review(account.id))["approved"] == 1
    marked = -1000000000123 if namespace == "channel" else -123
    assert member.telegram_group_id == marked
    assert group.group_id == 123  # Historical group references are retained.
    assert (await svc.ads(account.id)) == {"selected": True}
    svc.send.assert_awaited_once_with(account.id, member.id, campaign.id, creative.id, marked)


@pytest.mark.asyncio
async def test_permission_identity_mismatch_stays_pending(test_db, monkeypatch, redis):
    svc, account, member, _campaign, _creative, client = await setup(test_db, monkeypatch, redis)
    member.review_status, member.review_next_at = "pending", datetime.utcnow()
    await test_db.commit()
    client.get_entity = AsyncMock(return_value=NS(id=456, megagroup=True, left=False))
    client.get_permissions = AsyncMock()
    assert (await svc.review(account.id))["approved"] == 0
    client.get_permissions.assert_not_awaited()
    assert member.review_status == "pending"
    assert member.telegram_group_id == -1000000000123


@pytest.mark.asyncio
async def test_shared_group_cooldown_does_not_starve_other_due_groups(test_db, monkeypatch, redis):
    svc, account, member, campaign, creative, _client = await setup(test_db, monkeypatch, redis)
    other = TelegramAccount(identifier="other-promoter", session_name="other-promoter",
                            account_type=AccountType.PROMOTER, status=AccountStatus.ONLINE)
    fallback = Group(group_id=-1000000000124, title="未评级群", level="unrated", status="active")
    test_db.add_all([other, fallback])
    await test_db.flush()
    test_db.add(Member(account_id=other.id, group_id=member.group_id,
                       telegram_group_id=member.telegram_group_id, status="joined",
                       next_ad_allowed_at=datetime.utcnow() + timedelta(days=1)))
    ready = Member(account_id=account.id, group_id=fallback.id, telegram_group_id=fallback.group_id,
                   status="joined", review_status="approved", ad_status="active", can_send_text=True,
                   verification_status="complete", next_ad_allowed_at=datetime.utcnow(),
                   permission_checked_at=datetime.utcnow())
    test_db.add(ready)
    await test_db.commit()
    svc.send = AsyncMock(return_value={"selected": True})
    assert await svc.ads(account.id) == {"selected": True}
    svc.send.assert_awaited_once_with(account.id, ready.id, campaign.id, creative.id, fallback.group_id)


@pytest.mark.asyncio
async def test_weighted_choice_receives_all_enabled_bound_text_creatives(test_db, monkeypatch, redis):
    svc, account, member, campaign, first, _client = await setup(test_db, monkeypatch, redis)
    second = AdCreative(name="second", content="第二条纯文字", enabled=True, weight=200)
    linked = AdCreative(name="linked", content="https://example.com", enabled=True, weight=1000)
    unbound = AdCreative(name="unbound", content="未绑定素材", enabled=True, weight=1000)
    test_db.add_all([second, linked, unbound])
    await test_db.flush()
    test_db.add_all([AccountAdBinding(account_id=account.id, ad_campaign_id=campaign.id,
        creative_id=c.id, enabled=True, priority=0) for c in (second, linked)])
    await test_db.commit()
    def choose(creatives):
        assert {c.id for c in creatives} == {first.id, second.id}
        return next(c for c in creatives if c.id == second.id)
    svc.service._weighted_creative_choice = choose
    svc.send = AsyncMock(return_value={"selected": True})
    assert await svc.ads(account.id) == {"selected": True}
    svc.send.assert_awaited_once_with(account.id, member.id, campaign.id, second.id, member.telegram_group_id)


@pytest.mark.asyncio
async def test_pacing_scope_includes_enabled_promoters_and_preserves_manual_pause(test_db, monkeypatch, redis):
    from app.modules.acquisition.qualification_service import policy
    svc, account, *_ = await setup(test_db, monkeypatch, redis)
    control = await test_db.get(SystemSetting, "automation.group_qualification")
    control.value = '{"enabled":true,"account_ids":[999],"rollout_accounts":{"1":{"phase":"paused"}}}'
    await test_db.commit()
    rules = await policy(test_db)
    assert rules["account_ids"] == [account.id]
    assert rules["verification_account_ids"] == [account.id]
    assert rules["rollout_accounts"]["1"]["phase"] == "paused"
    with pytest.raises(Deferred, match="manual_business_pause"):
        await svc.account(account.id)


@pytest.mark.asyncio
async def test_paused_member_leave_invalidates_permission_without_unpausing(test_db, monkeypatch, redis):
    from app.modules.acquisition.simple_runtime import apply_fact
    _svc, account, member, *_ = await setup(test_db, monkeypatch, redis)
    member.ad_status = "paused"
    await test_db.commit()
    await apply_fact(test_db, account.id, {"kind": "membership", "peer": member.telegram_group_id,
        "member": False, "at": datetime.now(UTC).timestamp()})
    assert member.status == "left" and member.can_send_text is None
    assert member.ad_status == "paused" and member.review_status == "rejected"


@pytest.mark.asyncio
async def test_pending_approval_promotes_only_on_membership_event(test_db, monkeypatch, redis):
    from app.modules.acquisition.simple_runtime import apply_fact
    _svc, account, member, *_ = await setup(test_db, monkeypatch, redis)
    member.status, member.joined_at = "pending", None
    await test_db.commit()
    fact = {"kind": "membership", "peer": member.telegram_group_id, "at": datetime.now(UTC).timestamp()}
    await apply_fact(test_db, account.id, fact)
    assert member.status == "pending"
    await apply_fact(test_db, account.id, {**fact, "member": True})
    assert member.status == "joined" and member.review_status == "pending"
    assert member.verification_status == "unknown"


@pytest.mark.asyncio
async def test_account_risk_feedback_is_committed_with_send_result(test_db, monkeypatch, redis):
    from telethon.errors import PeerFloodError
    svc, account, member, campaign, creative, client = await setup(test_db, monkeypatch, redis)
    client.send_message.side_effect = PeerFloodError(None)
    await svc.send(account.id, member.id, campaign.id, creative.id, -1000000000123)
    await test_db.refresh(account)
    assert account.risk_level == "frozen"
    assert account.risk_pause_until > datetime.utcnow()


@pytest.mark.asyncio
async def test_maintenance_never_reads_ad_history(test_db, monkeypatch, redis):
    svc, account, *_ = await setup(test_db, monkeypatch, redis)
    result = await svc.maintenance(account.id)
    assert result["succeeded"] == 0
    svc.pool.acquire_by_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_permission_changed_during_resolution_blocks_send(test_db, monkeypatch, redis):
    svc, account, member, campaign, creative, client = await setup(test_db, monkeypatch, redis)
    async def resolve(_target):
        member.ad_status = "paused"
        await test_db.commit()
        return member.telegram_group_id
    client.get_input_entity.side_effect = resolve
    outcome = await svc.send(account.id, member.id, campaign.id, creative.id, member.telegram_group_id)
    assert outcome["reason"] == "final_gate_denied"
    client.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_growth_and_non_growth_share_total_cap(redis):
    from app.core.operating_time import operating_date
    key = f"risk:account:1:daily:outbound_message:{operating_date():%Y%m%d}"
    await redis.set(key, 2)
    await take_ad(redis, 1, -1000000000123, cap=30, account_interval=1,
                  group_interval=1, total_cap=5, durable_used=2)
    assert await redis.get(key) == "3"
    await redis.delete(PREFIX + "ads:1:due")
    with pytest.raises(Deferred):
        await take_ad(redis, 1, -1000000000124, cap=30, account_interval=1,
                      group_interval=1, total_cap=5, durable_used=2)


@pytest.mark.parametrize("status,review,ad,leave,expected", [
    ("joined", "manual_required", "blocked", "ai_unavailable", ("joined", "pending", "blocked", None)),
    ("joined", "approved", "paused", None, ("joined", "rejected", "paused", None)),
    ("left", "approved", "active", None, ("left", "rejected", "blocked", None)),
    ("leave_failed", "rejected", "blocked", "permanent_mute", ("leave_failed", "pending", "blocked", "permanent_mute")),
    ("leave_failed", "rejected", "blocked", "no_ad_precedent", ("joined", "pending", "blocked", None)),
])
def test_cutover_preserves_real_controls_and_never_approves(status, review, ad, leave, expected):
    from scripts.activate_simple_growth import convert_member
    member = Member(status=status, review_status=review, ad_status=ad, leave_error=leave)
    convert_member(member, datetime.utcnow())
    assert (member.status, member.review_status, member.ad_status, member.leave_error) == expected
    assert member.can_send_text is None


@pytest.mark.asyncio
async def test_actual_rpc_pacing_and_cancellation(monkeypatch, redis):
    from app.core.account import rpc_pacing
    monkeypatch.setattr("app.core.redis.get_redis", AsyncMock(return_value=redis))
    monkeypatch.setattr(rpc_pacing, "restore_cooldown", AsyncMock())
    monkeypatch.setattr(settings, "TELEGRAM_RPC_MIN_INTERVAL_SECONDS", 0.02)
    await rpc_pacing.pace(7)
    first_due = int(await redis.get("vanguard:rpc:pace:7"))
    await rpc_pacing.pace(7)
    # Redis owns admission time; Windows event-loop clocks have coarse ticks.
    assert int(await redis.get("vanguard:rpc:pace:7")) >= first_due + 20
    await redis.set("vanguard:rpc:pace:7", int(datetime.now(UTC).timestamp() * 1000) + 10000, px=10000)
    task = asyncio.create_task(rpc_pacing.pace(7))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_rpc_cooldown_hydration_closes_database_before_redis(monkeypatch, redis):
    from contextlib import asynccontextmanager

    from app.core.account import rpc_pacing
    open_transaction = False
    @asynccontextmanager
    async def session():
        nonlocal open_transaction
        open_transaction = True
        try:
            yield object()
        finally:
            open_transaction = False
    monkeypatch.setattr("app.core.database.get_db_session", session)
    monkeypatch.setattr("app.core.account.rpc_governor.load_state", AsyncMock(
        return_value={"pause_until": (datetime.utcnow() + timedelta(seconds=45)).isoformat()}))
    original = redis.eval
    async def checked(*args):
        assert not open_transaction
        return await original(*args)
    monkeypatch.setattr(redis, "eval", checked)
    await rpc_pacing.restore_cooldown(redis, 7)
    assert 43 <= await redis.ttl("vanguard:rpc:cooldown:7") <= 46


@pytest.mark.asyncio
async def test_join_cancel_is_recorded_and_not_reissued(test_db, monkeypatch, redis):
    import json

    from telethon.tl.types import Channel, ChatPhotoEmpty

    from app.modules.acquisition.join_budget import JoinRequestBudgetService
    from app.modules.acquisition.models import AutoJoinAttempt
    svc, account, member, *_ = await setup(test_db, monkeypatch, redis)
    account_id, group_id = account.id, member.group_id
    await test_db.delete(member)
    await test_db.commit()
    monkeypatch.setattr(JoinRequestBudgetService, "eligibility_reason", AsyncMock(return_value=None))
    class Client:
        calls = 0
        async def get_entity(self, _target):
            assert not test_db.in_transaction()
            return Channel(id=123, title="测试", photo=ChatPhotoEmpty(), date=datetime.utcnow(), megagroup=True)
        async def __call__(self, _request):
            assert not test_db.in_transaction() and not test_db.new
            self.calls += 1
            raise asyncio.CancelledError()
    client = Client()
    svc.pool.acquire_by_id.return_value.client = client
    token = "test-manual-cancel"
    await redis.set(PREFIX + f"manual_join:{account_id}:{token}", json.dumps({"link": "https://t.me/simple_group"}))
    with pytest.raises(asyncio.CancelledError):
        await svc.join_link(account_id, token)
    row = await test_db.scalar(select(AutoJoinAttempt))
    assert row.status == "unknown" and row.telegram_action_attempted and row.group_id == group_id
    assert await redis.hget(PREFIX + f"join:{account_id}:count", "used") == "1"
    await svc.join_link(account_id, token)
    assert client.calls == 1


@pytest.mark.asyncio
async def test_disappeared_prompt_does_not_complete_known_pending_verification(test_db, monkeypatch, redis):
    svc, account, member, *_ = await setup(test_db, monkeypatch, redis)
    member.verification_status = "pending"
    member.joined_at = datetime.utcnow() - timedelta(minutes=5)
    await test_db.commit()
    client = NS(get_me=AsyncMock(return_value=NS(id=100)), get_messages=AsyncMock(return_value=[]))
    fact = inspect_permission(NS(left=False), NS(is_admin=False, participant=NS()), datetime.utcnow())
    assert not await svc.verify(NS(client=client), object(), member, fact, NS(enabled=True))
    member.verification_status = "unknown"
    assert await svc.verify(NS(client=client), object(), member, fact, NS(enabled=True))


@pytest.mark.asyncio
async def test_non_growth_reservation_replay_keeps_ledger_semantics(test_db, monkeypatch, redis):
    from app.core.account.outbound_budget import AccountOutboundBudgetService, OutboundBudgetBlocked
    svc, account, member, campaign, creative, _client = await setup(test_db, monkeypatch, redis)
    config = await test_db.scalar(select(AccountOperationConfig).where(AccountOperationConfig.account_id == account.id))
    config.dynamic_capacity_enabled = True
    config.max_messages_per_day = 2
    await test_db.commit()
    await svc.send(account.id, member.id, campaign.id, creative.id, member.telegram_group_id)
    ledger = AccountOutboundBudgetService(test_db)
    first = await ledger.reserve(account.id, attempt_key="verification:one", category="verification", target_key="10")
    replay = await ledger.reserve(account.id, attempt_key="verification:one", category="verification", target_key="10")
    assert replay.id == first.id and replay.state == "reserved"
    with pytest.raises(OutboundBudgetBlocked):
        await ledger.reserve(account.id, attempt_key="verification:two", category="verification", target_key="11")


@pytest.mark.asyncio
async def test_old_growth_reconciliation_ticket_never_reads_telegram(monkeypatch):
    from app.core.account.reconciliation_handlers import _execute
    monkeypatch.setattr(settings, "GROWTH_SIMPLIFIED_ENABLED", True)
    for kind in ("ad", "join", "growth"):
        outcome = await _execute(None, kind, 1, 1, account_pool=None)
        assert outcome["state"] == "cancelled"


@pytest.mark.asyncio
async def test_member_detail_api_does_not_resolve_historical_audit(test_db, monkeypatch, redis):
    from app.api.group_qualification import get_review
    _svc, _account, member, *_ = await setup(test_db, monkeypatch, redis)
    response = await get_review(member.id, test_db)
    assert response["mode"] == "writable_v2"
    assert response["data"]["membership_id"] == member.id
    assert response["data"]["authorization"]["grant_audit_id"] is None


@pytest.mark.asyncio
async def test_callback_account_requires_inbound_telegram_evidence(test_db, monkeypatch, redis):
    from app.api.xboard import CallbackRequest, _verified_callback_account
    from app.modules.private_chat.models import PrivateChatConversation, PrivateChatMessage
    _svc, account, *_ = await setup(test_db, monkeypatch, redis)
    payload = CallbackRequest(callback_id="callback", trace_id="trace", event_type="user.registered",
        tg_user_id=42, occurred_at=datetime.now(UTC), payload={"account_id": account.id})
    assert await _verified_callback_account(test_db, payload) is None
    conversation = PrivateChatConversation(account_id=account.id, peer_telegram_id=42)
    test_db.add(conversation)
    await test_db.flush()
    test_db.add(PrivateChatMessage(conversation_id=conversation.id, account_id=account.id,
        peer_telegram_id=42, telegram_message_id=123, direction="inbound", source="telegram",
        status="received", occurred_at=datetime.utcnow()-timedelta(minutes=1)))
    await test_db.commit()
    assert await _verified_callback_account(test_db, payload) == account.id
    payload.payload = {"account_id": account.id + 1}
    assert await _verified_callback_account(test_db, payload) is None


@pytest.mark.asyncio
async def test_unknown_join_result_still_reserves_target_for_other_accounts(test_db, monkeypatch, redis):
    from app.modules.acquisition.join_claims import group_occupied
    from app.modules.acquisition.models import AutoJoinAttempt
    _svc, account, member, *_ = await setup(test_db, monkeypatch, redis)
    member.status, member.left_at = "left", datetime.utcnow()
    member.ad_status = "blocked"
    test_db.add(AutoJoinAttempt(account_id=account.id, group_id=member.group_id,
        telegram_group_id=member.telegram_group_id, status="unknown", telegram_action_attempted=True,
        request_state="completed", reconciliation_status="disabled"))
    await test_db.commit()
    assert await test_db.scalar(select(Group.id).where(Group.id == member.group_id, group_occupied(datetime.utcnow())))


@pytest.mark.asyncio
async def test_sdk_boundary_paces_every_read_in_batch(monkeypatch, redis):
    from telethon.tl.functions.contacts import ResolveUsernameRequest

    from app.core.account import rpc_pacing
    from app.core.account.rpc_governor import RpcGovernor, install_governor
    monkeypatch.setattr(settings, "TELEGRAM_RPC_PACING_ENABLED", True)
    monkeypatch.setattr(settings, "TELEGRAM_RPC_MIN_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr("app.core.redis.get_redis", AsyncMock(return_value=redis))
    monkeypatch.setattr(rpc_pacing, "restore_cooldown", AsyncMock())
    admission_due = []
    async def sdk(_sender, request, **_kwargs):
        assert not isinstance(request, list)
        # Windows loop.time() is coarser than the Redis millisecond clock.
        # Observe the admission at each actual SDK boundary instead.
        admission_due.append(int(await redis.get("vanguard:rpc:pace:7")))
        return request.username
    client = NS(_call=sdk)
    install_governor(client, RpcGovernor(7, lambda: "growth_permission"))
    outcome = await client._call(None, [ResolveUsernameRequest("first"), ResolveUsernameRequest("second")])
    assert outcome == ["first", "second"]
    assert admission_due[1] - admission_due[0] >= 20


@pytest.mark.asyncio
async def test_previous_dispatch_ticket_cannot_start_new_version(monkeypatch, redis):
    from app.core.scheduler import growth_dispatch
    monkeypatch.setattr(growth_dispatch, "PREFIX", "vanguard:growth_dispatch:v1:")
    await redis.set(growth_dispatch.ticket_key("ads", 7), "q:old", ex=120)
    monkeypatch.setattr(growth_dispatch, "PREFIX", "vanguard:growth_dispatch:v2:")
    assert await growth_dispatch.start(redis, "ads", 7, "old", datetime.now(UTC).timestamp()) == 0
