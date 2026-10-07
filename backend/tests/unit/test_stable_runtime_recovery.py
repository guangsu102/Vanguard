"""Failure-driven coverage for transport recovery and offline business progress."""

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock, Mock

import pytest
from telethon.tl.functions.updates import GetStateRequest

from app.core.account import event_inbox as inbox
from app.core.account import rpc_governor as rpc
from app.core.account.listener_storage import stored_facts
from app.core.account.rpc_budget_policy import read_lane
from tests.unit.test_ad_read_reserve import mock_usage


def test_listener_control_reserve_cannot_be_used_for_scans_or_bulk_catchup():
    assert read_lane(["users.GetUsersRequest"], "growth_listener") == "listener"
    assert read_lane(["updates.GetStateRequest"], "growth_listener_refresh") == "listener"
    for method in [
        "messages.GetHistoryRequest",
        "updates.GetDifferenceRequest",
        "channels.GetMessagesRequest",
    ]:
        assert read_lane([method], "growth_listener") == "sync"
    assert read_lane(["users.GetUsersRequest"], "group_qualification") == "critical"


@pytest.mark.asyncio
async def test_coldstart_survives_sync_exhaustion_but_obeys_its_cap(monkeypatch, test_db):
    mock_usage(monkeypatch, {"hour": (156, 1000), "day": (1560, 50000)})
    current = await rpc.check_read_ready(
        test_db, 2, purpose="growth_listener", requires_bootstrap=True
    )
    assert current["lanes"]["listener"]["remaining"] == 5
    assert current["lanes"]["sync"]["remaining"] == 0
    assert current["legacy_attribution"]["hour"]["unclassified"] == 156
    mock_usage(
        monkeypatch,
        {
            "hour": (161, 1000),
            "day": (1565, 50000),
            "survival_hour": (5, 1000),
            "survival_day": (5, 50000),
            "listener_hour": (5, 1000),
            "listener_day": (5, 50000),
        },
    )
    with pytest.raises(rpc.RpcDeferred):
        await rpc.check_read_ready(test_db, 2, purpose="growth_listener", requires_bootstrap=True)


@pytest.mark.asyncio
async def test_control_wait_keeps_transport_and_retries_only_pre_rpc(monkeypatch):
    from app.core.account import listener_budget_wait as wait

    governor = Obj(
        before=AsyncMock(side_effect=[rpc.RpcDeferred("telegram_read_budget", 90), None]),
        failed=AsyncMock(),
        succeeded=AsyncMock(),
        account_id=2,
    )
    original = AsyncMock(return_value=Obj())
    pause = Obj(request=Mock())
    client = Obj(
        _call=original, _updates_handle=asyncio.current_task(), _vanguard_listener_pause=pause
    )
    sleep = AsyncMock()
    monkeypatch.setattr(wait, "wait_global", sleep)
    monkeypatch.setattr(wait, "record_sync_result", AsyncMock())
    rpc.install_governor(client, governor)
    await client._call(None, GetStateRequest())
    original.assert_awaited_once()
    pause.request.assert_not_called()
    sleep.assert_awaited_once()
    assert governor.before.await_count == 2


