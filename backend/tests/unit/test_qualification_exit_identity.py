"""A reviewed Telegram identity cannot change at the actual leave boundary."""
from datetime import UTC, datetime
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest
from telethon.tl.types import Channel, Chat, ChatForbidden, ChatPhotoEmpty, InputChannel

from app.modules.acquisition import automation as automation_module
from app.modules.acquisition import qualification_service
from app.modules.acquisition.automation import AcquisitionAutomationService


def channel(raw=42):
    return Channel(id=raw, title='reviewed', photo=ChatPhotoEmpty(), date=datetime.now(UTC), megagroup=True, username='reviewed')


def basic(raw=42, migrated_to=None):
    return Chat(id=raw, title='reviewed', photo=ChatPhotoEmpty(), participants_count=60,
                date=datetime.now(UTC), version=1, migrated_to=migrated_to)


@pytest.mark.parametrize('entity,expected,allowed', [
    (channel(),42,True), (channel(),-1000000000042,True),
    (basic(),42,True), (basic(),-42,True),
    (ChatForbidden(id=42,title='reviewed'),-42,True),
    (channel(),-42,False), (basic(),-1000000000042,False),
    (channel(99),42,False), (Obj(id=42,megagroup=True),42,False),
    (None,42,False), (basic(migrated_to=InputChannel(77,88)),42,False),
    (channel(77),-1000000000077,True), (channel(77),-42,False),
])
def test_leave_identity_respects_typed_marked_and_migrated_ids(entity, expected, allowed):
    service = AcquisitionAutomationService(Obj())
    assert service._leave_entity_matches_group_id(entity, expected) is allowed


@pytest.mark.asyncio
async def test_username_reassigned_to_another_real_group_is_not_returned():
    client = Obj(get_entity=AsyncMock(return_value=channel(99)))
    service = AcquisitionAutomationService(Obj())
    entity, reason = await service._resolve_group_entity_for_leave(client, Obj(username='reviewed',group_id=42))
    assert entity is None and reason == 'group_identity_mismatch'


@pytest.mark.asyncio
async def test_dialogs_with_same_raw_id_wrong_namespace_are_not_selected():
    async def dialogs():
        yield Obj(entity=channel(42))
        yield Obj(entity=basic(42))
    service = AcquisitionAutomationService(Obj())
    resolved, reason = await service._resolve_group_entity_for_leave(Obj(iter_dialogs=dialogs), Obj(username=None,group_id=-42))
    assert reason is None and isinstance(resolved, Chat)


@pytest.fixture
def boundary(monkeypatch):
    client = Obj(get_permissions=AsyncMock(side_effect=[
        Obj(is_admin=False,is_creator=False,has_left=False), Obj(has_left=True)]))
    wrapper = Obj(client=client)
    pool = Obj(acquire_by_id=AsyncMock(return_value=wrapper), release=AsyncMock())
    db = Obj(scalar=AsyncMock(side_effect=[Obj(id=1),Obj(id=2)]))
    service = AcquisitionAutomationService(db,account_pool=pool)
    service._resolve_group_entity_for_leave = AsyncMock(return_value=(channel(),None))
    service.telegram_execution = Obj(leave_group=AsyncMock())
    authorization = AsyncMock(return_value=(True,'messages_below_5_in_72h'))
    monkeypatch.setattr(qualification_service,'authorize_leave',authorization)
    monkeypatch.setattr(qualification_service,'policy',AsyncMock(return_value={'enabled':True}))
    monkeypatch.setattr(automation_module,'is_owned_group_target',AsyncMock(return_value=False))
    return service,client,authorization


@pytest.mark.asyncio
@pytest.mark.parametrize('entity',[channel(99),Obj(id=42),basic(migrated_to=InputChannel(77,88))])
async def test_final_boundary_rejects_wrong_unknown_or_migrated_entity(boundary,entity):
    service,client,auth=boundary
    service._resolve_group_entity_for_leave.return_value=(entity,None)
    assert await service._leave_group(2,Obj(group_id=42)) == 'group_identity_mismatch'
    service.telegram_execution.leave_group.assert_not_awaited()
    client.get_permissions.assert_not_awaited()
    assert auth.await_count == 1


@pytest.mark.asyncio
async def test_latest_protection_or_review_change_after_lease_blocks(boundary):
    service,client,auth=boundary
    auth.side_effect=[(True,'reject'),(False,'qualification_protected')]
    assert await service._leave_group(2,Obj(group_id=42)) == 'qualification_protected'
    assert auth.await_count == 2 and client.get_permissions.await_count == 1
    service.telegram_execution.leave_group.assert_not_awaited()


@pytest.mark.asyncio
async def test_current_unknown_permission_shape_blocks(boundary):
    service,client,auth=boundary
    client.get_permissions.side_effect=None
    client.get_permissions.return_value=Obj()
    assert await service._leave_group(2,Obj(group_id=42)) == 'qualification_current_permission_unknown'
    service.telegram_execution.leave_group.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_identity_is_reauthorized_then_verified_left(boundary):
    service,client,auth=boundary
    assert await service._leave_group(2,Obj(group_id=42)) is None
    assert auth.await_count == 2 and client.get_permissions.await_count == 2
    service.telegram_execution.leave_group.assert_awaited_once()
