from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.group.collection import collection_for_groups, record_snapshot, sync_stale_groups
from app.core.group.models import Group
from app.modules.acquisition.ad_readiness import due_membership_query, schedule_readiness
from app.modules.acquisition.automation import AcquisitionAutomationService
from app.modules.acquisition.candidate_preview import CandidatePreview, public_candidate_preview
from app.modules.acquisition.models import AdDeliveryScheduleState
from tests.unit.modules.acquisition.test_candidate_preview import FakeClient
from tests.unit.test_dynamic_outbound_capacity import NOW, account_config, add_member

pytestmark = pytest.mark.asyncio


async def test_shared_cooldown_uses_exact_redis_deadline(test_db):
    account, config = await account_config(test_db)
    service = AcquisitionAutomationService(test_db, account_pool=SimpleNamespace())
    due = NOW + timedelta(minutes=23)
    service._get_ad_delivery_cooldown_until = AsyncMock(
        return_value=due.replace(tzinfo=UTC).timestamp()
    )
    ready = await service._ad_account_throttle_readiness(config, NOW)
    assert ready.reason == "account_ad_cooldown" and ready.next_allowed_at == due
    assert (
        await service._ad_account_throttle_skip_reason(config, NOW, delivery_policy="growth")
        == ready.reason
    )
    assert (await service._ad_account_throttle_readiness(config, due)).reason is None
    service._get_ad_delivery_cooldown_until.side_effect = ConnectionError
    assert (
        await service._ad_account_throttle_readiness(config, NOW)
    ).reason == "account_ad_cooldown_unavailable"


async def test_due_query_excludes_future_but_recovers_expired_lease(test_db):
    account, _ = await account_config(test_db)
    m1, _ = await add_member(test_db, account.id, 30, decision="allowed", review="approved")
    m2, _ = await add_member(test_db, account.id, 31, decision="allowed", review="approved")
    from app.modules.acquisition.models import AdCampaign

    campaign = AdCampaign(name="due")
    test_db.add(campaign)
    await test_db.flush()
    state = AdDeliveryScheduleState(
        account_id=account.id,
        group_id=m1.group_id,
        telegram_group_id=m1.telegram_group_id,
        campaign_id=campaign.id,
        status="idle",
        next_due_at=NOW + timedelta(hours=3),
    )
    test_db.add(state)
    await test_db.commit()
    assert [
        m.id
        for m in (await test_db.scalars(due_membership_query(account.id, campaign.id, NOW))).all()
    ] == [m2.id]
    assert schedule_readiness(state, NOW).next_allowed_at == state.next_due_at
    state.status = "sending"
    state.next_due_at = NOW
    state.lease_expires_at = NOW - timedelta(seconds=1)
    await test_db.commit()
    assert (
        len((await test_db.scalars(due_membership_query(account.id, campaign.id, NOW))).all()) == 2
    )
    assert schedule_readiness(state, NOW).reason is None
    state.status = "paused"
    await test_db.commit()
    assert (
        len((await test_db.scalars(due_membership_query(account.id, campaign.id, NOW))).all()) == 1
    )


async def test_verified_collection_survives_failure_and_older_snapshot(test_db):
    g = Group(group_id=-10000123333, member_count=800)
    test_db.add(g)
    await test_db.commit()
    snap = {
        "collected_at": datetime.utcnow().isoformat(),
        "member_count": 1200,
        "member_count_verified": True,
        "member_count_source": "full_chat.participants_count",
        "online_count": 0,
        "online_count_source": "messages.getOnlines",
    }
    assert await record_snapshot(test_db, g, snap, source="qualification")
    await test_db.commit()
    info = (await collection_for_groups(test_db, [g.group_id]))[g.group_id]
    assert g.member_count == 1200 and info["online_count"] == 0 and info["status"] == "fresh"
    old = dict(
        snap, collected_at=(datetime.utcnow() - timedelta(days=2)).isoformat(), member_count=20
    )
    assert not await record_snapshot(test_db, g, old, source="sync")
    assert g.member_count == 1200
    await record_snapshot(test_db, g, {}, source="sync", error="TimeoutError")
    await test_db.commit()
    info = (await collection_for_groups(test_db, [g.group_id]))[g.group_id]
    assert info["stale"] and info["member_count"] == 1200 and info["error"] == "TimeoutError"


async def test_sync_empty_is_explicit_skip(test_db):
    result = await sync_stale_groups(test_db, pool=SimpleNamespace())
    assert (
        result["checked"] == 0
        and result["status"] == "skipped"
        and result["reason"] == "no_stale_groups_due"
    )


async def test_preview_count_live_source_beats_cached_count():

    async def full(_):
        return SimpleNamespace(full_chat=SimpleNamespace(participants_count=49, about="允许广告"))

    class Small(FakeClient):
        async def __call__(self, request):
            return await full(request)

    result = await public_candidate_preview(
        Small(),
        SimpleNamespace(username="test", group_id=-1000000000123),
        own_user_ids={9},
        identity_coverage=True,
    )
    assert result.member_count == 49 and result.exclusion_reason == "prejoin_members_below_50"
    assert CandidatePreview(status="sampled", member_count=50).exclusion_reason is None
    assert CandidatePreview(
        status="sampled", rule_signal="explicit_ban", online_count=1
    ).exclusion_reason
    assert (
        CandidatePreview(
            status="sampled", rule_signal="explicit_ban", online_count=None
        ).exclusion_reason
        is None
    )
    assert (
        CandidatePreview(status="sampled", rule_signal="explicit_ban", ordinary_advertisers=1).score
        > CandidatePreview(status="sampled", rule_signal="explicit_allow").score
    )


async def test_sync_live_refresh_and_skip_fresh(test_db, monkeypatch):
    from app.modules.acquisition import ad_output_plan
    monkeypatch.setattr(ad_output_plan, "ad_output_plan", AsyncMock(
        return_value={"group_deficit": 0, "join_blocker": None}))
    account, config = await account_config(test_db)
    config.auto_join_enabled = False  # No replenishment task competing with metadata.
    member, _ = await add_member(test_db, account.id, 51, decision="allowed", review="approved")
    g = await test_db.get(Group, member.group_id)
    from app.modules.acquisition.qualification_identity import peer_identity

    peer = peer_identity(g.group_id)

    class Client:
        async def get_entity(self, target):
            return SimpleNamespace(id=peer[0], megagroup=True, title="verified group")

        async def __call__(self, request):
            return SimpleNamespace(
                full_chat=SimpleNamespace(participants_count=1234, online_count=4)
            )

    wrapper = SimpleNamespace(client=Client())
    pool = SimpleNamespace(
        sync_from_db=AsyncMock(), acquire_by_id=AsyncMock(return_value=wrapper), release=AsyncMock()
    )
    result = await sync_stale_groups(test_db, pool=pool)
    assert result["status"] == "updated" and result["updated"] == 1, result
    assert g.member_count == 1234
    pool.release.assert_awaited_once_with(wrapper)
    again = await sync_stale_groups(test_db, pool=pool)
    assert again["checked"] == 0
    assert pool.acquire_by_id.await_count == 1