def test_disk_replay_ack_is_versioned_and_never_creates_session(tmp_path):
    path = tmp_path / "missing.session"
    assert stored_facts(path) == [] and not path.exists()
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE vanguard_listener_facts (fact_key TEXT PRIMARY KEY,value TEXT NOT NULL)"
        )
        db.execute("INSERT INTO vanguard_listener_facts VALUES (?,?)", ("deleted:1", "old"))
    rows = stored_facts(path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE vanguard_listener_facts SET value=?", ("new",))
    stored_facts(path, acknowledge=rows)
    assert stored_facts(path) == [("deleted:1", "new")]
    stored_facts(path, acknowledge=stored_facts(path))
    assert stored_facts(path) == []


@pytest.mark.asyncio
async def test_offline_claim_only_consumes_local_facts(test_db):
    from app.core.account.listener_facts import enqueue_fact
    from tests.unit.test_growth_event_inbox import account, event

    await account(test_db)
    await inbox.enqueue(test_db, 2, "message", event())
    await enqueue_fact(
        test_db,
        2,
        {"key": "deleted:-100123:9", "kind": "deleted", "peer": -100123, "message_id": 9, "at": 1},
    )
    await test_db.commit()
    row = await inbox.claim(test_db, [], local_account_ids=[2])
    assert row.kind == "listener_fact"
    await inbox.finish(test_db, row.id)
    assert await inbox.claim(test_db, [], local_account_ids=[2]) is None
    assert (await inbox.claim(test_db, [2])).kind == "message"


@pytest.mark.asyncio
async def test_cold_worker_projects_committed_disk_facts_without_connect(monkeypatch):
    from app.core.account import listener_facts
    from app.workers import telegram_worker as module

    rows = [("fact", json.dumps({"kind": "deleted"}))]
    reader = AsyncMock(return_value=rows)
    pool = Obj(
        get_account_by_id=AsyncMock(return_value=Obj(client=None)), listener_stored_facts=reader
    )
    worker = module.TelegramWorker(module.TelegramWorkerRole.GROWTH_USER)
    worker._account_pool = pool
    worker._growth_runtime_account_ids = {2}
    calls = []

    @asynccontextmanager
    async def session():
        yield object()
        calls.append("commit")

    persist = AsyncMock(side_effect=lambda *a: calls.append("enqueue"))
    monkeypatch.setattr(module, "get_db_session", session)
    monkeypatch.setattr(listener_facts, "enqueue_fact", persist)
    await worker._project_growth_listener_facts()
    assert calls == ["enqueue", "commit"]
    reader.assert_any_await(2, acknowledge=rows)
    assert worker._growth_listener_sessions == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [True, False])
async def test_local_ai_skips_read_gate_only_with_fresh_evidence(test_db, monkeypatch, failure):
    from app.core.account import read_schedule
    from app.modules.acquisition import qualification_service as service
    from tests.unit.test_qualification_service import setup

    account, group, member, row = await setup(test_db)
    now = datetime.utcnow()
    data = json.loads(row.evidence_json)
    data.update(
        ai_pending=True, collected_at=(now - timedelta(hours=25 if failure else 1)).isoformat()
    )
    row.evidence_json = json.dumps(data)
    row.state = "waiting_ai"
    row.next_retry_at = now - timedelta(seconds=1)
    await test_db.commit()
    gate = AsyncMock(return_value=("telegram_read_budget", now + timedelta(hours=2)))
    monkeypatch.setattr(read_schedule, "read_wait", gate)
    invoked = []

    async def assess(*a, **kw):
        invoked.append(kw["row"].id)
        # Stop before follow-up business actions: this test exercises claiming.
        raise ValueError("local_ai_claimed")

    monkeypatch.setattr(service, "assess", assess)
    if failure:
        assert (await service.run_reviews(Obj(db=test_db)))["processed"] == 0
        gate.assert_awaited_once()
        assert not invoked
    else:
        result = await service.run_reviews(Obj(db=test_db))
        assert result["failed"] == 1
        assert result["results"][0]["reason"] == "review_exception:ValueError"
        assert row.state == "completed" and row.next_retry_at > now
        gate.assert_not_awaited()
        assert invoked == [row.id]


@pytest.mark.parametrize(
    "change",
    [
        {"invalidated_at": "now"},
        {"technical_errors": ["TimeoutError"]},
        {"profile_fingerprint": "changed"},
        {"policy_version": -1},
        {"collected_at": (datetime.utcnow() + timedelta(hours=1)).isoformat()},
    ],
)
def test_reusable_ai_evidence_fails_closed_on_changed_or_invalid_data(change):
    from app.modules.acquisition import qualification_service as service
    from app.modules.acquisition.group_qualification import POLICY_VERSION
    from app.modules.acquisition.qualification_ai import profile_fingerprint

    account = Obj(ad_profile_text=None)
    now = datetime.utcnow()
    data = {
        "ai_pending": True,
        "quality_status": "qualified",
        "collected_at": now.isoformat(),
        "policy_version": POLICY_VERSION,
        "profile_fingerprint": profile_fingerprint(account),
    }
    assert service.ai_evidence_reusable(data, account, now)
    assert not service.ai_evidence_reusable({**data, **change}, account, now)
