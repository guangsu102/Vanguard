"""Mock-only tests for the gate-checked owned-group Telegram adapter."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import types

from app.core.account.models import AccountStatus, AccountType, TelegramAccount
from app.core.ephemeral_secret import encrypt_ephemeral_secret
from app.core.p0_safety_gate import SafetyGateDecision
from app.modules.owned_group.contracts import ItemStatus
from app.modules.owned_group.models import (
    OwnedGroupAsset,
    OwnedGroupOperation,
    OwnedGroupOperationItem,
)
from app.modules.owned_group.models_extra import OwnedBotProfile, OwnedGroupInviteLink
from app.modules.owned_group.telegram_adapter import (
    InviteLinkMutationError,
    TelethonOwnedGroupTelegramAdapter,
    _participant_is_admin,
    _participant_is_creator,
    _participant_is_member,
    classify_telegram_error,
)


@pytest.fixture(autouse=True)
def load_related_models():
    # This mock-only module must also run on its own, without another test's DB fixture.
    from tests.conftest import _import_models

    _import_models()


class FakePool:
    def __init__(self, wrappers: dict[int, object]):
        self.wrappers = wrappers
        self.acquired: list[int] = []
        self.released: list[int] = []

    async def add_account_from_db(self, account):
        return self.wrappers.get(int(account.id))

    async def acquire_by_id(self, account_id, **kwargs):
        self.acquired.append(int(account_id))
        return self.wrappers.get(int(account_id))

    async def release(self, wrapper):
        self.released.append(int(wrapper.account_id))


class FakeDB:
    def __init__(self, rows: dict[tuple[type, int], object]):
        self.rows = rows
        self.added: list[object] = []
        self.flush = AsyncMock()
        self.commit = AsyncMock()

    async def get(self, model, key):
        return self.rows.get((model, int(key)))

    async def scalar(self, _query):
        return None

    def add(self, value):
        self.added.append(value)


class FakeClient:
    def __init__(
        self, *, participants=None, invite_error=None, edit_invite_error=None, export_error=None
    ):
        self.participants = list(participants or [])
        self.invite_error = invite_error
        self.edit_invite_error = edit_invite_error
        self.export_error = export_error
        self.requests: list[object] = []

    async def get_entity(self, value):
        if isinstance(value, str) and value.startswith("@"):
            return types.User(id=9001, access_hash=900100)
        if int(value) == 12345:
            return SimpleNamespace(id=12345, megagroup=True, broadcast=False, username="owned")
        return types.User(id=int(value), access_hash=int(value) * 100)

    async def get_me(self):
        return SimpleNamespace(id=2001)

    async def __call__(self, request):
        self.requests.append(request)
        name = request.__class__.__name__
        if name == "GetParticipantRequest":
            participant = self.participants.pop(0) if self.participants else None
            return SimpleNamespace(participant=participant)
        if name == "InviteToChannelRequest" and self.invite_error is not None:
            raise self.invite_error
        if name == "EditExportedChatInviteRequest" and self.edit_invite_error is not None:
            raise self.edit_invite_error
        if name == "CreateChannelRequest":
            return SimpleNamespace(
                chats=[SimpleNamespace(id=12345, megagroup=True, broadcast=False)]
            )
        if name == "ExportChatInviteRequest":
            if self.export_error is not None:
                raise self.export_error
            return SimpleNamespace(link="https://t.me/+OwnedGroupTest1234")
        return True


def _account(account_id: int, *, account_type=AccountType.PROMOTER):
    return TelegramAccount(
        id=account_id,
        identifier=f"account-{account_id}",
        session_name=f"session-{account_id}",
        account_type=account_type,
        status=AccountStatus.ONLINE,
        is_active=True,
        session_string="encrypted-session",
        risk_level="normal",
    )


def _member(*, user_id=9001, admin_rights=None, rank=None):
    name = "ChannelParticipantAdmin" if admin_rights is not None else "ChannelParticipant"
    participant = type(name, (), {})()
    participant.user_id = user_id
    participant.admin_rights = admin_rights
    participant.rank = rank
    return participant


def _creator(**values):
    values.setdefault("user_id", 2001)
    participant_type = type("ChannelParticipantCreator", (), {})
    participant = participant_type()
    for key, value in values.items():
        setattr(participant, key, value)
    return participant


def _asset(*, owner_id=1, visibility="public", username="owned", mode="direct_invite"):
    return OwnedGroupAsset(
        id=10,
        internal_name="owned-10",
        title="Owned group",
        about="about",
        visibility=visibility,
        telegram_username=username,
        owner_account_id=owner_id,
        invite_mode=mode,
        telegram_chat_id=12345,
    )


def _operation_item(resource_type="user", resource_id=2, *, admin=False):
    return OwnedGroupOperationItem(
        id=20,
        operation_id=30,
        resource_type=resource_type,
        resource_id=resource_id,
        admin_required=admin,
        admin_permissions=json.dumps({"invite_users": True}) if admin else None,
        admin_title="Ops" if admin else None,
    )


def _adapter(db, pool, *, enabled=True, precheck=None, bot_factory=None):
    return TelethonOwnedGroupTelegramAdapter(
        db,
        pool,
        execution_enabled_reader=lambda: enabled,
        safety_state_reader=lambda: SimpleNamespace(global_stop=False, backend_available=True),
        safety_precheck=precheck or (lambda *args, **kwargs: _allowed_precheck()),
        bot_client_factory=bot_factory,
    )


async def _allowed_precheck():
    return SafetyGateDecision(True, "eligible")


@pytest.mark.asyncio
async def test_execution_disabled_never_acquires_account():
    owner = _account(1)
    db = FakeDB({(TelegramAccount, 1): owner})
    pool = FakePool({})
    adapter = _adapter(db, pool, enabled=False)

    result = await adapter.create_group(_asset(), owner)

    assert result.success is False
    assert result.reason_code == "owned_group_execution_disabled"
    assert pool.acquired == []


@pytest.mark.asyncio
async def test_create_uses_supergroup_request_and_public_username():
    owner = _account(1)
    client = FakeClient()
    wrapper = SimpleNamespace(account_id=1, client=client)
    db = FakeDB({(TelegramAccount, 1): owner})
    pool = FakePool({1: wrapper})
    asset = _asset(visibility="public", username="owned_public", mode="direct_invite")
    asset.telegram_chat_id = None
    adapter = _adapter(db, pool)

    result = await adapter.create_group(asset, owner)

    assert result.success is True
    assert result.telegram_chat_id == 12345
    assert result.telegram_user_id == 2001
    assert asset.telegram_chat_id == 12345
    assert [request.__class__.__name__ for request in client.requests] == [
        "CreateChannelRequest",
        "UpdateUsernameRequest",
    ]
    create_request = client.requests[0]
    assert create_request.megagroup is True
    assert create_request.broadcast is False


@pytest.mark.asyncio
async def test_private_direct_invite_group_still_persists_recovery_link():
    owner = _account(1)
    client = FakeClient()
    wrapper = SimpleNamespace(account_id=1, client=client)
    db = FakeDB({(TelegramAccount, 1): owner})
    pool = FakePool({1: wrapper})
    asset = _asset(visibility="private", username=None, mode="direct_invite")
    asset.telegram_chat_id = None
    adapter = _adapter(db, pool)

    result = await adapter.create_group(asset, owner)

    assert result.success is True
    assert result.telegram_user_id == 2001
    request_names = [request.__class__.__name__ for request in client.requests]
    assert request_names == ["CreateChannelRequest", "ExportChatInviteRequest"]
    assert any(isinstance(value, OwnedGroupInviteLink) for value in db.added)


@pytest.mark.asyncio
async def test_existing_group_requires_owner_admin_before_ready():
    owner = _account(1)
    client = FakeClient(participants=[_member(user_id=2001)])
    wrapper = SimpleNamespace(account_id=1, client=client)
    db = FakeDB({(TelegramAccount, 1): owner})
    pool = FakePool({1: wrapper})
    adapter = _adapter(db, pool)

    result = await adapter.create_group(_asset(), owner)

    assert result.success is False
    assert result.reason_code == "owner_not_group_admin"
    assert not any(
        request.__class__.__name__ == "CreateChannelRequest" for request in client.requests
    )


@pytest.mark.asyncio
async def test_existing_private_group_repairs_missing_invite_without_recreating():
    owner = _account(1)
    owner_admin = _creator(admin_rights=SimpleNamespace(invite_users=True))
    client = FakeClient(participants=[owner_admin])
    wrapper = SimpleNamespace(account_id=1, client=client)
    db = FakeDB({(TelegramAccount, 1): owner})
    pool = FakePool({1: wrapper})
    adapter = _adapter(db, pool)

    result = await adapter.create_group(
        _asset(visibility="private", username=None, mode="direct_invite"), owner
    )

    assert result.success is True
    request_names = [request.__class__.__name__ for request in client.requests]
    assert "CreateChannelRequest" not in request_names
    assert "ExportChatInviteRequest" in request_names
    assert any(isinstance(value, OwnedGroupInviteLink) for value in db.added)


@pytest.mark.asyncio
async def test_revoke_invite_durably_hides_local_bearer_before_remote_write():
    owner = _account(1)
    owner_admin = _creator(admin_rights=SimpleNamespace(invite_users=True))
    client = FakeClient(participants=[owner_admin])
    wrapper = SimpleNamespace(account_id=1, client=client)
    db = FakeDB({(TelegramAccount, 1): owner})
    pool = FakePool({1: wrapper})
    invite = OwnedGroupInviteLink(
        id=55,
        group_asset_id=10,
        link_type="member_invite",
        link_ciphertext=encrypt_ephemeral_secret("https://t.me/+OwnedGroupTest1234"),
        is_active=True,
    )
    adapter = _adapter(db, pool)

    result = await adapter.revoke_invite_link(_asset(visibility="private"), invite)

    assert result["status"] == "revoked"
    assert invite.is_active is False
    db.commit.assert_awaited_once()
    assert [request.__class__.__name__ for request in client.requests] == [
        "GetParticipantRequest",
        "EditExportedChatInviteRequest",
    ]


@pytest.mark.asyncio
async def test_regenerate_keeps_old_invite_hidden_when_export_fails():
    owner = _account(1)
    owner_admin = _creator(admin_rights=SimpleNamespace(invite_users=True))
    client = FakeClient(participants=[owner_admin], export_error=RuntimeError("transport ended"))
    wrapper = SimpleNamespace(account_id=1, client=client)
    db = FakeDB({(TelegramAccount, 1): owner})
    pool = FakePool({1: wrapper})
    invite = OwnedGroupInviteLink(
        id=56,
        group_asset_id=10,
        link_type="member_invite",
        link_ciphertext=encrypt_ephemeral_secret("https://t.me/+OwnedGroupTest1234"),
        is_active=True,
    )
    adapter = _adapter(db, pool)

    with pytest.raises(InviteLinkMutationError):
        await adapter.regenerate_invite_link(
            _asset(visibility="private"), invite=invite, request_needed=False
        )

    assert invite.is_active is False
    db.commit.assert_awaited_once()
    assert not any(
        isinstance(value, OwnedGroupInviteLink) and value.is_active for value in db.added
    )


@pytest.mark.asyncio
async def test_bot_invite_and_admin_promotion_do_not_acquire_bot_lease():
    owner = _account(1)
    linked_bot_account = _account(2, account_type=AccountType.GUARDIAN_BOT)
    token_ciphertext = encrypt_ephemeral_secret("123456:bot-secret")
    profile = OwnedBotProfile(
        id=7,
        owner_account_id=1,
        account_id=2,
        bot_user_id=None,
        token_ciphertext=token_ciphertext,
        status="verified",
        enabled=True,
    )
    non_admin = _member()
    admin = _member(
        admin_rights=SimpleNamespace(invite_users=True),
        rank="Ops",
    )
    client = FakeClient(participants=[None, non_admin, non_admin, admin])
    owner_wrapper = SimpleNamespace(account_id=1, client=client)
    db = FakeDB(
        {
            (TelegramAccount, 1): owner,
            (TelegramAccount, 2): linked_bot_account,
            (OwnedBotProfile, 7): profile,
        }
    )
    pool = FakePool({1: owner_wrapper})
    bot_client = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(id=9001, username="owned_bot")),
        close=AsyncMock(),
    )

    def bot_factory(token):
        return bot_client

    seen_resources: list[list[dict]] = []

    async def precheck(_db, resources, _owner_id, **_kwargs):
        seen_resources.append(resources)
        return SafetyGateDecision(True, "eligible")

    adapter = _adapter(db, pool, precheck=precheck, bot_factory=bot_factory)
    item = _operation_item("bot", 7, admin=True)
    operation = OwnedGroupOperation(id=30, group_asset_id=10, operation_type="join")

    result = await adapter.execute_item(_asset(), operation, item)

    assert result.success is True
    assert result.status == ItemStatus.ADMIN_VERIFIED.value
    assert result.telegram_user_id == 9001
    assert pool.acquired == [1]
    assert pool.released == [1]
    assert any(
        request.__class__.__name__ == "InviteToChannelRequest" for request in client.requests
    )
    assert any(request.__class__.__name__ == "EditAdminRequest" for request in client.requests)
    assert seen_resources[-1][1] == {"resource_type": "bot", "resource_id": 7}
    bot_client.get_me.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconcile_bot_is_read_only_and_does_not_acquire_target():
    owner = _account(1)
    linked_bot_account = _account(2, account_type=AccountType.GUARDIAN_BOT)
    profile = OwnedBotProfile(
        id=7,
        owner_account_id=1,
        account_id=2,
        bot_user_id=9001,
        token_ciphertext=encrypt_ephemeral_secret("123456:bot-secret"),
        status="verified",
        enabled=True,
    )
    admin = _member(admin_rights=SimpleNamespace(invite_users=True), rank="Ops")
    client = FakeClient(participants=[admin])
    owner_wrapper = SimpleNamespace(account_id=1, client=client)
    db = FakeDB(
        {
            (TelegramAccount, 1): owner,
            (TelegramAccount, 2): linked_bot_account,
            (OwnedBotProfile, 7): profile,
        }
    )
    pool = FakePool({1: owner_wrapper})
    adapter = _adapter(db, pool)
    item = _operation_item("bot", 7, admin=True)
    operation = OwnedGroupOperation(id=30, group_asset_id=10, operation_type="join")

    result = await adapter.reconcile_item(_asset(), operation, item)

    assert result is not None
    assert result.success is True
    assert result.status == ItemStatus.ADMIN_VERIFIED.value
    assert result.telegram_user_id == 9001
    assert pool.acquired == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("participant", "expected_reason", "expected_is_admin"),
    [
        (
            _member(),
            "admin_not_verified",
            False,
        ),
        (
            _member(
                admin_rights=SimpleNamespace(invite_users=False),
                rank="Ops",
            ),
            "admin_permissions_not_verified",
            True,
        ),
        (
            _member(
                admin_rights=SimpleNamespace(invite_users=True),
                rank="Different",
            ),
            "admin_title_not_verified",
            True,
        ),
    ],
)
async def test_reconcile_never_completes_an_unverified_admin_contract(
    participant, expected_reason, expected_is_admin
):
    owner = _account(1)
    linked_bot_account = _account(2, account_type=AccountType.GUARDIAN_BOT)
    profile = OwnedBotProfile(
        id=7,
        owner_account_id=1,
        account_id=2,
        bot_user_id=9001,
        token_ciphertext=encrypt_ephemeral_secret("123456:bot-secret"),
        status="verified",
        enabled=True,
    )
    client = FakeClient(participants=[participant])
    owner_wrapper = SimpleNamespace(account_id=1, client=client)
    db = FakeDB(
        {
            (TelegramAccount, 1): owner,
            (TelegramAccount, 2): linked_bot_account,
            (OwnedBotProfile, 7): profile,
        }
    )
    pool = FakePool({1: owner_wrapper})
    adapter = _adapter(db, pool)

    result = await adapter.reconcile_item(
        _asset(),
        OwnedGroupOperation(id=30, group_asset_id=10, operation_type="join"),
        _operation_item("bot", 7, admin=True),
    )

    assert result is not None
    assert result.success is False
    assert result.transient is True
    assert result.status == ItemStatus.FAILED_TRANSIENT.value
    assert result.reason_code == expected_reason
    assert result.membership_verified is True
    assert result.is_admin is expected_is_admin
    assert pool.acquired == [1]


@pytest.mark.asyncio
async def test_admin_promotion_requires_configured_title_on_second_read():
    owner = _account(1)
    linked_bot_account = _account(2, account_type=AccountType.GUARDIAN_BOT)
    profile = OwnedBotProfile(
        id=7,
        owner_account_id=1,
        account_id=2,
        bot_user_id=9001,
        token_ciphertext=encrypt_ephemeral_secret("123456:bot-secret"),
        status="verified",
        enabled=True,
    )
    non_admin = _member()
    wrong_title_admin = _member(
        admin_rights=SimpleNamespace(invite_users=True),
        rank="Different",
    )
    client = FakeClient(participants=[None, non_admin, non_admin, wrong_title_admin])
    owner_wrapper = SimpleNamespace(account_id=1, client=client)
    db = FakeDB(
        {
            (TelegramAccount, 1): owner,
            (TelegramAccount, 2): linked_bot_account,
            (OwnedBotProfile, 7): profile,
        }
    )
    adapter = _adapter(db, FakePool({1: owner_wrapper}))

    result = await adapter.execute_item(
        _asset(),
        OwnedGroupOperation(id=30, group_asset_id=10, operation_type="join"),
        _operation_item("bot", 7, admin=True),
    )

    assert result.success is False
    assert result.status == ItemStatus.ADMIN_PROMOTING.value
    assert result.reason_code == "admin_verification_pending"
    assert result.membership_verified is True
    assert result.is_admin is True
    assert result.admin_title == "Different"


def test_error_classification_is_conservative_and_redacted():
    flood_type = type("FloodWaitError", (RuntimeError,), {})
    flood = flood_type("wait of 30 seconds for bot123456:secret")
    flood.seconds = 30
    classified = classify_telegram_error(flood)
    assert classified.status == ItemStatus.FAILED_TRANSIENT.value
    assert classified.reason_code == "flood_wait"
    assert classified.retry_after_seconds == 30
    assert "bot123456:secret" not in classified.message

    unknown = classify_telegram_error(RuntimeError("transport ended"))
    assert unknown.status == ItemStatus.UNKNOWN.value
    assert unknown.reason_code == "unknown_needs_reconcile"

    privacy_type = type("UserPrivacyRestrictedError", (RuntimeError,), {})
    privacy = classify_telegram_error(privacy_type("privacy denied"))
    assert privacy.status == ItemStatus.FAILED_PERMANENT.value
    assert privacy.reason_code == "privacy_restricted"


class OwnerPeerClient(FakeClient):
    def __init__(
        self,
        *,
        participants=None,
        resolve_id=3001,
        phone_resolve_id=None,
        imported_id=3001,
        import_client_id=0,
    ):
        super().__init__(participants=participants)
        self.resolve_id = resolve_id
        self.phone_resolve_id = phone_resolve_id
        self.imported_id = imported_id
        self.import_client_id = import_client_id
        self.lookups = []

    async def get_input_entity(self, value):
        self.lookups.append(value)
        if self.resolve_id is None:
            raise ValueError("Peer absent in owner session")
        return types.InputPeerUser(self.resolve_id, 222222)

    async def get_entity(self, value):
        if value == 12345:
            return await super().get_entity(value)
        return await self.get_input_entity(value)

    async def __call__(self, request):
        if request.__class__.__name__ == "ResolvePhoneRequest":
            self.requests.append(request)
            user_id = self.phone_resolve_id
            return SimpleNamespace(
                peer=SimpleNamespace(user_id=user_id),
                users=(
                    [types.User(id=user_id, access_hash=444444)]
                    if user_id is not None
                    else []
                ),
            )
        if request.__class__.__name__ == "ImportContactsRequest":
            self.requests.append(request)
            return SimpleNamespace(
                imported=[
                    SimpleNamespace(client_id=self.import_client_id, user_id=self.imported_id)
                ],
                users=[types.User(id=self.imported_id, access_hash=333333)],
            )
        return await super().__call__(request)


def _user_invite_case(client, *, phone=None, username="selected_target", precheck=None):
    owner, target = _account(1), _account(2)
    # The target's real get_me carries is_self=True and a different access hash.
    # Passing it to owner.get_participant was the production false-success bug.
    me = types.User(id=3001, access_hash=999999, is_self=True, username=username, phone=phone)
    target_client = SimpleNamespace(get_me=AsyncMock(return_value=me))
    owner_wrapper = SimpleNamespace(account_id=1, client=client)
    target_wrapper = SimpleNamespace(account_id=2, client=target_client)
    db = FakeDB({(TelegramAccount, 1): owner, (TelegramAccount, 2): target})
    pool = FakePool({1: owner_wrapper, 2: target_wrapper})
    return _adapter(db, pool, precheck=precheck), db, pool


def _request_names(client):
    return [request.__class__.__name__ for request in client.requests]


@pytest.mark.parametrize(
    "check", [_participant_is_member, _participant_is_admin, _participant_is_creator]
)
def test_creator_response_for_owner_never_confirms_different_target(check):
    assert check(_creator(user_id=2001), expected_user_id=3001) is False
    assert check(_creator(user_id=3001), expected_user_id=3001) is True


@pytest.mark.parametrize("value", [None, 0, -1, True, "invalid"])
def test_participant_requires_a_positive_verified_identity(value):
    assert not _participant_is_member(_member(user_id=value), expected_user_id=3001)
    assert not _participant_is_admin(_creator(user_id=value), expected_user_id=3001)


def test_unknown_participant_shape_and_left_or_banned_states_cannot_be_members():
    for name in ("UnknownParticipant", "ChannelParticipantLeft", "ChannelParticipantBanned"):
        participant = type(name, (), {})()
        participant.user_id = 3001
        participant.admin_rights = SimpleNamespace(invite_users=True)
        assert not _participant_is_member(participant, expected_user_id=3001)
        assert not _participant_is_admin(participant, expected_user_id=3001)


@pytest.mark.asyncio
async def test_execute_owner_creator_response_cannot_create_false_target_membership():
    client = OwnerPeerClient(participants=[_creator(), _creator(), _creator()])
    adapter, db, _ = _user_invite_case(client)
    result = await adapter.execute_item(
        _asset(), OwnedGroupOperation(id=30), _operation_item(admin=True)
    )
    assert not result.success
    assert not result.membership_verified
    assert result.status == ItemStatus.INVITE_SENT.value
    assert "EditAdminRequest" not in _request_names(client)
    assert db.added == []
    invitation = next(
        r for r in client.requests if r.__class__.__name__ == "InviteToChannelRequest"
    )
    assert len(invitation.users) == 1
    assert isinstance(invitation.users[0], types.InputUser)
    assert invitation.users[0].user_id == 3001
    assert invitation.users[0].access_hash == 222222
    for query in (r for r in client.requests if r.__class__.__name__ == "GetParticipantRequest"):
        assert query.participant.user_id == 3001
        assert not isinstance(query.participant, (types.InputPeerSelf, types.InputUserSelf))


@pytest.mark.asyncio
async def test_reconcile_owner_creator_response_stays_unknown_without_writes():
    client = OwnerPeerClient(participants=[_creator()])
    adapter, db, _ = _user_invite_case(client, phone="15551234567")
    result = await adapter.reconcile_item(_asset(), OwnedGroupOperation(id=30), _operation_item())
    assert result.status == ItemStatus.UNKNOWN.value
    assert not result.success
    assert not result.membership_verified
    assert db.added == []
    assert _request_names(client) == ["GetParticipantRequest"]


@pytest.mark.asyncio
async def test_correct_target_is_verified_using_owner_session_access_hash():
    participant = _member(user_id=3001)
    client = OwnerPeerClient(participants=[None, participant, participant])
    adapter, _, _ = _user_invite_case(client)
    result = await adapter.execute_item(_asset(), OwnedGroupOperation(id=30), _operation_item())
    assert result.success
    assert result.membership_verified
    assert result.telegram_user_id == 3001
    invitation = next(
        r for r in client.requests if r.__class__.__name__ == "InviteToChannelRequest"
    )
    assert isinstance(invitation.users[0], types.InputUser)
    assert invitation.users[0].access_hash == 222222
    assert client.lookups[0] == "@selected_target"
    assert "ImportContactsRequest" not in _request_names(client)


@pytest.mark.asyncio
async def test_wrong_owner_resolved_id_fails_without_invite_or_promotion():
    client = OwnerPeerClient(resolve_id=2001)
    adapter, db, _ = _user_invite_case(client)
    result = await adapter.execute_item(
        _asset(), OwnedGroupOperation(id=30), _operation_item(admin=True)
    )
    assert not result.success
    assert result.reason_code == "target_peer_unavailable"
    assert not client.requests
    assert not db.added


@pytest.mark.asyncio
async def test_single_contact_import_resolves_only_selected_target_before_inviting():
    member = _member(user_id=3001)
    client = OwnerPeerClient(resolve_id=None, participants=[None, member, member])
    adapter, _, _ = _user_invite_case(client, phone="15551234567", username=None)
    result = await adapter.execute_item(_asset(), OwnedGroupOperation(id=30), _operation_item())
    assert result.success
    imports = [r for r in client.requests if r.__class__.__name__ == "ImportContactsRequest"]
    assert len(imports) == 1
    assert len(imports[0].contacts) == 1
    assert imports[0].contacts[0].phone == "+15551234567"
    invitation = next(
        r for r in client.requests if r.__class__.__name__ == "InviteToChannelRequest"
    )
    assert invitation.users[0].user_id == 3001
    assert invitation.users[0].access_hash == 333333


@pytest.mark.asyncio
async def test_phone_resolution_avoids_contact_import_when_privacy_allows_it():
    member = _member(user_id=3001)
    client = OwnerPeerClient(
        resolve_id=None,
        phone_resolve_id=3001,
        participants=[None, member, member],
    )
    adapter, _, _ = _user_invite_case(
        client,
        phone="15551234567",
        username=None,
    )

    result = await adapter.execute_item(
        _asset(), OwnedGroupOperation(id=30), _operation_item()
    )

    assert result.success
    assert _request_names(client).count("ResolvePhoneRequest") == 1
    assert "ImportContactsRequest" not in _request_names(client)
    invitation = next(
        request
        for request in client.requests
        if request.__class__.__name__ == "InviteToChannelRequest"
    )
    assert invitation.users[0].user_id == 3001
    assert invitation.users[0].access_hash == 444444


@pytest.mark.asyncio
@pytest.mark.parametrize("imported_id,client_id", [(2001, 0), (3001, 9)])
async def test_contact_import_must_match_selected_identity_and_request(imported_id, client_id):
    client = OwnerPeerClient(resolve_id=None, imported_id=imported_id, import_client_id=client_id)
    adapter, _, _ = _user_invite_case(client, phone="15551234567")
    result = await adapter.execute_item(
        _asset(), OwnedGroupOperation(id=30), _operation_item(admin=True)
    )
    assert not result.success
    assert result.reason_code == "target_peer_unavailable"
    assert _request_names(client) == ["ResolvePhoneRequest", "ImportContactsRequest"]


@pytest.mark.asyncio
async def test_reconcile_does_not_import_missing_peer_even_with_known_target_phone():
    client = OwnerPeerClient(resolve_id=None)
    adapter, _, _ = _user_invite_case(client, phone="15551234567")
    result = await adapter.reconcile_item(_asset(), OwnedGroupOperation(id=30), _operation_item())
    assert not result.success
    assert result.reason_code == "target_peer_unavailable"
    assert not client.requests


@pytest.mark.asyncio
async def test_contact_import_rechecks_safety_gate_immediately_before_write():
    decisions = [
        SafetyGateDecision(True, "eligible"),
        SafetyGateDecision(False, "global_stop_enabled"),
    ]
    precheck = AsyncMock(side_effect=decisions)
    client = OwnerPeerClient(resolve_id=None)
    adapter, _, _ = _user_invite_case(client, phone="15551234567", precheck=precheck)
    result = await adapter.execute_item(_asset(), OwnedGroupOperation(id=30), _operation_item())
    assert not result.success
    assert result.reason_code == "global_stop_enabled"
    assert precheck.await_count == 2
    assert _request_names(client) == ["ResolvePhoneRequest"]


@pytest.mark.asyncio
async def test_admin_verification_cannot_accept_owner_creator_for_target():
    client = OwnerPeerClient(participants=[_member(user_id=3001), _creator()])
    adapter, _, _ = _user_invite_case(client)
    result = await adapter.execute_item(
        _asset(), OwnedGroupOperation(id=30), _operation_item(admin=True)
    )
    assert not result.success
    assert result.status == ItemStatus.ADMIN_PROMOTING.value
    assert result.is_admin is False
    promotion = next(r for r in client.requests if r.__class__.__name__ == "EditAdminRequest")
    assert isinstance(promotion.user_id, types.InputUser)
    assert promotion.user_id.user_id == 3001


@pytest.mark.asyncio
async def test_existing_group_owner_must_match_creator_identity():
    owner = _account(1)
    client = FakeClient(participants=[_creator(user_id=3001)])
    wrapper = SimpleNamespace(account_id=1, client=client)
    adapter = _adapter(FakeDB({(TelegramAccount, 1): owner}), FakePool({1: wrapper}))
    result = await adapter.create_group(_asset(), owner)
    assert not result.success
    assert result.reason_code == "owner_not_group_member"
    assert _request_names(client) == ["GetParticipantRequest"]
