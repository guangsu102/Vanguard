"""Final Telegram permission checks must fail before sending or leaving."""

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock, Mock

import pytest
from telethon.errors import UserNotParticipantError

from app.core.account.telegram_execution import (
    TelegramExecutionError,
    TelegramExecutionService,
    TelegramSendOutcomeUnknownError,
)
from app.modules.acquisition import qualification_actions as actions

PRECEDENT_DATE = datetime.utcnow() - timedelta(hours=30)


class Client:
    def __init__(self, *, about="允许成员文字广告", slow=None, paid=0):
        self.entity = Obj(id=456, megagroup=True, default_banned_rights=None)
        self.permissions = Obj(
            is_admin=False,
            is_creator=False,
            has_left=False,
            is_banned=False,
            participant=Obj(banned_rights=None),
        )
        self.full = Obj(about=about, online_count=2, slowmode_next_send_date=slow, send_paid_messages_stars=paid)
        self.pins = []
        self.history = []
        self.old_admin = []
        self.precedents = [
            Obj(
                id=101 + index,
                sender_id=501 + index,
                sender=Obj(id=501 + index, bot=False),
                message=f"优惠出售服务联系 https://seller-{index}.example",
                date=PRECEDENT_DATE,
                edit_date=None,
                media=None,
                reply_to=None,
            )
            for index in range(2)
        ]
        self.sender_permissions = {}
        self.send_message = AsyncMock(return_value=Obj())

    async def get_entity(self, target):
        return self.entity

    async def get_permissions(self, entity, who):
        if who != "me":
            value = self.sender_permissions.get(who.id, self.permissions)
            if isinstance(value, Exception):
                raise value
            return value
        return self.permissions

    async def get_messages(self, entity, *, ids):
        if set(ids) & {101, 102}:
            return [item for item in self.precedents if item.id in ids]
        return [item for item in [*self.old_admin, *self.history] if item is None or item.id in ids]

    async def __call__(self, request):
        return Obj(full_chat=self.full)

    async def iter_messages(self, entity, **kwargs):
        for item in (self.pins if "filter" in kwargs else self.history)[: kwargs.get("limit", 100)]:
            yield item


@pytest.mark.asyncio
async def test_legacy_positive_id_resolves_as_proven_channel_not_user(live_auth):
    _, _, db = live_auth
    client = Client()
    client.get_entity = AsyncMock(return_value=client.entity)
    await actions.refresh_live_authorization(db, client, 2, 456)
    client.get_entity.assert_awaited_once_with(-1000000000456)
    client.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_renewal_uses_one_entity_read_with_real_telethon(live_auth):
    from datetime import UTC
    from telethon import TelegramClient, types
    from telethon.sessions import MemorySession
    from telethon.tl.functions.channels import GetChannelsRequest, GetFullChannelRequest, GetParticipantRequest

    _, _, db = live_auth
    now = datetime.now(UTC)
    entity = types.Channel(id=456, title="test", photo=types.ChatPhotoEmpty(),
                           date=now, megagroup=True, access_hash=123)
    client = TelegramClient(MemorySession(), 123, "test")
    client.session.process_entities(Obj(users=[], chats=[entity]))
    calls = []

    async def dispatch(sender, rpc, **kwargs):
        calls.append(type(rpc).__name__)
        if isinstance(rpc, GetChannelsRequest):
            return Obj(chats=[entity])
        if isinstance(rpc, GetFullChannelRequest):
            return Obj(full_chat=Obj(about="", participants_count=100))
        assert isinstance(rpc, GetParticipantRequest)
        return Obj(participant=types.ChannelParticipant(user_id=200, date=now))

    client._call = dispatch
    await actions.refresh_live_authorization(db, client, 2, 456, permissions_only=True)
    assert calls == ["GetChannelsRequest", "GetFullChannelRequest", "GetParticipantRequest"]


