"""Connection reuse must preserve per-message evidence and durable claims."""

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock, Mock

import pytest
from telethon.tl import types

from app.core.account.rpc_governor import RpcDeferred
from app.modules.acquisition import automation, survival_reads
from app.modules.acquisition.models import AdDeliveryLog
from app.modules.acquisition.survival_reads import SurvivalReadBatch
from tests.unit.test_qualification_automation_lifecycle import case

pytestmark = pytest.mark.asyncio


def pool_case():
    wrapper = Obj(client=Obj(is_connected=Mock(return_value=True)), operation_lease_lost=False)
    pool = Obj(acquire_by_id=AsyncMock(return_value=wrapper), release=AsyncMock())
    return pool, wrapper, SurvivalReadBatch(pool)


async def test_batch_rotates_after_five_reads_and_releases_at_end():
    pool, wrapper, reads = pool_case()
    try:
        for _ in range(6):
            async with reads.borrow(2, "ad_survival_check") as client:
                assert client is wrapper
        assert pool.acquire_by_id.await_count == 2
        assert pool.release.await_count == 1
    finally:
        await reads.close()
    assert pool.release.await_count == 2


@pytest.mark.parametrize("change", ["account", "purpose", "elapsed", "lease_lost", "disconnected"])
async def test_batch_never_reuses_other_account_purpose_or_invalid_connection(change, monkeypatch):
    pool, wrapper, reads = pool_case()
    clock = [0]
    monkeypatch.setattr(survival_reads.time, "monotonic", lambda: clock[0])
    try:
        async with reads.borrow(2, "ad_survival_check"):
            pass
        if change == "elapsed":
            clock[0] = 61
        elif change == "lease_lost":
            wrapper.operation_lease_lost = True
        elif change == "disconnected":
            wrapper.client.is_connected.return_value = False
        async with reads.borrow(
            3 if change == "account" else 2,
            "ad_result_reconciliation" if change == "purpose" else "ad_survival_check",
        ):
            pass
        assert pool.release.await_count == 1
        assert pool.acquire_by_id.await_count == 2
    finally:
        await reads.close()


@pytest.mark.parametrize(
    "failure", [RpcDeferred("telegram_read_budget", 2000), TimeoutError(), asyncio.CancelledError()]
)
async def test_batch_drops_lease_on_defer_timeout_or_cancellation(failure):
    pool, _, reads = pool_case()
    with pytest.raises(type(failure)):
        async with reads.borrow(2, "ad_survival_check"):
            raise failure
    assert reads.wrapper is None
    pool.release.assert_awaited_once()


async def two_logs(db, monkeypatch):
    service, account, group, _, first, now = await case(db)
    monkeypatch.setattr(automation, "_now", lambda: now)
    first.qualification_context_json = json.dumps({"group_type": "supergroup"})
    second = AdDeliveryLog(
        account_id=account.id,
        group_id=group.id,
        telegram_group_id=group.group_id,
        ad_campaign_id=first.ad_campaign_id,
        status="success",
        telegram_message_id=20,
        sent_at=first.sent_at,
        survival_status="pending",
        survival_stage="two_minute",
        survival_check_due_at=now,
        qualification_context_json=first.qualification_context_json,
    )
    db.add(second)
    await db.commit()
    pool, wrapper, _ = pool_case()
    service.account_pool = pool
    return service, wrapper, first, second, now


async def test_worker_reuses_lease_but_reads_every_messages_rights_ttl_and_identity(
    test_db, monkeypatch
):
    service, wrapper, first, second, now = await two_logs(test_db, monkeypatch)
    client = AsyncMock(return_value=Obj(full_chat=Obj(ttl_period=0)))
    client.is_connected = Mock(return_value=True)
    client.get_entity = AsyncMock(
        return_value=types.Channel(
            id=first.telegram_group_id,
            title="current",
            photo=types.ChatPhotoEmpty(),
            date=now,
            megagroup=True,
        )
    )
    client.get_me = AsyncMock(return_value=Obj(id=200, deleted=False))
    client.get_permissions = AsyncMock(
        return_value=Obj(is_admin=False, has_left=False, participant=Obj(banned_rights=None))
    )

    async def message(entity, ids):
        return Obj(id=ids, out=True, date=first.sent_at)

    client.get_messages = AsyncMock(side_effect=message)
    wrapper.client = client
    result = await service.check_ad_survival()
    assert result["processed"] == 2 and result["pending_one_hour"] == 2
    service.account_pool.acquire_by_id.assert_awaited_once()
    service.account_pool.release.assert_awaited_once()
    for method in [
        client.get_entity,
        client.get_me,
        client.get_permissions,
        client.get_messages,
        client,
    ]:
        assert method.await_count == 2
    assert [c.kwargs["ids"] for c in client.get_messages.await_args_list] == [19, 20]
    for log in [first, second]:
        await test_db.refresh(log)
        assert log.survival_claim_token is None and log.survival_stage == "one_hour"
        assert log.survived_two_minute_at == now


async def test_batch_defer_keeps_original_receipt_and_retry_count(test_db, monkeypatch):
    service, _, first, second, now = await two_logs(test_db, monkeypatch)
    second.survival_retry_count = 3
    await test_db.commit()
    service._inspect_ad_survival_facts = AsyncMock(
        side_effect=[
            {
                "exists": True,
                "group_accessible": True,
                "account_readable": True,
                "member": True,
                "can_send": True,
                "ttl_period": 0,
                "errors": [],
            },
            RpcDeferred("telegram_read_budget", 2400),
        ]
    )
    result = await service.check_ad_survival()
    assert result["pending_one_hour"] == 1 and result["deferred"] == 1
    service.account_pool.acquire_by_id.assert_awaited_once()
    service.account_pool.release.assert_awaited_once()
    await test_db.refresh(second)
    assert second.telegram_message_id == 20 and second.status == "success"
    assert second.survival_stage == "two_minute" and second.survival_retry_count == 3
    assert second.survival_check_due_at == now + timedelta(seconds=2401)
    assert second.survival_claim_token is None


async def test_cancelled_worker_releases_batch_without_advancing_unread_message(
    test_db, monkeypatch
):
    service, _, first, _, _ = await two_logs(test_db, monkeypatch)
    service._inspect_ad_survival_facts = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await service.check_ad_survival()
    service.account_pool.release.assert_awaited_once()
    await test_db.refresh(first)
    assert first.survived_two_minute_at is None and first.survival_stage == "two_minute"
