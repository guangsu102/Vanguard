import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock, Mock

import pytest
from app.core.account.rpc_governor import RpcDeferred
from app.core.account.read_timeout import read_operation_timeout, paced_sleep
from app.modules.acquisition import candidate_inventory as inventory
from app.modules.acquisition.candidate_preview import CandidatePreview, public_candidate_preview
from tests.unit.modules.acquisition.test_candidate_preview import FakeClient

NOW = datetime(2026, 9, 27, 11)
GROUP = Obj(id=1, group_id=-1000000000123, username='offline')


def ready_hint(**kwargs):
    return CandidatePreview('sampled', peer_namespace='channel', member_count=100,
                            rule_signal='explicit_allow', **kwargs)


def test_positive_inventory_survives_48_minute_join_cadence_without_false_renewal():
    fact = inventory.record(GROUP, 2, ready_hint(), NOW)
    assert inventory.fresh(fact, GROUP, 2, NOW + timedelta(minutes=96))
    deferred = inventory.deferred_fact(GROUP, 2, fact, NOW + timedelta(hours=8))
    assert deferred['expires_at'] == fact['expires_at']
    assert not inventory.fresh(deferred, GROUP, 2, NOW + timedelta(hours=3))
    assert not inventory.due(deferred, GROUP, 2, NOW + timedelta(hours=4))
    assert inventory.due(deferred, GROUP, 2, NOW + timedelta(hours=8))


def test_cached_stage_uses_original_collection_time_and_identity():
    fact = inventory.record(GROUP, 2, ready_hint(evidence_collected_at=(NOW-timedelta(hours=2)).isoformat()), NOW)
    assert fact['expires_at'] == (NOW + timedelta(hours=1)).isoformat()
    assert not inventory.fresh(fact, GROUP, 3, NOW)
    assert not inventory.fresh(fact, Obj(group_id=GROUP.group_id, username='renamed'), 2, NOW)


@pytest.mark.asyncio
async def test_staged_preview_reuses_rules_but_rechecks_identity_and_keeps_original_age():
    c = FakeClient(about='允许广告推广')
    saved = []
    async def checkpoint(value): saved.append(value)
    await public_candidate_preview(c, GROUP, own_user_ids=set(), identity_coverage=True, now=NOW, checkpoint=checkpoint)
    assert len(saved) == 1
    c.calls.clear()
    result = await public_candidate_preview(c, GROUP, own_user_ids=set(), identity_coverage=True,
                                           now=NOW+timedelta(hours=1), progress=saved[0])
    assert [kind for kind, _ in c.calls] == ['entity', 'history']
    assert result.evidence_collected_at == NOW.isoformat()
    c.entity.id = 456
    assert (await public_candidate_preview(c, GROUP, own_user_ids=set(), identity_coverage=True,
                                          now=NOW+timedelta(hours=1), progress=saved[0])).status == 'identity_mismatch'


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['entity', 'metadata', 'history'])
async def test_preview_propagates_budget_defer_without_turning_it_into_unknown(stage):
    class Deferred(FakeClient):
        async def get_entity(self, username):
            if stage == 'entity': raise RpcDeferred('telegram_read_budget', 900)
            return await super().get_entity(username)
        async def __call__(self, request):
            if stage == 'metadata': raise RpcDeferred('telegram_read_budget', 900)
            return await super().__call__(request)
        def iter_messages(self, entity, **kwargs):
            async def deferred():
                raise RpcDeferred('telegram_read_budget', 900)
                yield
            return deferred() if stage == 'history' else super().iter_messages(entity, **kwargs)
    with pytest.raises(RpcDeferred):
        await public_candidate_preview(Deferred(), GROUP, own_user_ids=set(), identity_coverage=True)


