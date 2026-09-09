from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.api.owned_groups import _is_admin_assignment_conflict
from app.core.account.models import AccountStatus, AccountType, TelegramAccount
from app.core.security import get_current_user
from app.main import app
from app.modules.owned_group.models import (
    OwnedGroupAsset,
    OwnedGroupOperation,
    OwnedGroupOperationItem,
)
from app.modules.owned_group.models_extra import OwnedBotProfile, OwnedGroupAdminAssignment


@pytest.fixture(autouse=True)
def override_authenticated_user():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 9001,
        "username": "owned-group-test",
        "role": "admin",
    }
    yield
    app.dependency_overrides.pop(get_current_user, None)


async def _seed_ready_asset(test_db):
    owner = TelegramAccount(
        identifier="owned-group-owner",
        session_name="owned-group-owner",
        account_type=AccountType.PROMOTER,
        is_active=True,
        status=AccountStatus.ONLINE,
        session_string="owner-session",
    )
    member = TelegramAccount(
        identifier="owned-group-member",
        session_name="owned-group-member",
        account_type=AccountType.PROMOTER,
        is_active=True,
        status=AccountStatus.ONLINE,
        session_string="member-session",
    )
    test_db.add_all([owner, member])
    await test_db.flush()
    asset = OwnedGroupAsset(
        internal_name="owned-group-test",
        title="Owned Group Test",
        visibility="private",
        owner_account_id=owner.id,
        invite_mode="direct_invite",
        status="ready",
    )
    test_db.add(asset)
    await test_db.flush()
    await test_db.commit()
    return asset, owner, member


@pytest.mark.parametrize(
    "message",
    [
        'duplicate key value violates unique constraint "uq_owned_group_admin_resource"',
        "UNIQUE constraint failed: owned_group_admin_assignments.group_asset_id",
    ],
)
def test_admin_assignment_conflict_detection(message):
    error = IntegrityError("INSERT", {}, RuntimeError(message))
    assert _is_admin_assignment_conflict(error)


def test_other_integrity_error_is_not_admin_assignment_conflict():
    error = IntegrityError(
        "INSERT", {}, RuntimeError("UNIQUE constraint failed: owned_group_operation_items.id")
    )
    assert not _is_admin_assignment_conflict(error)


@pytest.mark.asyncio
async def test_draft_records_creator(client, test_db):
    owner = TelegramAccount(
        identifier="owned-group-draft-owner",
        session_name="owned-group-draft-owner",
        account_type=AccountType.PROMOTER,
        is_active=True,
    )
    test_db.add(owner)
    await test_db.flush()
    await test_db.commit()

    response = await client.post(
        "/api/owned-groups/drafts",
        json={
            "internal_name": "owned-group-draft",
            "title": "Owned Group Draft",
            "visibility": "private",
            "owner_account_id": owner.id,
        },
    )

    assert response.status_code == 201
    asset = await test_db.get(OwnedGroupAsset, response.json()["id"])
    assert asset is not None
    assert asset.created_by == 9001


@pytest.mark.asyncio
async def test_draft_rejects_whitespace_required_fields(client):
    response = await client.post(
        "/api/owned-groups/drafts",
        json={
            "internal_name": " 	 ",
            "title": "Valid title",
            "visibility": "private",
            "owner_account_id": 1,
        },
    )

    assert response.status_code == 422
    assert "internal_name" in response.text


@pytest.mark.asyncio
async def test_draft_rejects_whitespace_title(client):
    response = await client.post(
        "/api/owned-groups/drafts",
        json={
            "internal_name": "Valid internal name",
            "title": "   ",
            "visibility": "private",
            "owner_account_id": 1,
        },
    )

    assert response.status_code == 422
    assert "title" in response.text


@pytest.mark.asyncio
async def test_auditor_cannot_create_owned_group_resources(client):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 9002,
        "username": "owned-group-auditor",
        "role": "auditor",
    }

    draft_response = await client.post(
        "/api/owned-groups/drafts",
        json={
            "internal_name": "auditor-owned-group",
            "title": "Auditor Owned Group",
            "visibility": "private",
            "owner_account_id": 1,
        },
    )
    assert draft_response.status_code == 403

    operation_response = await client.post(
        "/api/owned-groups/1/operations",
        headers={"Idempotency-Key": "auditor-op-1"},
        json={"resources": [{"resource_type": "user", "resource_id": 1}]},
    )
    assert operation_response.status_code == 403