@pytest.mark.asyncio
async def test_unresolvable_peer_is_a_bounded_retry(live_auth):
    _, _, db = live_auth
    client = Client()
    client.get_entity = AsyncMock(side_effect=ValueError("missing input entity"))
    with pytest.raises(TelegramExecutionError, match="qualification_entity_unavailable") as error:
        await actions.refresh_live_authorization(db, client, 2, 456)
    assert error.value.retry_after_seconds == 300
    client.send_message.assert_not_awaited()


@pytest.fixture
def live_auth(monkeypatch):
    import json

    row = Obj(
        evidence_json=json.dumps(
            {
                "group_type": "supergroup",
                "raw_peer_id": 456,
                "system_identity_coverage": True,
                "system_user_ids": [200],
                "evidence": [
                    {"source": "full_about", "text": "允许成员文字广告"},
                    *[
                        {
                            "source": "recent_promotional_message",
                            "message_id": 101 + index,
                            "sender_id": 501 + index,
                            "sender_role": "ordinary",
                            "text": f"优惠出售服务联系 https://seller-{index}.example",
                            "date": PRECEDENT_DATE.isoformat(),
                            "edited_at": None,
                            "age_hours": 30,
                            "accessible": True,
                            "warning_search_complete": True,
                            "warning_reply_ids": [],
                        }
                        for index in range(2)
                    ],
                ],
            }
        ),
        checked_at=datetime.utcnow() - timedelta(seconds=1),
        expires_at=datetime.utcnow() + timedelta(hours=24),
        reason=None,
        decision="trial",
    )
    group, member = Obj(group_id=456), Obj(review_status="approved", ad_status="active")
    monkeypatch.setattr(
        actions, "current_authorization", AsyncMock(return_value=(row, group, member))
    )
    return row, member, Obj(commit=AsyncMock())


@pytest.mark.asyncio
async def test_current_rules_invalidate_before_send(live_auth):
    live_auth[0].decision = "allowed"  # Group-rule fallback must still track rule changes.
    row, member, db = live_auth
    client = Client(about="禁止推广")
    with pytest.raises(TelegramExecutionError, match="rules_changed"):
        await actions.refresh_live_authorization(db, client, 2, 456)
    assert member.review_status == "initial_pending"
    assert row.expires_at <= datetime.utcnow()
    db.commit.assert_awaited_once()
    client.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_unchanged_ad_ban_with_verified_precedent_passes_live_read(live_auth):
    row, _, db = live_auth
    snapshot = json.loads(row.evidence_json)
    snapshot["evidence"][0]["text"] = "本群禁止任何广告和推广。"
    row.evidence_json = json.dumps(snapshot)
    client = Client(about="本群禁止任何广告和推广。")
    await actions.refresh_live_authorization(db, client, 2, 456)
    client.send_message.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change,reason",
    [
        ("slow", "slowmode_wait"),
        ("paid", "paid_messages"),
        ("admin", "protected_membership"),
        ("restricted", "permission_changed"),
    ],
)
async def test_current_sending_conditions(live_auth, change, reason):
    _, _, db = live_auth
    client = Client()
    if change == "slow":
        client.full.slowmode_next_send_date = datetime.utcnow() + timedelta(minutes=5)
    elif change == "paid":
        client.full.send_paid_messages_stars = 1
    elif change == "admin":
        client.permissions.is_admin = True
    else:
        client.entity.default_banned_rights = Obj(send_plain=True)
    with pytest.raises(TelegramExecutionError, match=reason):
        await actions.refresh_live_authorization(db, client, 2, 456)
    client.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_preview_restriction_does_not_block_plain_text(live_auth):
    _, _, db = live_auth
    client = Client()
    client.entity.default_banned_rights = Obj(embed_links=True)
    await actions.refresh_live_authorization(db, client, 2, 456)


