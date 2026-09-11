"""Mock-only tests for the gate-checked owned-group Telegram adapter."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

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
    classify_telegram_error,
)


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
        if int(value) == 12345:
            return SimpleNamespace(id=12345, megagroup=True, broadcast=False, username="owned")
        return value

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


def _creator(**values):
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
    client = FakeClient(participants=[SimpleNamespace(admin_rights=None)])
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
    non_admin = SimpleNamespace(admin_rights=None)
    admin = SimpleNamespace(
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
    admin = SimpleNamespace(admin_rights=SimpleNamespace(invite_users=True), rank="Ops")
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
            SimpleNamespace(admin_rights=None),
            "admin_not_verified",
            False,
        ),
        (
            SimpleNamespace(
                admin_rights=SimpleNamespace(invite_users=False),
                rank="Ops",
            ),
            "admin_permissions_not_verified",
            True,
        ),
        (
            SimpleNamespace(
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
    non_admin = SimpleNamespace(admin_rights=None)
    wrong_title_admin = SimpleNamespace(
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
