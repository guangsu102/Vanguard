"""A larger local allowance must not weaken account-specific flood recovery."""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.core.account import rpc_governor as rpc
from app.core.settings_models import SystemSetting
from tests.unit.test_ad_read_reserve import empty_usage


@pytest.mark.parametrize('state', [{}, {'read_factor': 0.9}, {'read_factor': 0.5}, {'read_factor': 0.125}])
def test_all_read_windows_scale_without_changing_account_state(monkeypatch, state):
    original = dict(state)
    now = datetime(2026, 10, 1)
    monkeypatch.setattr(rpc.settings, 'TELEGRAM_READ_BUDGET_PERCENT', 100)
    before = rpc.limits_for(state, now)
    monkeypatch.setattr(rpc.settings, 'TELEGRAM_READ_BUDGET_PERCENT', 150)
    after = rpc.limits_for(state, now)
    for window in ('minute', 'hour', 'day'):
        assert after[window] == before[window] * 3 // 2
    for window in ('hour', 'day'):
        assert sum(after[f'{lane}_{window}'] for lane in ('ad', 'survival', 'critical', 'sync', 'routine')) == after[window]
    assert state == original


@pytest.mark.asyncio
async def test_flood_halves_expanded_budget_and_preserves_recovery_epoch(test_db, monkeypatch):
    monkeypatch.setattr(rpc.settings, 'TELEGRAM_READ_BUDGET_PERCENT', 150)
    now = datetime(2026, 10, 1)
    state = await rpc.record_flood(test_db, 2, 100, 'messages.GetHistoryRequest', 'group_qualification', now)
    await test_db.commit()
    assert state['read_factor'] == 0.5
    assert rpc.limits_for(state, now)['hour'] == 180
    again = await rpc.record_flood(test_db, 2, 10, 'messages.GetHistoryRequest', 'group_qualification', now)
    assert again['read_factor'] == state['read_factor']
    assert again['pause_until'] == state['pause_until']
    assert rpc.limits_for(again, now)['hour'] == 180
    state['last_success_at'] = (now + timedelta(minutes=3)).isoformat()
    assert rpc.limits_for(state, now + timedelta(days=2))['hour'] == 216


@pytest.mark.asyncio
async def test_expansion_does_not_label_recovering_account_as_fully_recovered(test_db, monkeypatch):
    monkeypatch.setattr(rpc.settings, 'TELEGRAM_READ_BUDGET_PERCENT', 150)
    monkeypatch.setattr(rpc, 'read_budget_state', AsyncMock(return_value={
        'usage': empty_usage(), 'blocked_windows': [], 'retry_after_seconds': 0,
        'emergency_cooldown_seconds': 0,
    }))
    test_db.add(SystemSetting(key=rpc.PREFIX + '2', value='{"read_factor": 0.9}'))
    await test_db.commit()
    state = await rpc.snapshot(test_db, 2, datetime(2026, 10, 1))
    assert state['limits']['hour'] == 324
    assert state['state'] == 'recovering'
    assert state['effective_read_factor'] == 0.9
    assert state['budget_percent'] == 150