@pytest.mark.asyncio
async def test_explicit_rule_permission_needs_no_ordinary_ad_precedent(live_auth):
    row, _, db = live_auth
    row.decision = "allowed"
    data = json.loads(row.evidence_json)
    data["evidence"] = [item for item in data["evidence"] if item["source"] == "full_about"]
    row.evidence_json = json.dumps(data)
    client = Client()
    client.precedents = []
    await actions.refresh_live_authorization(db, client, 2, 456)
    client.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_trial_without_original_ordinary_ad_is_invalidated(live_auth):
    row, _, db = live_auth
    data = json.loads(row.evidence_json)
    data["evidence"] = [item for item in data["evidence"] if item["source"] == "full_about"]
    row.evidence_json = json.dumps(data)
    with pytest.raises(TelegramExecutionError, match="rules_unknown"):
        await actions.refresh_live_authorization(db, Client(), 2, 456)
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_ad_without_message_id_is_unknown_and_recorded_as_failure(monkeypatch):
    client = Client()
    guard = Obj(
        db=Obj(get=AsyncMock(return_value=None), scalar=AsyncMock(return_value=None)),
        check_and_reserve=AsyncMock(return_value=Obj(allowed=True, reason="reserved")),
        record_failure=AsyncMock(),
        record_success=AsyncMock(),
    )
    from app.modules.acquisition import qualification_service as service

    # This test exercises the post-RPC outcome; qualification prerequisites are
    # independently covered with actual database records by the service suite.
    monkeypatch.setattr(service, "send_gate", AsyncMock(return_value=None))
    monkeypatch.setattr(actions, "validate_live_send", AsyncMock())
    monkeypatch.setattr(
        service,
        "current_authorization",
        AsyncMock(
            return_value=(
                Obj(id=1, evidence_hash="proof", content_scope="text_profile", policy_version="v2",
                    evidence_json='{"group_type":"supergroup"}'),
                Obj(group_id=456),
                Obj(id=1),
            )
        ),
    )
    execution = TelegramExecutionService(guard)
    from telethon.tl.types import InputPeerChannel
    client.get_input_entity = AsyncMock(return_value=InputPeerChannel(456, 987))
    monkeypatch.setattr(execution, "_outbound_service", AsyncMock(return_value=None))
    with pytest.raises(TelegramSendOutcomeUnknownError):
        await execution.send_ad(Obj(account_id=2, client=client), 456, "广告")
    assert client.send_message.await_count == 1
    guard.record_success.assert_not_called()
    guard.record_failure.assert_awaited_once()


def rule_message(message_id=10, *, text="本群禁止广告", date=None, edit_date=None, sender_id=8):
    return Obj(
        id=message_id,
        message=text,
        date=date or datetime.utcnow(),
        edit_date=edit_date,
        media=None,
        sender_id=sender_id,
        sender=Obj(id=sender_id, bot=False),
    )


@pytest.mark.asyncio
async def test_missing_self_permission_never_allows_send(live_auth):
    _, _, db = live_auth
    client = Client()
    client.permissions = None
    with pytest.raises(TelegramExecutionError, match="permission_unknown"):
        await actions.refresh_live_authorization(db, client, 2, 456)
    client.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("sender_id,error", [(8, ValueError("unavailable")), (-123, None)])
