from datetime import datetime
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock
import pytest
from tests.unit.test_group_qualification import Client
from app.modules.acquisition.group_qualification import EvidenceCollector
from app.modules.acquisition.qualification_service import automatic_exit_reason

@pytest.mark.asyncio
@pytest.mark.parametrize('until,permanent', [(None,True),(datetime(1970,1,1),True),(datetime(2025,1,1),False),(datetime(2027,1,1),False)])
async def test_live_rights_long_term_mute_and_expired_restrictions(until,permanent):
    now=datetime(2026,9,26)
    client=Client([])
    client.get_permissions=AsyncMock(return_value=Obj(is_admin=False,is_creator=False,has_left=False,is_banned=False,participant=Obj(banned_rights=Obj(send_messages=True,until_date=until))))
    collector=EvidenceCollector(client);collector.stop_on_permanent_mute=True
    result=await collector.collect(Obj(id=42,megagroup=True),now=now)
    assert result['permissions']['permanent_send_restriction_verified']==permanent
    assert (automatic_exit_reason(result)=='account_permanent_send_restriction')==permanent
    if permanent:assert result['sampling_stopped_reason']=='confirmed_long_term_send_restriction'
