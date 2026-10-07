from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from app.core.account import rpc_governor as rpc


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'purpose,bootstrap,expected_wait',
    [
        ('group_qualification', True, None),
        ('growth_listener', True, None),
        ('group_qualification', False, None),
        ('join_candidate_preview', True, None),
        ('ad_survival_check', True, None),
        ('ad_delivery', True, None),
        ('ad_result_reconciliation', True, None),
        ('join_request_reconciliation', True, None),
    ],
)
async def test_bootstrap_uses_existing_method_lane_without_consuming_budget(
    monkeypatch, purpose, bootstrap, expected_wait,
):
    snapshot = AsyncMock(return_value={
        'state': 'recovering', 'reason': None, 'retry_after_seconds': 0,
        'lanes': {
            'routine': {'remaining': 29, 'retry_after_seconds': 0},
            'background': {'remaining': 18, 'retry_after_seconds': 0},
            'critical': {'remaining': 59, 'retry_after_seconds': 0},
            'ad': {'remaining': 84, 'retry_after_seconds': 0},
            'listener': {'remaining': 5, 'retry_after_seconds': 0},
            'sync': {'remaining': 0, 'retry_after_seconds': 2400},
        },
    })
    monkeypatch.setattr(rpc, 'snapshot', snapshot)
    now, db = datetime.utcnow(), object()
    if expected_wait:
        with pytest.raises(rpc.RpcDeferred) as caught:
            await rpc.check_read_ready(db, 2, now, purpose=purpose, requires_bootstrap=bootstrap)
        assert (caught.value.reason, caught.value.retry_after_seconds) == (
            'telegram_read_budget', expected_wait,
        )
    else:
        await rpc.check_read_ready(db, 2, now, purpose=purpose, requires_bootstrap=bootstrap)
    snapshot.assert_awaited_once_with(db, 2, now)


@pytest.mark.asyncio
async def test_bootstrap_precheck_preserves_official_cooldown(monkeypatch):
    monkeypatch.setattr(rpc, 'snapshot', AsyncMock(return_value={
        'state': 'cooldown', 'reason': 'telegram_rpc_cooldown', 'retry_after_seconds': 7200,
        'lanes': {'routine': {'retry_after_seconds': 7200}, 'sync': {'retry_after_seconds': 2400}},
    }))
    with pytest.raises(rpc.RpcDeferred) as caught:
        await rpc.check_read_ready(object(), 2, purpose='group_qualification', requires_bootstrap=True)
    assert (caught.value.reason, caught.value.retry_after_seconds) == ('telegram_rpc_cooldown', 7200)
