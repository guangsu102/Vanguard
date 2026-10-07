from datetime import datetime

import pytest

from app.core.account import rpc_governor as rpc
from app.core.account.reserved_reads import allocations, classified, available
from tests.unit.test_ad_read_reserve import mock_usage


@pytest.mark.parametrize('total', range(30, 241))
def test_reservations_sum_to_cap_and_preserve_joining(total):
    caps = allocations(total)
    assert sum(caps) == total and min(caps) >= 0
    assert caps[0]*100 >= 35*total and caps[2]*100 >= 25*total
    # Synchronization spends its own reserve and all elasticity. Every other
    # protected consumer retains exactly its own floor.
    used = [0, 0, 0, caps[3]+caps[4], 0]
    assert available(total, used, caps, 'sync') == 0
    for lane, index in [('ad', 0), ('survival', 1), ('critical', 2)]:
        assert available(total, used, caps, lane) == caps[index]


@pytest.mark.asyncio
async def test_sync_and_background_cannot_exhaust_join_preflight(monkeypatch, test_db):
    mock_usage(monkeypatch, {'hour': (60, 1500), 'day': (600, 80000),
                            'sync_reserved_hour': (48,1500), 'sync_reserved_day': (480,80000),
                            'flex_reserved_hour': (12,1500), 'flex_reserved_day': (120,80000)})
    for purpose in ['auto_join', 'join_candidate_preview', 'qualification_verification', 'group_qualification']:
        state = await rpc.check_read_ready(test_db, 3, purpose=purpose)
        assert state['lanes']['critical']['remaining'] == 60
        assert state['lanes']['ad']['remaining'] == 84
    state = await rpc.check_read_ready(test_db, 3, purpose='growth_listener')
    assert state['lanes']['listener']['remaining'] == 5
    assert state['lanes']['sync']['remaining'] == 0


def test_legacy_spend_not_erased_or_fabricated_as_new_join_counter():
    caps = allocations(168)
    used = classified(122, [39,0,0,0,0], caps)
    assert sum(used) == 122
    assert available(168, used, caps, 'ad') == 20
    assert available(168, used, caps, 'critical') == 0
    assert available(168, used, caps, 'survival') == 26