async def test_new_potential_rule_with_unknown_author_pauses(live_auth, sender_id, error):
    live_auth[0].decision = "allowed"  # Group-rule fallback must still track rule changes.
    row, member, db = live_auth
    client = Client()
    client.history = [rule_message(sender_id=sender_id)]
    if error:
        client.sender_permissions[sender_id] = error
    with pytest.raises(TelegramExecutionError, match="rules_unknown"):
        await actions.refresh_live_authorization(db, client, 2, 456, message_ids=[10])
    assert row.decision == "observe"
    assert member.review_status == "initial_pending"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_new_admin_rule_is_not_hidden_by_same_text_in_ordinary_evidence(live_auth):
    live_auth[0].decision = "allowed"  # Group-rule fallback must still track rule changes.
    row, _, db = live_auth
    prior = json.loads(row.evidence_json)
    prior["evidence"].append({"source": "recent_promotional_message", "text": "本群禁止广告"})
    row.evidence_json = json.dumps(prior)
    client = Client()
    client.history = [rule_message()]
    client.sender_permissions[8] = Obj(is_admin=True, is_creator=False)
    with pytest.raises(TelegramExecutionError, match="rules_changed"):
        await actions.refresh_live_authorization(db, client, 2, 456, message_ids=[10])


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [[], [None]])
async def test_old_admin_rule_omitted_from_response_invalidates(live_auth, missing):
    live_auth[0].decision = "allowed"  # Group-rule fallback must still track rule changes.
    row, _, db = live_auth
    prior = json.loads(row.evidence_json)
    prior["evidence"].append({"source": "admin_rule", "message_id": 10, "text": "允许广告"})
    row.evidence_json = json.dumps(prior)
    client = Client()
    client.old_admin = missing
    with pytest.raises(TelegramExecutionError, match="rules_unknown"):
        await actions.refresh_live_authorization(db, client, 2, 456)
    assert row.reason == "admin_rule_unavailable_before_send"


@pytest.mark.asyncio
async def test_edit_of_old_admin_rule_invalidates_even_when_text_is_identical(live_auth):
    live_auth[0].decision = "allowed"  # Group-rule fallback must still track rule changes.
    row, _, db = live_auth
    prior = json.loads(row.evidence_json)
    prior["evidence"].append(
        {
            "source": "admin_rule",
            "message_id": 10,
            "text": "允许广告",
            "edited_at": None,
        }
    )
    row.evidence_json = json.dumps(prior)
    client = Client()
    client.old_admin = [
        rule_message(
            text="允许广告", date=datetime.utcnow() - timedelta(days=5), edit_date=datetime.utcnow()
        )
    ]
    with pytest.raises(TelegramExecutionError, match="rules_changed"):
        await actions.refresh_live_authorization(db, client, 2, 456)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_dates", [False, True])
async def test_requested_rule_event_must_have_message_and_date(live_auth, missing_dates):
    live_auth[0].decision = "allowed"  # Group-rule fallback must still track rule changes.
    row, _, db = live_auth
    client = Client()
    client.history = []
    if missing_dates:
        client.history = [Obj(id=1, message="普通聊天", date=None)]
    with pytest.raises(TelegramExecutionError, match="rules_unknown"):
        await actions.refresh_live_authorization(db, client, 2, 456, message_ids=[1])
    assert row.reason == ("recent_rules_coverage_unknown_before_send" if missing_dates else "event_rules_unavailable")


def exit_fixture():
    group = Obj(id=40, group_id=456)
    member = Obj(
        id=60,
        group_id=40,
        account_id=2,
        leave_attempts=1,
        review_status="leave_failed",
        status="joined",
        ad_status="blocked",
        review_next_at=datetime.utcnow(),
        leave_retry_at=datetime.utcnow(),
        left_at=None,
        leave_confirmed_at=None,
        leave_error="leave_outcome_unconfirmed:TimeoutError",
    )
    client = Obj(
        get_permissions=AsyncMock(
            return_value=Obj(
                has_left=True,
                is_admin=False,
                is_creator=False,
            )
        )
    )
    wrapper = Obj(client=client)
    db = Obj(
        get=AsyncMock(return_value=group),
        commit=AsyncMock(),
        scalars=AsyncMock(return_value=Obj(all=lambda: [member])),
    )
    service = Obj(
        db=db,
        account_pool=Obj(add_account_from_db=AsyncMock(), acquire_by_id=AsyncMock(return_value=wrapper), release=AsyncMock()),
        _resolve_group_entity_for_leave=AsyncMock(return_value=(Obj(id=456), None)),
        _discovered_group_from_model=Mock(return_value=Obj(group_id=456)),
        _leave_group=AsyncMock(),
    )
    return service, member, group, client


