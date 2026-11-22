from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock
from sqlalchemy import and_
import pytest
from app.modules.acquisition import growth_efficiency_overview as api
from app.modules.acquisition import capacity,qualification_service
from app.modules.guardian import models as guardian_models

NOW=datetime(2026,11,6,1)
class DB:
    def __init__(self,account,config,rows=()):self.account,self.config,self.rows=account,config,rows;self.maintenance_query=None
    @asynccontextmanager
    async def begin_nested(self):yield
    async def get(self,*args):return self.account
    async def scalar(self,*args):return self.config
    async def scalars(self,query):
        self.maintenance_query=str(and_(*query._where_criteria).compile(compile_kwargs={'literal_binds':True}))
        return Obj(all=lambda:list(self.rows))
    def commit(self):raise AssertionError('readiness must not commit')
    def add(self,*args):raise AssertionError('readiness must not create work')

@pytest.mark.asyncio
async def test_reuses_local_gates_and_separates_maintenance_without_writes(monkeypatch):
    mock=AsyncMock(return_value={'ad_targets':3,'join_candidates':2,'sample_complete':True,'blocker_counts':{'send_unknown':1}})
    monkeypatch.setattr(capacity,'executable_inventory',mock)
    monkeypatch.setattr(qualification_service,'account_block_reason',lambda *args:None)
    row=Obj(id=9,group_id=7,state='WAIT_EVENT',reason='business_evidence_missing',next_step_reads=0,ready_at=None,updated_at=NOW)
    db=DB(Obj(id=33),Obj(enabled=True,auto_join_enabled=True),[row])
    value=await api.overview(db,33,NOW)
    assert value['ad_targets']==3 and value['join_candidates']==2
    assert value['blocker_counts']=={'send_unknown':1}
    assert value['maintenance']['items'][0]['id']==9 and value['maintenance']['blocks_qualification'] is False
    assert "'business_maintenance'" in db.maintenance_query
    mock.assert_awaited_once_with(db,33,db.config,NOW,include_candidates=True)

@pytest.mark.asyncio
async def test_real_zero_and_unknown_are_distinct(monkeypatch):
    monkeypatch.setattr(capacity,'executable_inventory',AsyncMock(return_value={'ad_targets':0,'join_candidates':0,'sample_complete':False}))
    monkeypatch.setattr(qualification_service,'account_block_reason',lambda *args:None)
    db=DB(Obj(id=33),Obj(enabled=True,auto_join_enabled=True))
    known=await api.overview(db,33,NOW)
    assert known['available'] and known['ad_targets']==0 and known['join_candidates']==0 and not known['sample_complete']
    monkeypatch.setattr(capacity,'executable_inventory',AsyncMock(side_effect=RuntimeError('missing local evidence')))
    unknown=await api.overview(db,33,NOW)
    assert not unknown['available'] and unknown['ad_targets'] is None and unknown['join_candidates'] is None

@pytest.mark.asyncio
async def test_hard_account_hold_blocks_execution_without_erasing_candidate_inventory(monkeypatch):
    monkeypatch.setattr(capacity,'executable_inventory',AsyncMock(return_value={'ad_targets':3,'join_candidates':2,'sample_complete':True}))
    monkeypatch.setattr(qualification_service,'account_block_reason',lambda *args:'account_inactive')
    db=DB(Obj(id=33),Obj(enabled=True,auto_join_enabled=False))
    value=await api.overview(db,33,NOW)
    assert value['ad_targets']==0 and value['join_candidates']==2
    assert value['blocker_counts']['account_inactive']==1 and value['blocker_counts']['account_auto_join_disabled']==1

@pytest.mark.asyncio
async def test_missing_config_never_fabricates_empty_inventory(monkeypatch):
    mock=AsyncMock(side_effect=AssertionError('must not compute without config'))
    monkeypatch.setattr(capacity,'executable_inventory',mock)
    result=await api.overview(DB(Obj(id=33),None),33,NOW)
    assert result['available'] is False and result['reason']=='account_configuration_missing'
    assert result['ad_targets'] is None
    mock.assert_not_awaited()