@pytest.mark.asyncio
async def test_operation_freezes_owner_and_admin_contract_without_telegram(
    client,
    test_db,
):
    asset, owner, member = await _seed_ready_asset(test_db)
    payload = {
        "resources": [
            {
                "resource_type": "user",
                "resource_id": member.id,
                "admin_required": True,
                "admin_permissions": {"delete_messages": True, "invite_users": False},
                "admin_title": " Content Admin ",
            }
        ],
        "batch_size": 3,
    }

    response = await client.post(
        f"/api/owned-groups/{asset.id}/operations",
        headers={"Idempotency-Key": "owned-op-1"},
        json=payload,
    )
    assert response.status_code == 202
    body = response.json()
    assert body["planned_count"] == 2

    operation = await test_db.get(OwnedGroupOperation, body["id"])
    assert operation is not None
    assert operation.created_by == 9001
    selection = json.loads(operation.selection_snapshot)
    assert selection == [
        {"resource_id": owner.id, "resource_type": "user"},
        {"resource_id": member.id, "resource_type": "user"},
    ]
    config = json.loads(operation.config_snapshot)
    assert config["owner_auto_included"] is True
    assert config["batch_size"] == 3
    assert config["admin_configs"] == [
        {
            "admin_permissions": {"delete_messages": True, "invite_users": False},
            "admin_required": True,
            "admin_title": "Content Admin",
            "resource_id": member.id,
            "resource_type": "user",
        }
    ]

    items = (
        await test_db.scalars(
            select(OwnedGroupOperationItem).where(
                OwnedGroupOperationItem.operation_id == operation.id
            )
        )
    ).all()
    by_resource = {(item.resource_type, item.resource_id): item for item in items}
    owner_item = by_resource[("user", owner.id)]
    assert owner_item.status == "skipped_already_member"
    assert owner_item.reason_code == "already_member"
    assert owner_item.admin_required is False
    assert by_resource[("user", member.id)].admin_required is True

    assignment_count = await test_db.scalar(
        select(func.count(OwnedGroupAdminAssignment.id)).where(
            OwnedGroupAdminAssignment.group_asset_id == asset.id
        )
    )
    assert assignment_count == 1


    assignment = await test_db.scalar(
        select(OwnedGroupAdminAssignment).where(
            OwnedGroupAdminAssignment.group_asset_id == asset.id
        )
    )
    assert assignment is not None
    assert assignment.created_by == 9001

    explicit_owner = await client.post(
        f"/api/owned-groups/{asset.id}/operations",
        headers={"Idempotency-Key": "owned-op-3"},
        json={
            "resources": [
                {
                    "resource_type": "user",
                    "resource_id": owner.id,
                    "admin_required": True,
                    "admin_permissions": {"invite_users": True},
                    "admin_title": "Owner",
                },
                {
                    "resource_type": "user",
                    "resource_id": member.id,
                },
            ]
        },
    )
    assert explicit_owner.status_code == 202
    explicit_operation = await test_db.get(OwnedGroupOperation, explicit_owner.json()["id"])
    assert explicit_operation is not None
    assert explicit_operation.created_by == 9001
    assert json.loads(explicit_operation.selection_snapshot) == [
        {"resource_id": owner.id, "resource_type": "user"},
        {"resource_id": member.id, "resource_type": "user"},
    ]
    assert json.loads(explicit_operation.config_snapshot)["owner_auto_included"] is False
    explicit_items = (
        await test_db.scalars(
            select(OwnedGroupOperationItem).where(
                OwnedGroupOperationItem.operation_id == explicit_operation.id,
                OwnedGroupOperationItem.resource_id == owner.id,
            )
        )
    ).all()
    assert len(explicit_items) == 1
    assert explicit_items[0].admin_required is False
    assignment_count = await test_db.scalar(
        select(func.count(OwnedGroupAdminAssignment.id)).where(
            OwnedGroupAdminAssignment.group_asset_id == asset.id
        )
    )
    assert assignment_count == 1

    duplicate = await client.post(
        f"/api/owned-groups/{asset.id}/operations",
        headers={"Idempotency-Key": "owned-op-1"},
        json=payload,
    )
    assert duplicate.status_code == 202
    assert duplicate.json()["id"] == body["id"]

    repeated = await client.post(
        f"/api/owned-groups/{asset.id}/operations",
        headers={"Idempotency-Key": "owned-op-2"},
        json=payload,
    )
    assert repeated.status_code == 202
    assignment_count = await test_db.scalar(
        select(func.count(OwnedGroupAdminAssignment.id)).where(
            OwnedGroupAdminAssignment.group_asset_id == asset.id
        )
    )
    assert assignment_count == 1