@pytest.fixture
def isolated_exit_workflow(monkeypatch):
    # These existing tests cover reconciliation. Distributed lock behavior has
    # its own independent test module and must not be inferred from this stub.
    from app.modules.acquisition import adaptive_frequency
    # No adaptive exit is pending in this legacy reconciliation/lock fixture.
    monkeypatch.setattr(adaptive_frequency, "run_frequency_exits", AsyncMock(return_value={"processed": 0}))
    @asynccontextmanager
    async def unlocked(_db):
        yield None

    monkeypatch.setattr(actions, "exit_serialization_lock", unlocked)
    monkeypatch.setattr(actions, "reconcile_stale_exit_memberships", AsyncMock(return_value={}))


@pytest.mark.asyncio
@pytest.mark.parametrize("proof", ["has_left", "not_participant"])
async def test_exit_retry_reconciles_before_fresh_assessment(
    monkeypatch, proof, isolated_exit_workflow
):
    service, member, group, client = exit_fixture()
    if proof == "not_participant":
        client.get_permissions.side_effect = UserNotParticipantError(None)
    monkeypatch.setattr(
        actions,
        "policy",
        AsyncMock(
            return_value={
                "enabled": True,
                "execute_exits": True,
                "account_ids": [2],
            }
        ),
    )
    assess = AsyncMock()
    monkeypatch.setattr(actions, "assess", assess)
    record_exit = AsyncMock()
    monkeypatch.setattr(actions, "record_confirmed_exit", record_exit)
    result = await actions.run_exits(service)
    assert result["processed"] == 1
    assert result["results"][0]["reconciled"]
    assert member.status == "left"
    assert member.left_at is not None
    assert member.leave_attempts == 1
    assess.assert_not_awaited()
    service._leave_group.assert_not_awaited()

    record_exit.assert_awaited_once_with(service.db, member, group)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["unknown", "protected"])
async def test_exit_uncertain_or_protected_membership_never_replays_leave(
    monkeypatch, state, isolated_exit_workflow
):
    service, member, _, client = exit_fixture()
    client.get_permissions.return_value = (
        None
        if state == "unknown"
        else Obj(
            has_left=False,
            is_admin=True,
            is_creator=False,
        )
    )
    monkeypatch.setattr(
        actions,
        "policy",
        AsyncMock(
            return_value={
                "enabled": True,
                "execute_exits": True,
                "account_ids": [2],
            }
        ),
    )
    assess = AsyncMock()
    monkeypatch.setattr(actions, "assess", assess)
    await actions.run_exits(service)
    assert member.review_status == (
        "exit_pending" if state == "unknown" else "owned_group_excluded"
    )
    assert member.left_at is None
    assess.assert_not_awaited()
    service._leave_group.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolution_error_is_not_evidence_that_the_target_was_left():
    service, member, group, client = exit_fixture()
    service._resolve_group_entity_for_leave.side_effect = UserNotParticipantError(None)
    assert await actions.reconcile_exit(service, member, group) == "unknown"
    client.get_permissions.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconciliation_rejects_wrong_target_identity():
    service, member, group, client = exit_fixture()
    service._resolve_group_entity_for_leave.return_value = (Obj(id=999), None)
    assert await actions.reconcile_exit(service, member, group) == "unknown"
    client.get_permissions.assert_not_awaited()


