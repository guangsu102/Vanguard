"""Original event age and negative-proof freshness survive repeated page reuse."""
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest

from app.modules.acquisition import qualification_service as service
from app.modules.acquisition import review_history_progress as pages
from tests.conftest import test_db as test_db
from tests.unit.test_group_qualification_sampling import ENTITY
from tests.unit.test_qualification_exit_scope import seed
from tests.unit.test_qualification_service import setup
from tests.unit.test_review_history_progress import env as env, history_calls


@pytest.mark.asyncio
async def test_access_does_not_renew_page_or_original_version_expiry(env, monkeypatch):
    client = env.client([env.msg(100, age=71.5)])
    first = await env.collector(client).collect(ENTITY, now=env.now)
    cursor = first['collection_progress']['history_cursors'][2]
    key = cursor['page_key']
    original = await env.store.load_history_page(34, pages.group_key(ENTITY), key=key)
    observed = original.data['metadata']['observed_at']
    for seconds in (301, 600, 1200):
        monkeypatch.setattr(pages.time, 'time', lambda elapsed=seconds: env.now.replace(tzinfo=UTC).timestamp()+elapsed)
        client.calls.clear()
        current = await env.collector(client, first).collect(ENTITY, now=env.now)
        assert not history_calls(client)
        assert current['history_source_oldest_at'] == env.now.isoformat()
        retained = await env.store.load_history_page(34, pages.group_key(ENTITY), key=key)
        assert retained.data['metadata']['observed_at'] == observed
    monkeypatch.setattr(pages.time, 'time', lambda: env.now.replace(tzinfo=UTC).timestamp()+1801)
    client.calls.clear()
    await env.collector(client, first).collect(ENTITY, now=env.now)
    assert history_calls(client)
    assert not (await env.store.load_history_page(34, pages.group_key(ENTITY), key=key)).ok


@pytest.mark.asyncio
@pytest.mark.parametrize('age_seconds,expected', [(240, True), (301, False), (3600, False)])
async def test_recent_review_cannot_authorize_exit_from_old_collection(test_db, age_seconds, expected):
    group, member, row, _ = await seed(test_db)
    proof = json.loads(row.evidence_json)
    now = datetime.utcnow()
    proof['collected_at'] = (now-timedelta(seconds=age_seconds)).isoformat()
    proof['history_source_oldest_at'] = proof['collected_at']
    row.checked_at = now
    row.evidence_json = json.dumps(proof)
    await test_db.commit()
    allowed, reason = await service.authorize_leave(test_db, 2, group, member)
    assert allowed is expected
    if not expected:
        assert reason == 'qualification_exit_evidence_stale'


@pytest.mark.asyncio
async def test_assess_persists_oldest_history_source_even_after_fresh_roles(test_db, monkeypatch):
    account, group, _, row = await setup(test_db, decision='observe')
    source = datetime.utcnow()-timedelta(hours=1)
    client = Obj(get_entity=AsyncMock(return_value=Obj(id=group.group_id)),
                 get_me=AsyncMock(return_value=Obj(id=200)))
    pool = Obj(add_account_from_db=AsyncMock(),
               acquire_by_id=AsyncMock(return_value=Obj(client=client)), release=AsyncMock())
    collector = Obj(collect=AsyncMock(return_value={
        'quality_status': 'observe', 'quality_reason': 'insufficient_evidence',
        'history_source_oldest_at': source.isoformat(),
        'permissions': {'member': True, 'can_send_text': True}, 'valid_messages': 8,
        'member_count': 100, 'evidence': [], 'unknowns': ['history_hidden'],
    }))
    monkeypatch.setattr(service, 'EvidenceCollector', lambda *args, **kwargs: collector)
    actor = Obj(db=test_db, account_pool=pool,
                _join_review_account_block_reason=lambda *_: None,
                _telegram_entity_matches_group_id=lambda *_: True)
    await service.assess(actor, account.id, group, row=row, force_refresh=True)
    proof = json.loads(row.evidence_json)
    assert proof['collected_at'] == source.isoformat()
    assert proof['history_source_oldest_at'] == source.isoformat()
    assert row.checked_at > source
    assert collector.collect.await_count == 1
    pool.release.assert_awaited_once()
