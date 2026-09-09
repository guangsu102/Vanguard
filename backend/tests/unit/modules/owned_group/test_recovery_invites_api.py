from __future__ import annotations

import importlib

import pytest
from sqlalchemy import select

from app.core.account.models import AccountStatus, AccountType, TelegramAccount
from app.core.ephemeral_secret import encrypt_ephemeral_secret
from app.core.security import get_current_user
from app.main import app
from app.modules.owned_group.models import (
    OwnedGroupAsset,
    OwnedGroupOperation,
    OwnedGroupOperationItem,
)
from app.modules.owned_group.models_extra import OwnedGroupAuditEvent, OwnedGroupInviteLink
from app.modules.owned_group.worker import GroupCreateResult, ItemExecutionResult, PreflightResult


@pytest.fixture(autouse=True)
def override_authenticated_user():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 9401,
        "username": "owned-recovery-test",
        "role": "admin",
    }
    yield
    app.dependency_overrides.pop(get_current_user, None)


async def _seed_asset(
    test_db,
    *,
    public_link: str | None = None,
    visibility: str = "private",
    asset_status: str = "ready",
    telegram_chat_id: int | None = None,
    telegram_username: str | None = None,
):
    owner = TelegramAccount(
        identifier="owned-recovery-invite-owner",
        session_name="owned-recovery-invite-owner",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
        session_string="owner-session",
    )
    test_db.add(owner)
    await test_db.flush()
    asset = OwnedGroupAsset(
        internal_name="owned-recovery-invite-asset",
        title="Recovery Invite",
        visibility=visibility,
        telegram_username=telegram_username,
        public_link=public_link,
        owner_account_id=owner.id,
        invite_mode="link_self_join",
        status=asset_status,
        telegram_chat_id=telegram_chat_id,
    )
    test_db.add(asset)
    await test_db.flush()
    return asset, owner


