from __future__ import annotations

import pytest

from app.core.account.models import AccountStatus, AccountType, TelegramAccount
from app.core.security import get_current_user
from app.main import app
from app.modules.owned_group.models import OwnedGroupAsset


@pytest.fixture(autouse=True)
def override_authenticated_user():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 9011,
        "username": "owned-group-idempotency-test",
        "role": "admin",
    }
    yield
    app.dependency_overrides.pop(get_current_user, None)


async def _seed_ready_asset(test_db):
    owner = TelegramAccount(
        identifier="owned-idempotency-owner",
        session_name="owned-idempotency-owner",
        account_type=AccountType.PROMOTER,
        is_active=True,
        status=AccountStatus.ONLINE,
        session_string="owner-session",
    )
    member = TelegramAccount(
        identifier="owned-idempotency-member",
        session_name="owned-idempotency-member",
        account_type=AccountType.PROMOTER,
        is_active=True,
        status=AccountStatus.ONLINE,
        session_string="member-session",
    )
    test_db.add_all([owner, member])
    await test_db.flush()
    asset = OwnedGroupAsset(
        internal_name="owned-idempotency-asset",
        title="Owned Idempotency Asset",
        visibility="private",
        owner_account_id=owner.id,
        invite_mode="direct_invite",
        status="ready",
    )
    test_db.add(asset)
    await test_db.flush()
    await test_db.commit()
    return asset, member


def _payload(
    member_id: int,
    *,
    batch_size: int = 5,
    admin_permissions: dict[str, bool] | None = None,
):
    return {
        "resources": [
            {
                "resource_type": "user",
                "resource_id": member_id,
                "admin_required": True,
                "admin_permissions": admin_permissions or {"invite_users": True},
                "admin_title": "Operations",
            }
        ],
        "batch_size": batch_size,
    }


@pytest.mark.asyncio
async def test_idempotency_key_rejects_changed_operation_config(client, test_db):
    asset, member = await _seed_ready_asset(test_db)
    first_payload = _payload(member.id)
    first = await client.post(
        f"/api/owned-groups/{asset.id}/operations",
        headers={"Idempotency-Key": "owned-idempotency-config"},
        json=first_payload,
    )
    assert first.status_code == 202

    changed = await client.post(
        f"/api/owned-groups/{asset.id}/operations",
        headers={"Idempotency-Key": "owned-idempotency-config"},
        json=_payload(member.id, batch_size=6),
    )
    assert changed.status_code == 409
    assert changed.json()["detail"]["reason"] == "idempotency_key_payload_mismatch"
    assert changed.json()["detail"]["operation_id"] == first.json()["id"]


@pytest.mark.asyncio
async def test_idempotency_key_rejects_changed_admin_snapshot(client, test_db):
    asset, member = await _seed_ready_asset(test_db)
    first = await client.post(
        f"/api/owned-groups/{asset.id}/operations",
        headers={"Idempotency-Key": "owned-idempotency-admin"},
        json=_payload(member.id),
    )
    assert first.status_code == 202

    changed = await client.post(
        f"/api/owned-groups/{asset.id}/operations",
        headers={"Idempotency-Key": "owned-idempotency-admin"},
        json=_payload(member.id, admin_permissions={"delete_messages": True}),
    )
    assert changed.status_code == 409
    assert changed.json()["detail"]["reason"] == "idempotency_key_payload_mismatch"


@pytest.mark.asyncio
async def test_idempotency_key_rejects_changed_schedule(client, test_db):
    asset, member = await _seed_ready_asset(test_db)
    first = await client.post(
        f"/api/owned-groups/{asset.id}/operations",
        headers={"Idempotency-Key": "owned-idempotency-schedule"},
        json={**_payload(member.id), "schedule_at": "2030-01-01T00:00:00Z"},
    )
    assert first.status_code == 202

    changed = await client.post(
        f"/api/owned-groups/{asset.id}/operations",
        headers={"Idempotency-Key": "owned-idempotency-schedule"},
        json={**_payload(member.id), "schedule_at": "2030-01-01T01:00:00+00:00"},
    )
    assert changed.status_code == 409
    assert changed.json()["detail"]["reason"] == "idempotency_key_payload_mismatch"


@pytest.mark.asyncio
async def test_idempotency_key_replay_with_same_payload_returns_original(client, test_db):
    asset, member = await _seed_ready_asset(test_db)
    payload = _payload(member.id)
    headers = {"Idempotency-Key": "owned-idempotency-replay"}
    first = await client.post(
        f"/api/owned-groups/{asset.id}/operations", headers=headers, json=payload
    )
    second = await client.post(
        f"/api/owned-groups/{asset.id}/operations", headers=headers, json=payload
    )
    assert first.status_code == second.status_code == 202
    assert second.json()["id"] == first.json()["id"]