@pytest.mark.asyncio
async def test_strict_operation_precheck_is_read_only_and_checks_session(client, test_db):
    asset, owner, member = await _seed_ready_asset(test_db)

    response = await client.post(
        f"/api/owned-groups/{asset.id}/operations/precheck",
        json={"resources": [{"resource_type": "user", "resource_id": member.id}]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["allowed"] is True
    assert body["reason"] == "eligible"
    assert body["owner_auto_included"] is True
    assert body["selection_snapshot"] == [
        {"resource_type": "user", "resource_id": owner.id},
        {"resource_type": "user", "resource_id": member.id},
    ]
    assert body["config_snapshot"]["owner_auto_included"] is True
    assert await test_db.scalar(select(func.count(OwnedGroupOperation.id))) == 0

    member.session_string = None
    member.auth_key_base64 = None
    await test_db.flush()
    failed = await client.post(
        f"/api/owned-groups/{asset.id}/operations/precheck",
        json={"resources": [{"resource_type": "user", "resource_id": member.id}]},
    )
    assert failed.status_code == 200
    failed_body = failed.json()
    assert failed_body["allowed"] is False
    assert any(
        violation["resource_id"] == member.id
        and violation["reason"] == "account_session_missing"
        for violation in failed_body["details"]["violations"]
    )
    assert await test_db.scalar(select(func.count(OwnedGroupOperation.id))) == 0


@pytest.mark.asyncio
async def test_strict_operation_precheck_uses_owned_bot_profile_id(client, test_db):
    asset, owner, _member = await _seed_ready_asset(test_db)
    bot_account = TelegramAccount(
        identifier="owned-group-bot-account",
        session_name="owned-group-bot-account",
        account_type=AccountType.GUARDIAN_BOT,
        is_active=True,
        status=AccountStatus.ONLINE,
        session_string="bot-session",
    )
    test_db.add(bot_account)
    await test_db.flush()
    profile = OwnedBotProfile(
        owner_account_id=owner.id,
        account_id=bot_account.id,
        token_ciphertext="encrypted-token",
        status="verified",
        enabled=True,
    )
    test_db.add(profile)
    await test_db.flush()

    response = await client.post(
        f"/api/owned-groups/{asset.id}/operations/precheck",
        json={"resources": [{"resource_type": "bot", "resource_id": profile.id}]},
    )
    assert response.status_code == 200
    assert response.json()["allowed"] is True

    raw_account_id = await client.post(
        f"/api/owned-groups/{asset.id}/operations/precheck",
        json={"resources": [{"resource_type": "bot", "resource_id": bot_account.id}]},
    )
    assert raw_account_id.status_code == 200
    assert raw_account_id.json()["allowed"] is False
    assert any(
        item["reason"] == "bot_profile_not_found"
        for item in raw_account_id.json()["details"]["violations"]
    )


@pytest.mark.asyncio
async def test_operation_queries_are_readable_and_asset_scoped(client, test_db):
    asset, owner, member = await _seed_ready_asset(test_db)
    create = await client.post(
        f"/api/owned-groups/{asset.id}/operations",
        headers={"Idempotency-Key": "owned-query-op-1"},
        json={
            "resources": [
                {
                    "resource_type": "user",
                    "resource_id": member.id,
                    "admin_required": True,
                    "admin_permissions": {"invite_users": True},
                    "admin_title": "Query Admin",
                }
            ]
        },
    )
    assert create.status_code == 202
    operation_id = create.json()["id"]

    app.dependency_overrides[get_current_user] = lambda: {
        "id": 9003,
        "username": "owned-group-auditor",
        "role": "auditor",
    }
    listed = await client.get(f"/api/owned-groups/{asset.id}/operations")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["data"][0]["id"] == operation_id

    detail = await client.get(
        f"/api/owned-groups/{asset.id}/operations/{operation_id}"
    )
    assert detail.status_code == 200
    assert detail.json()["items_total"] == 2
    assert detail.json()["selection_snapshot"]
    assert detail.json()["config_snapshot"]["admin_configs"][0]["admin_required"] is True

    items = await client.get(
        f"/api/owned-groups/{asset.id}/operations/{operation_id}/items",
        params={"status": "pending", "resource_type": "user"},
    )
    assert items.status_code == 200
    assert items.json()["total"] == 1
    assert items.json()["data"][0]["resource_id"] == member.id
    assert items.json()["data"][0]["admin_permissions"] == {"invite_users": True}

    root_items = await client.get(f"/api/owned-groups/operations/{operation_id}/items")
    assert root_items.status_code == 200
    assert root_items.json()["total"] == 2

    root_list = await client.get("/api/owned-groups/operations")
    assert root_list.status_code == 200
    assert root_list.json()["total"] == 1

    wrong_asset = await client.get(
        f"/api/owned-groups/999999/operations/{operation_id}"
    )
    assert wrong_asset.status_code == 404