@pytest.mark.asyncio
async def test_pacing_does_not_consume_network_timeout_but_total_time_is_bounded():
    async with read_operation_timeout(network_seconds=.2, total_seconds=2):
        await paced_sleep(.3)
        await paced_sleep(.3)
    with pytest.raises(TimeoutError):
        async with read_operation_timeout(network_seconds=.05, total_seconds=.1):
            await paced_sleep(.2)
    with pytest.raises(TimeoutError):
        async with read_operation_timeout(network_seconds=.02, total_seconds=.5):
            await asyncio.sleep(.1)

@pytest.mark.asyncio
async def test_ranker_stops_scanning_when_three_fresh_candidates_exist(test_db, monkeypatch):
    from app.modules.acquisition import qualification_service
    from app.modules.acquisition.automation import AcquisitionAutomationService
    monkeypatch.setattr(qualification_service, 'policy', AsyncMock(return_value={'enabled': True}))
    groups = [Obj(id=i, group_id=-(1000000000000+i), username=f'offline{i}') for i in range(1, 10)]
    for group in groups[:3]:
        await inventory.save(test_db, 2, group.id, inventory.record(group, 2, ready_hint(), datetime.utcnow()))
    await test_db.commit()
    service = object.__new__(AcquisitionAutomationService)
    service.db, service.logger = test_db, Mock()
    service.account_pool = Obj(acquire_by_id=AsyncMock(), release=AsyncMock())
    result = await service._rank_pending_join_candidates(2, groups)
    assert len(result) == 3
    service.account_pool.acquire_by_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_ranker_persists_partial_progress_on_defer_then_resumes_without_replacing_evidence(test_db, monkeypatch):
    from app.modules.acquisition import qualification_service, automation
    monkeypatch.setattr(qualification_service, 'policy', AsyncMock(return_value={'enabled': True}))
    attempts = []
    async def preview(client, group, *, checkpoint, progress, **kwargs):
        attempts.append(progress)
        await checkpoint({'checked_at': datetime.utcnow().isoformat(), 'namespace': 'channel', 'entity_id': 123})
        raise RpcDeferred('telegram_read_budget', 600)
    monkeypatch.setattr(automation, 'public_candidate_preview', preview)
    service = object.__new__(automation.AcquisitionAutomationService)
    service.db, service.logger = test_db, Mock()
    service.account_pool = Obj(acquire_by_id=AsyncMock(return_value=Obj(client=Obj())), release=AsyncMock())
    assert await service._rank_pending_join_candidates(2, [GROUP]) == []
    fact = (await inventory.load(test_db, 2, [GROUP.id]))[GROUP.id]
    assert fact['progress']['entity_id'] == 123 and fact['expires_at'] is None
    assert not inventory.fresh(fact, GROUP, 2, datetime.utcnow())
    assert not inventory.due(fact, GROUP, 2, datetime.utcnow())
    assert await service._rank_pending_join_candidates(2, [GROUP]) == []
    assert len(attempts) == 1
    service.account_pool.release.assert_awaited_once()

@pytest.mark.asyncio
async def test_identity_bootstrap_defer_sets_one_account_wait_and_stops_scanning(test_db, monkeypatch):
    from app.modules.acquisition import qualification_service, automation
    from app.core.settings_models import SystemSetting
    from sqlalchemy import select, func
    monkeypatch.setattr(qualification_service, 'policy', AsyncMock(return_value={'enabled': True, 'system_account_user_ids': {'2': 99}}))
    preview = AsyncMock()
    monkeypatch.setattr(automation, 'public_candidate_preview', preview)
    service = object.__new__(automation.AcquisitionAutomationService)
    service.db, service.logger = test_db, Mock()
    service.account_pool = Obj(acquire_by_id=AsyncMock(return_value=Obj(client=Obj(
        get_me=AsyncMock(side_effect=RpcDeferred('telegram_read_budget', 600))))), release=AsyncMock())
    assert await service._rank_pending_join_candidates(2, [GROUP]) == []
    preview.assert_not_awaited()
    assert await inventory.account_retry_at(test_db, 2) > datetime.utcnow()
    assert await test_db.scalar(select(func.count()).select_from(SystemSetting)) == 1
    service.account_pool.release.assert_awaited_once()
