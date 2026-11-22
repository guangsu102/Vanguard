"""Exercise the public local service entry over the real R18 budget chain."""
import json
from datetime import datetime
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest
from telethon.tl.functions.messages import GetChatsRequest
from telethon.tl.functions.users import GetUsersRequest
from telethon.tl.types import Chat, ChatPhotoEmpty, InputUserSelf

from app.core.account import read_work, rpc_governor as rpc
from app.core.account.control_models import AccountBudgetReservation
from app.modules.acquisition import qualification_engine as engine
from app.modules.acquisition import qualification_service as service
from app.modules.acquisition.automation import AcquisitionAutomationService
from .test_join_floor_chain import chain_services, client_with_governor, request
from .test_read_budget_class import lua_store, seed_production_windows
from .test_initial_join_provenance import isolated_rollout, joined


def service_fixture(db, account_id, group, monkeypatch, *, mismatch=False):
    """Fake only network answers and the final collector snapshot."""
    client, _ = client_with_governor(account_id, Obj(value="group_qualification"))
    entity = Chat(
        id=group.group_id + int(mismatch), title="service-entry",
        photo=ChatPhotoEmpty(), participants_count=100,
        date=datetime.utcnow(), version=1,
    )
    observed = {"entity": [], "me": [], "collector": []}

    async def get_entity(target):
        active = read_work.current(account_id)
        observed["entity"].append(active)
        await client._call(None, GetChatsRequest([entity.id]))
        return entity

    async def get_me():
        observed["me"].append(read_work.current(account_id))
        await client._call(None, GetUsersRequest([InputUserSelf()]))
        return Obj(id=200)

    client.get_entity = get_entity
    client.get_me = get_me
    client.send_message = AsyncMock()
    client.send_file = AsyncMock()
    pool = Obj(
        add_account_from_db=AsyncMock(),
        acquire_by_id=AsyncMock(return_value=Obj(client=client)),
        release=AsyncMock(),
    )

    class Collector:
        def __init__(self, *args, **kwargs):
            pass

        async def collect(self, entity, *, now):
            active = read_work.current(account_id)
            observed["collector"].append(active)
            # The actual local for_review/apply path must have installed the
            # bounded plan before entity/self identity entered the SDK.
            assert active.kind == "review"
            assert active.budget_workload == "join"
            assert active.planned_reads == active.limit == 8
            assert active.reads == active.entered_reads == 2
            for _ in range(6):
                await client._call(None, request())
            with pytest.raises(rpc.RpcDeferred, match="telegram_read_slice"):
                await client._call(None, request())
            assert active.reads == active.entered_reads == 8
            return {
                "quality_status": "technical_wait",
                "quality_reason": "evidence_incomplete",
                "technical_errors": ["evidence_incomplete"],
                "collection_complete": False,
                "collection_progress": {"pending_message_ids": [501]},
                "permissions": {"member": True, "can_send_text": True},
                "evidence": [],
            }

    monkeypatch.setattr(service, "EvidenceCollector", Collector)
    # The diagnostic writer is a telemetry sink, outside business admission.
    monkeypatch.setattr(service, "record_technical_diagnostic", lambda *args: None)
    fake = Obj(
        db=db, account_pool=pool,
        _join_review_account_block_reason=lambda *_: None,
        _telegram_entity_matches_group_id=lambda actual, expected:
            AcquisitionAutomationService._telegram_entity_matches_group_id(None, actual, expected),
    )
    return fake, client, observed


def assert_no_authorization(result, draft, client):
    assert not result.passed and not result.should_leave
    assert result.ad_allowed is None
    assert draft.decision == "technical_wait"
    client.send_message.assert_not_called()
    client.send_file.assert_not_called()


@pytest.mark.asyncio
async def test_public_local_assess_binds_first_slice_and_next_entry_uses_review(
    test_db, chain_services, monkeypatch,
):
    store = chain_services
    account, group, _, _, _, durable, claim = await joined(test_db)
    account_id, group_id = account.id, group.id
    await seed_production_windows(store)
    draft = await engine._draft(test_db, claim)
    fake, client, observed = service_fixture(test_db, account_id, group, monkeypatch)

    result = await service.assess(fake, account_id, group, row=draft, force_refresh=True)
    assert_no_authorization(result, draft, client)
    assert len(observed["entity"]) == len(observed["me"]) == len(observed["collector"]) == 1
    first = observed["collector"][0]
    assert first.kind == "review" and first.budget_claim == claim
    assert first.phase == "first" and first.planned_reads == first.limit == 8
    assert first.reads == first.entered_reads == client.sdk_requests == 8
    assert json.loads(durable.progress_json)["join_initial_budget"]["first_read_token"] == first.token
    assert not durable.initial_collection_pending
    ledger = await test_db.get(AccountBudgetReservation, first.ledger_id, populate_existing=True)
    assert (ledger.state, ledger.workload, ledger.consumed_reads) == ("settled", "review", 8)
    payload = json.loads(draft.evidence_json)
    assert payload["read_plan"]["budget_workload"] == "join"
    assert payload["read_plan"]["required_reads"] == 8
    assert payload["read_cost"]["kind"] == "review"
    assert await store.get("vanguard:rpc:2:critical_review_hour") == "54"
    assert await store.get("vanguard:rpc:2:critical_review_day") == "540"
    assert await store.get("vanguard:rpc:2:critical_join_day") == "8"

    second = await engine._draft(test_db, claim)
    later = await service.assess(fake, account_id, group, row=second, force_refresh=True)
    assert_no_authorization(later, second, client)
    assert len(observed["entity"]) == 2
    assert len(observed["me"]) == len(observed["collector"]) == 1
    later_work = observed["entity"][-1]
    assert later_work.token != first.token
    assert later_work.budget_workload is None and later_work.budget_claim is None
    assert later_work.reads == later_work.entered_reads == 0
    assert client.sdk_requests == 8
    assert second.reason == "telegram_read_budget"
    assert json.loads(second.evidence_json)["read_plan"]["budget_workload"] == "review"
    assert await store.get("vanguard:rpc:2:critical_join_day") == "8"
    assert not await engine.initial_join_budget_current(test_db, claim, account_id, group_id, work_token="next")


@pytest.mark.asyncio
async def test_public_local_assess_identity_mismatch_stops_before_self_or_collector(
    test_db, chain_services, monkeypatch,
):
    store = chain_services
    account, group, _, _, _, _, claim = await joined(test_db)
    account_id = account.id
    await seed_production_windows(store)
    draft = await engine._draft(test_db, claim)
    fake, client, observed = service_fixture(test_db, account_id, group, monkeypatch, mismatch=True)

    result = await service.assess(fake, account_id, group, row=draft, force_refresh=True)
    assert_no_authorization(result, draft, client)
    assert draft.reason == "group_identity_mismatch"
    assert len(observed["entity"]) == 1 and not observed["me"] and not observed["collector"]
    first = observed["entity"][0]
    assert first.kind == "review" and first.budget_workload == "join"
    assert first.planned_reads == first.limit == 8
    assert first.reads == first.entered_reads == client.sdk_requests == 1
    ledger = await test_db.get(AccountBudgetReservation, first.ledger_id, populate_existing=True)
    assert (ledger.state, ledger.workload, ledger.consumed_reads) == ("settled", "review", 1)
    assert await store.get("vanguard:rpc:2:critical_join_day") == "1"
    assert await store.get("vanguard:rpc:2:critical_review_day") == "540"