@pytest.mark.asyncio
async def test_admin_can_reconcile_create_timeout_without_replaying_group_create(
    client, test_db, monkeypatch
):
    asset, _ = await _seed_asset(test_db, asset_status="needs_attention")
    await test_db.commit()

    class ExistingGroupAdapter:
        async def preflight(self, received_asset, owner):
            assert owner.id == received_asset.owner_account_id
            return PreflightResult(ready=True)

        async def create_group(self, received_asset, owner):
            # The control must bind the candidate first, which forces the real
            # adapter to reconcile instead of creating a second group.
            assert received_asset.telegram_chat_id == -100777888999
            return GroupCreateResult(
                success=True,
                telegram_chat_id=received_asset.telegram_chat_id,
                reason_code="already_created",
            )

    async def allow_execution():
        return None

    controls_api = importlib.import_module("app.api.owned_group_controls")
    factory = importlib.import_module("app.modules.owned_group.factory")
    monkeypatch.setattr(controls_api, "require_owned_group_execution_enabled", allow_execution)
    monkeypatch.setattr(factory, "build_owned_group_adapter", lambda db: ExistingGroupAdapter())

    response = await client.post(
        f"/api/owned-groups/{asset.id}/reconcile",
        json={"telegram_chat_id": -100777888999},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    await test_db.refresh(asset)
    assert asset.status == "ready"
    assert asset.telegram_chat_id == -100777888999


@pytest.mark.asyncio
async def test_admin_can_replace_public_username_during_asset_reconciliation(
    client, test_db, monkeypatch
):
    asset, _ = await _seed_asset(
        test_db,
        visibility="public",
        asset_status="needs_attention",
        telegram_chat_id=-100777000111,
        telegram_username="occupied_name",
    )
    await test_db.commit()

    class ExistingPublicGroupAdapter:
        async def preflight(self, received_asset, owner):
            return PreflightResult(ready=True)

        async def create_group(self, received_asset, owner):
            assert received_asset.telegram_chat_id == -100777000111
            assert received_asset.telegram_username == "available_name"
            return GroupCreateResult(
                success=True,
                telegram_chat_id=received_asset.telegram_chat_id,
                telegram_username=received_asset.telegram_username,
                public_link="https://t.me/available_name",
                reason_code="already_created",
            )

    async def allow_execution():
        return None

    controls_api = importlib.import_module("app.api.owned_group_controls")
    factory = importlib.import_module("app.modules.owned_group.factory")
    monkeypatch.setattr(controls_api, "require_owned_group_execution_enabled", allow_execution)
    monkeypatch.setattr(
        factory, "build_owned_group_adapter", lambda db: ExistingPublicGroupAdapter()
    )

    response = await client.post(
        f"/api/owned-groups/{asset.id}/reconcile",
        json={"telegram_username": "@available_name"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    await test_db.refresh(asset)
    assert asset.telegram_username == "available_name"
    assert asset.public_link == "https://t.me/available_name"


@pytest.mark.asyncio
async def test_asset_reconcile_rejects_invalid_chat_id_and_username(client, test_db):
    asset, _ = await _seed_asset(test_db, asset_status="needs_attention")
    await test_db.commit()

    zero_chat = await client.post(
        f"/api/owned-groups/{asset.id}/reconcile",
        json={"telegram_chat_id": 0},
    )
    invalid_username = await client.post(
        f"/api/owned-groups/{asset.id}/reconcile",
        json={"telegram_username": "bad name"},
    )

    assert zero_chat.status_code == 422
    assert invalid_username.status_code == 422


@pytest.mark.asyncio
async def test_operator_cannot_bind_asset_reconciliation_candidate(client, test_db):
    asset, _ = await _seed_asset(test_db, asset_status="needs_attention")
    await test_db.commit()
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 9403,
        "username": "owned-reconcile-operator-test",
        "role": "operator",
    }

    response = await client.post(
        f"/api/owned-groups/{asset.id}/reconcile",
        json={"telegram_chat_id": -100777888999},
    )

    assert response.status_code == 403
    await test_db.refresh(asset)
    assert asset.telegram_chat_id is None
    assert asset.status == "needs_attention"


@pytest.mark.asyncio
async def test_invite_links_are_decrypted_only_for_explicit_authenticated_read(client, test_db):
    asset, _ = await _seed_asset(
        test_db,
        public_link="https://t.me/owned_public",
        visibility="public",
    )
    ciphertext = encrypt_ephemeral_secret("https://t.me/+AbCdEfGh12345678")
    test_db.add(
        OwnedGroupInviteLink(
            group_asset_id=asset.id,
            link_type="member_invite",
            link_ciphertext=ciphertext,
            is_active=True,
        )
    )
    await test_db.commit()

    response = await client.get(f"/api/owned-groups/{asset.id}/invite-links")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, private"
    body = response.json()
    links = {row["link_type"]: row for row in body["data"]}
    assert links["public"]["link"] == "https://t.me/owned_public"
    assert links["member_invite"]["link"] == "https://t.me/+AbCdEfGh12345678"
    assert ciphertext not in response.text
    audit = await test_db.scalar(
        select(OwnedGroupAuditEvent).where(
            OwnedGroupAuditEvent.event_type == "owned_group_invite_link_viewed"
        )
    )
    assert audit is not None
    assert "+AbCdEfGh12345678" not in (audit.after_state or "")


@pytest.mark.asyncio
async def test_private_asset_never_exposes_inconsistent_public_link(client, test_db):
    asset, _ = await _seed_asset(test_db, public_link="https://t.me/owned_public")
    await test_db.commit()

    response = await client.get(f"/api/owned-groups/{asset.id}/invite-links")

    assert response.status_code == 200
    assert response.json()["data"] == []


@pytest.mark.asyncio
async def test_admin_can_revoke_invite_through_guarded_adapter(client, test_db, monkeypatch):
    asset, _ = await _seed_asset(test_db)
    invite = OwnedGroupInviteLink(
        group_asset_id=asset.id,
        link_type="member_invite",
        link_ciphertext=encrypt_ephemeral_secret("https://t.me/+AbCdEfGh12345678"),
        is_active=True,
    )
    test_db.add(invite)
    await test_db.commit()

    class FakeAdapter:
        async def revoke_invite_link(self, received_asset, received_invite):
            assert received_asset.id == asset.id
            assert received_invite.id == invite.id
            received_invite.is_active = False
            return {"invite_id": received_invite.id, "status": "revoked"}

        async def regenerate_invite_link(self, *args, **kwargs):
            raise AssertionError("not used")

    async def allow_execution():
        return None

    import app.modules.owned_group.factory as factory

    invite_api = importlib.import_module("app.api.owned_group_invites")
    monkeypatch.setattr(invite_api, "require_owned_group_execution_enabled", allow_execution)
    monkeypatch.setattr(factory, "build_owned_group_adapter", lambda db: FakeAdapter())

    response = await client.post(f"/api/owned-groups/{asset.id}/invite-links/{invite.id}/revoke")

    assert response.status_code == 200
    assert response.json()["data"] == {"invite_id": invite.id, "status": "revoked"}
    await test_db.refresh(invite)
    assert invite.is_active is False
    audit = await test_db.scalar(
        select(OwnedGroupAuditEvent).where(
            OwnedGroupAuditEvent.event_type == "owned_group_invite_link_revoked"
        )
    )
    assert audit is not None
    assert "t.me" not in str(audit.before_state or "")
    assert "t.me" not in str(audit.after_state or "")


@pytest.mark.asyncio
async def test_regenerate_response_never_echoes_plaintext_link(client, test_db, monkeypatch):
    asset, _ = await _seed_asset(test_db)
    await test_db.commit()

    class FakeAdapter:
        async def revoke_invite_link(self, *args, **kwargs):
            raise AssertionError("not used")

        async def regenerate_invite_link(self, received_asset, **kwargs):
            assert received_asset.id == asset.id
            assert kwargs["request_needed"] is True
            replacement = OwnedGroupInviteLink(
                group_asset_id=asset.id,
                link_type="join_request",
                link_ciphertext=encrypt_ephemeral_secret("https://t.me/+NewSecretHash1234"),
                is_active=True,
                created_by=kwargs["created_by"],
            )
            test_db.add(replacement)
            await test_db.flush()
            # A malicious/buggy adapter result must still not be echoed by the API.
            return {
                "invite_id": replacement.id,
                "status": "active",
                "link_type": "join_request",
                "link": "https://t.me/+NewSecretHash1234",
            }

    async def allow_execution():
        return None

    import app.modules.owned_group.factory as factory

    invite_api = importlib.import_module("app.api.owned_group_invites")
    monkeypatch.setattr(invite_api, "require_owned_group_execution_enabled", allow_execution)
    monkeypatch.setattr(factory, "build_owned_group_adapter", lambda db: FakeAdapter())

    response = await client.post(
        f"/api/owned-groups/{asset.id}/invite-links/regenerate",
        json={"request_needed": True},
    )

    assert response.status_code == 200
    assert "NewSecretHash1234" not in response.text
    assert response.json()["data"]["link_type"] == "join_request"


@pytest.mark.asyncio
async def test_operator_cannot_mutate_invite_link(client, test_db):
    asset, _ = await _seed_asset(test_db)
    invite = OwnedGroupInviteLink(
        group_asset_id=asset.id,
        link_type="member_invite",
        link_ciphertext=encrypt_ephemeral_secret("https://t.me/+AbCdEfGh12345678"),
        is_active=True,
    )
    test_db.add(invite)
    await test_db.commit()
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 9402,
        "username": "owned-operator-test",
        "role": "operator",
    }

    response = await client.post(f"/api/owned-groups/{asset.id}/invite-links/{invite.id}/revoke")

    assert response.status_code == 403
    await test_db.refresh(invite)
    assert invite.is_active is True


@pytest.mark.asyncio
async def test_corrupt_invite_ciphertext_is_not_exposed(client, test_db):
    asset, _ = await _seed_asset(test_db)
    test_db.add(
        OwnedGroupInviteLink(
            group_asset_id=asset.id,
            link_type="member_invite",
            link_ciphertext="not-a-valid-ciphertext",
            is_active=True,
        )
    )
    await test_db.commit()

    response = await client.get(f"/api/owned-groups/{asset.id}/invite-links")

    assert response.status_code == 200
    row = response.json()["data"][0]
    assert row["link"] is None
    assert row["available"] is False
    assert row["status"] == "unavailable"
    assert "not-a-valid-ciphertext" not in response.text


@pytest.mark.asyncio
async def test_reconcile_control_uses_injected_concrete_adapter(client, test_db, monkeypatch):
    asset, owner = await _seed_asset(test_db)
    operation = OwnedGroupOperation(
        group_asset_id=asset.id,
        operation_type="join",
        status="stopping",
        selection_snapshot="[]",
        selection_snapshot_hash="a" * 64,
        config_snapshot="{}",
        config_snapshot_hash="b" * 64,
        idempotency_key="owned-recovery-api-reconcile",
        planned_count=1,
    )
    test_db.add(operation)
    await test_db.flush()
    item = OwnedGroupOperationItem(
        operation_id=operation.id,
        resource_type="user",
        resource_id=owner.id,
        status="invite_sent",
    )
    test_db.add(item)
    await test_db.commit()

    class CountingAdapter:
        def __init__(self):
            self.calls = 0

        async def reconcile_item(self, asset, operation, item):
            self.calls += 1
            return ItemExecutionResult(status="member_verified", success=True, telegram_user_id=123)

    adapter = CountingAdapter()
    factory = __import__("app.modules.owned_group.factory", fromlist=["build_owned_group_adapter"])
    monkeypatch.setattr(
        factory, "build_owned_group_adapter", lambda db, allow_read_only=False: adapter
    )

    response = await client.post(f"/api/owned-groups/operations/{operation.id}/reconcile")

    assert response.status_code == 202
    assert adapter.calls == 1
    assert response.json()["status"] == "stopped"


@pytest.mark.asyncio
async def test_control_operation_detail_redacts_last_error(client, test_db):
    asset, _ = await _seed_asset(test_db)
    operation = OwnedGroupOperation(
        group_asset_id=asset.id,
        operation_type="join",
        status="failed",
        selection_snapshot="[]",
        selection_snapshot_hash="c" * 64,
        config_snapshot="{}",
        config_snapshot_hash="d" * 64,
        idempotency_key="owned-recovery-control-redaction",
        planned_count=1,
        last_error="bot_token=123456789:ABCDEFGHIJKLMNOPQRSTUV invite=https://t.me/+SecretHash123456",
    )
    test_db.add(operation)
    await test_db.commit()

    response = await client.get(f"/api/owned-groups/operations/{operation.id}")

    assert response.status_code == 200
    assert "ABCDEFGHIJKLMNOPQRSTUV" not in response.text
    assert "SecretHash123456" not in response.text