@pytest.mark.asyncio
async def test_exit_recheck_retries_busy_account_lease_without_dropping_task(
    monkeypatch, isolated_exit_workflow
):
    service, member, group, _client = exit_fixture()
    member.leave_attempts = 0
    member.review_status = "exit_pending"
    row = Obj(state="completed", next_retry_at=datetime.utcnow() + timedelta(hours=2))
    monkeypatch.setattr(
        actions,
        "policy",
        AsyncMock(
            return_value={
                "enabled": True,
                "execute_exits": True,
                "account_ids": [2],
                "exit_membership_ids": [member.id],
                "exit_reason_allowlist": ["messages_below_5_in_72h"],
            }
        ),
    )
    monkeypatch.setattr(
        actions,
        "current_authorization",
        AsyncMock(return_value=(row, group, member)),
    )
    monkeypatch.setattr(
        actions,
        "assess",
        AsyncMock(
            return_value=Obj(
                verification_details={"qualification_decision": "technical_wait"},
                permission_reason="AccountOperationLeaseBusy",
            )
        ),
    )

    result = await actions.run_exits(service)

    assert result["processed"] == 1
    assert member.review_status == "exit_pending"
    assert member.ad_status == "blocked"
    assert member.leave_attempts == 0
    assert datetime.utcnow() < member.review_next_at <= datetime.utcnow() + timedelta(minutes=6)
    assert member.leave_error == "exit_review_technical_wait:AccountOperationLeaseBusy"
    service._leave_group.assert_not_awaited()


@pytest.mark.asyncio
async def test_exit_recheck_keeps_pending_when_model_is_unavailable(
    monkeypatch, isolated_exit_workflow
):
    service, member, group, _client = exit_fixture()
    member.leave_attempts = 0
    member.review_status = "exit_pending"
    retry_at = datetime.utcnow() + timedelta(minutes=5)
    row = Obj(state="waiting_ai", next_retry_at=retry_at)
    monkeypatch.setattr(
        actions,
        "policy",
        AsyncMock(
            return_value={
                "enabled": True,
                "execute_exits": True,
                "account_ids": [2],
                "exit_membership_ids": [member.id],
                "exit_reason_allowlist": ["group_rules_ai_disallow_ads"],
            }
        ),
    )
    monkeypatch.setattr(
        actions,
        "current_authorization",
        AsyncMock(return_value=(row, group, member)),
    )
    monkeypatch.setattr(
        actions,
        "assess",
        AsyncMock(
            return_value=Obj(
                verification_details={"qualification_decision": "observe"},
                permission_reason="group_rules_ai_unavailable",
            )
        ),
    )

    result = await actions.run_exits(service)

    assert result["processed"] == 1
    assert member.review_status == "exit_pending"
    assert member.ad_status == "blocked"
    assert member.review_next_at == retry_at
    assert member.leave_error == "exit_review_waiting_ai"
    service._leave_group.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "online_count,expected",
    [(1, "qualification_no_other_online_member"), (None, "qualification_online_count_unknown")],
)
async def test_precedent_send_does_not_require_another_member_online(live_auth, online_count, expected):
    row, member, db = live_auth
    client = Client()
    client.full.online_count = online_count
    await actions.refresh_live_authorization(db, client, 2, 456)
    assert member.review_status == "approved"
    assert row.expires_at > datetime.utcnow()
    db.commit.assert_not_awaited()
    client.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_deleted_ad_precedent_invalidates_before_send(live_auth):
    row, member, db = live_auth
    client = Client()
    client.precedents.pop(0)
    with pytest.raises(TelegramExecutionError, match="rules_changed"):
        await actions.refresh_live_authorization(db, client, 2, 456)
    assert row.reason == "advertising_precedent_disappeared_before_send"
    assert member.review_status == "initial_pending"
    client.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_edited_ad_precedent_invalidates_before_send(live_auth):
    row, _, db = live_auth
    client = Client()
    client.precedents[0].edit_date = datetime.utcnow()
    with pytest.raises(TelegramExecutionError, match="rules_changed"):
        await actions.refresh_live_authorization(db, client, 2, 456)
    assert row.reason == "advertising_precedent_changed_before_send"


@pytest.mark.asyncio
async def test_ad_precedent_sender_becomes_admin_before_send(live_auth):
    row, _, db = live_auth
    client = Client()
    client.sender_permissions[501] = Obj(is_admin=True, is_creator=False)
    with pytest.raises(TelegramExecutionError, match="rules_unknown"):
        await actions.refresh_live_authorization(db, client, 2, 456)
    assert row.reason == "advertising_precedent_identity_changed_before_send"
