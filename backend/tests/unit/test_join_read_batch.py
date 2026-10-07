import asyncio
from datetime import timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock, Mock
import pytest
from app.core.account.rpc_governor import RpcDeferred
from app.modules.acquisition.models import AutoJoinAttempt
from tests.unit.test_qualification_automation_lifecycle import case

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize('failure', [None, RpcDeferred('telegram_read_budget', 900), asyncio.CancelledError()])
async def test_original_approval_requests_share_one_connection_and_release_on_every_outcome(test_db, monkeypatch, failure):
    from app.core.account import read_schedule
    service, account, group, first, _, now = await case(test_db)
    second = AutoJoinAttempt(account_id=account.id, telegram_group_id=900012,
        target_key='username:another_original', request_state='sent', status='pending',
        telegram_action_attempted=True, request_sent_at=now, attempted_at=now)
    test_db.add(second)
    await test_db.commit()
    ids = [first.id, second.id]
    wrapper = Obj(client=Obj(is_connected=Mock(return_value=True)), operation_lease_lost=False)
    service.account_pool.acquire_by_id = AsyncMock(return_value=wrapper)
    service.telegram_execution = Obj(resolve_join_group_by_link_membership=AsyncMock(side_effect=[None, failure]))
    monkeypatch.setattr(read_schedule, 'read_wait', AsyncMock(return_value=None))
    if isinstance(failure, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await service.reconcile_join_requests()
    else:
        result = await service.reconcile_join_requests()
        assert result['checked'] == 2
    service.account_pool.acquire_by_id.assert_awaited_once_with(account.id,
        purpose='join_request_reconciliation', raise_on_lease_failure=True)
    service.account_pool.release.assert_awaited_once_with(wrapper)
    assert service.telegram_execution.resolve_join_group_by_link_membership.await_count == 2
    for item in [first, second]:
        await test_db.refresh(item)
        assert item.id in ids and item.request_state == 'sent' and item.status == 'pending'
        assert item.reconciliation_status == 'pending'
        assert item.reconciliation_failure_count == 0
    if isinstance(failure, RpcDeferred):
        assert second.reconciliation_next_at >= now + timedelta(seconds=900)
