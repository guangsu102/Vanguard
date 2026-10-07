"""Rule cues and interruptible renewal preserve evidence and finish bounded work."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest
from telethon import errors, types
from telethon.tl.functions.channels import GetParticipantRequest, GetParticipantsRequest

from app.core.account.listener_interest import project
from app.core.account.rpc_governor import RpcDeferred
from app.core.account.telegram_execution import TelegramExecutionError
from app.modules.acquisition import qualification_actions as actions
from app.modules.acquisition.renewal_progress import administrator_ids, resume
from tests.unit.test_qualification_actions import Client, live_auth  # noqa: F401
from tests.unit.test_qualification_light_renewal import fixture
from tests.unit.test_selective_listener import PEER, chatter, policy


def test_ordinary_promotion_does_not_revoke_grant_but_rule_and_watched_edit_do():
    item = chatter(22)
    message = item.updates[0].message
    message.message = '广告推广服务，低价套餐，联系 @example'
    assert project(item, policy())[2] == []
    message.message = '本群禁止广告，违者封禁'
    assert project(item, policy())[2][0]['kind'] == 'rules'
    message.message = '已编辑为普通内容'
    p = policy(groups={PEER: {'watched_evidence': [22], 'joined_at': '2026-01-01T00:00:00'}})
    assert project(item, p)[2][0]['kind'] == 'rules'


def test_admin_role_change_invalidates_observations_but_member_join_does_not():
    now = datetime.now(UTC)
    update = types.UpdateChannelParticipant(channel_id=22, date=now, actor_id=55, user_id=44,
        qts=1, prev_participant=None, new_participant=types.ChannelParticipant(44, now))
    envelope = types.UpdateShort(update, now)
    assert project(envelope, policy())[2] == []
    update.new_participant = types.ChannelParticipantCreator(44, types.ChatAdminRights())
    assert project(envelope, policy())[2][0]['kind'] == 'rules'
    update.prev_participant, update.new_participant = update.new_participant, None
    assert project(envelope, policy())[2][0]['kind'] == 'rules'


def test_progress_is_scoped_and_expires_while_queue_flag_alone_is_not_new_evidence():
    now = datetime.utcnow()
    member = Obj(id=60, joined_at=now - timedelta(days=3))
    event = {'kinds': ['gap'], 'token': 'one', 'queued': False}
    previous = {'policy_version': 'policy', 'profile_fingerprint': 'profile'}
    progress = resume(previous, event, member, now)
    progress['checked']['22'] = 'signature'
    previous['renewal_progress'] = progress
    assert resume(previous, {**event, 'queued': True}, member, now)['checked']
    assert not resume(previous, {**event, 'token': 'two'}, member, now)['checked']
    assert not resume(previous, event, member, now + timedelta(minutes=6))['checked']
    assert not resume(previous, event, member, now - timedelta(seconds=1))['checked']
    assert not resume({**previous, 'profile_fingerprint': 'other'}, event, member, now)['checked']
    member.joined_at += timedelta(seconds=1)
    assert not resume(previous, event, member, now)['checked']


def channel():
    return types.Channel(id=456, title='test', photo=types.ChatPhotoEmpty(),
        date=datetime.now(UTC), megagroup=True, access_hash=123)


@pytest.mark.asyncio
async def test_incomplete_or_denied_admin_list_does_not_establish_ordinary_role():
    client = AsyncMock(return_value=Obj(count=400, participants=[Obj(user_id=i) for i in range(100)]))
    assert await administrator_ids(client, channel(), {}) is None
    assert client.await_count == 3
    duplicate_pages = AsyncMock(return_value=Obj(count=200, participants=[Obj(user_id=i + 1) for i in range(100)]))
    assert await administrator_ids(duplicate_pages, channel(), {}) is None
    client = AsyncMock(side_effect=errors.ChatAdminRequiredError(None))
    progress = {}
    assert await administrator_ids(client, channel(), progress) is None
    assert progress['admin_list_denied'] is True
    assert await administrator_ids(client, channel(), progress) is None
    assert client.await_count == 1
    client = AsyncMock(side_effect=RpcDeferred('telegram_read_slice', 60))
    with pytest.raises(RpcDeferred):
        await administrator_ids(client, channel(), {})


class ChannelClient(Client):
    def __init__(self):
        super().__init__()
        self.entity = channel()
        self.calls = []

    async def get_input_entity(self, user):
        return types.InputPeerSelf() if user == 'me' else types.InputPeerUser(user.id, 123)

    async def __call__(self, request):
        self.calls.append(type(request).__name__)
        if isinstance(request, GetParticipantsRequest):
            return Obj(count=1, participants=[Obj(user_id=999)])
        if isinstance(request, GetParticipantRequest):
            return Obj(participant=types.ChannelParticipant(
                getattr(request.participant, 'user_id', 200), datetime.now(UTC)))
        return await super().__call__(request)


def rule_message(mid):
    return Obj(id=mid, sender_id=mid + 1000, sender=Obj(id=mid + 1000, bot=False),
        message='本群禁止刷屏，规则转述', date=datetime.utcnow(), edit_date=None, media=None)


@pytest.mark.asyncio
@pytest.mark.parametrize('decision,participant_reads', [('allowed', 1), ('trial', 2)])
async def test_complete_admin_list_replaces_thirty_two_rule_author_reads(live_auth, decision, participant_reads):
    row, _, db = live_auth
    row.decision = decision
    client = ChannelClient()
    client.history = [rule_message(200 + i) for i in range(32)]
    await actions.refresh_live_authorization(db, client, 2, 456,
        recheck_rules=True, message_ids=[m.id for m in client.history], progress={})
    assert client.calls.count('GetParticipantsRequest') == 1
    assert client.calls.count('GetParticipantRequest') == participant_reads
    client.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_fallback_resumes_checked_messages_and_changed_content_is_rechecked(live_auth):
    row, _, db = live_auth
    row.decision = 'allowed'
    client = Client()
    client.history = [rule_message(200 + i) for i in range(8)]
    original = client.get_permissions
    calls = []

    async def permissions(entity, who):
        if who != 'me' and who.id >= 1200:
            calls.append(who.id)
            if len(calls) == 5:
                raise RpcDeferred('telegram_read_slice', 60)
        return await original(entity, who)

    client.get_permissions = permissions
    progress = {}
    kwargs = {'recheck_rules': True, 'message_ids': [m.id for m in client.history], 'progress': progress}
    with pytest.raises(RpcDeferred):
        await actions.refresh_live_authorization(db, client, 2, 456, **kwargs)
    assert len(progress['checked']) == 4
    await actions.refresh_live_authorization(db, client, 2, 456, **kwargs)
    assert calls == [1200, 1201, 1202, 1203, 1204, 1204, 1205, 1206, 1207]
    client.history[0].message = '本群禁止任何广告'
    client.sender_permissions[1200] = Obj(is_admin=True, is_creator=False, has_left=False, is_banned=False)
    with pytest.raises(TelegramExecutionError, match='rules'):
        await actions.refresh_live_authorization(db, client, 2, 456, **kwargs)
    assert calls[-1] == 1200


@pytest.mark.asyncio
async def test_partial_renewal_persists_without_acknowledging_new_event(test_db, monkeypatch):
    from app.modules.acquisition.qualification_events import acknowledge, pending, request
    from app.modules.acquisition.qualification_renewal import try_light_renewal

    actor, account, group, member, row, previous = await fixture(test_db)
    await request(test_db, member, 'rules', message_ids=[200])
    await test_db.flush()
    event = await pending(test_db, member)

    async def interrupt(*args, **kwargs):
        kwargs['progress']['checked']['200'] = 'proof'
        raise RpcDeferred('telegram_read_slice', 60)

    monkeypatch.setattr(actions, 'refresh_live_authorization', interrupt)
    result = await try_light_renewal(actor, account, group, member, row, previous, force_refresh=False)
    assert not result.passed and row.decision == 'trial'
    saved = json.loads(row.evidence_json)
    assert saved['renewal_progress']['checked'] == {'200': 'proof'}
    assert await pending(test_db, member) == event
    await request(test_db, member, 'rules', message_ids=[201])
    await test_db.flush()
    assert not await acknowledge(test_db, member, event)
    assert not resume(saved, await pending(test_db, member), member, datetime.utcnow())['checked']
