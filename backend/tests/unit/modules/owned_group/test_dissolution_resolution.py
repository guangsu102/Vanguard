from __future__ import annotations

import importlib
import json

import pytest
from sqlalchemy import select

from app.core.account.models import AccountStatus, AccountType, TelegramAccount
from app.core.security import get_current_user
from app.main import app
from app.modules.owned_group.models import OwnedGroupAsset, OwnedGroupOperation
from app.modules.owned_group.models_extra import OwnedGroupAuditEvent


@pytest.fixture(autouse=True)
def override_admin_user():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 9301,
        "username": "dissolution-review-admin",
        "role": "admin",
    }
    yield
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture(autouse=True)
def disable_module_gate(monkeypatch):
    monkeypatch.setattr(
        importlib.import_module("app.api.owned_groups"),
        "require_owned_group_module_enabled",
        lambda: None,
    )


async def _seed_asset(
    test_db,
    *,
    asset_status: str = "needs_attention",
    operation_status: str = "unknown",
    operation_type: str = "dissolve",
):
    suffix = f"{asset_status}-{operation_status}-{operation_type}"
    owner = TelegramAccount(
        identifier=f"resolution-owner-{suffix}",
        session_name=f"resolution-owner-{suffix}",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    test_db.add(owner)
    await test_db.flush()
    asset = OwnedGroupAsset(
        internal_name=f"resolution-{suffix}",
        title="Dissolution Resolution Test Group",
        visibility="private",
        owner_account_id=owner.id,
        invite_mode="direct_invite",
        status=asset_status,
        telegram_chat_id=-(2000000000000 + owner.id),
        governance_status="disabled",
        member_count=128,
    )
    test_db.add(asset)
    await test_db.flush()
    operation = None
    if operation_status is not None:
        operation = OwnedGroupOperation(
            group_asset_id=asset.id,
            operation_type=operation_type,
            status=operation_status,
            selection_snapshot="[]",
            selection_snapshot_hash="c" * 64,
            config_snapshot=json.dumps({"operation": operation_type, "max_attempts": 1}),
            config_snapshot_hash="d" * 64,
            idempotency_key=f"resolution-{suffix}-{owner.id}",
            planned_count=1 if operation_type == "dissolve" else 0,
        )
        test_db.add(operation)
        await test_db.flush()
    await test_db.commit()
    return asset.id, operation.id if operation is not None else None


@pytest.mark.asyncio
async def test_resolution_requires_exact_phrase(client, test_db):
    asset_id, _ = await _seed_asset(test_db)

    wrong = await client.post(
        f"/api/owned-groups/{asset_id}/dissolution/resolution",
        json={"outcome": "archived", "confirmation": f"WRONG PHRASE {asset_id}"},
    )
    assert wrong.status_code == 409
    assert (
        wrong.json()["detail"]["reason"] == "dissolution_resolution_confirmation_invalid"
    )


@pytest.mark.asyncio
async def test_resolution_archived_closes_operation_and_audits(client, test_db):
    asset_id, operation_id = await _seed_asset(test_db, operation_status="unknown")

    resolved = await client.post(
        f"/api/owned-groups/{asset_id}/dissolution/resolution",
        json={"outcome": "archived", "confirmation": f"CONFIRM DISSOLVED {asset_id}"},
    )
    assert resolved.status_code == 200
    payload = resolved.json()
    assert payload["code"] == 0
    assert payload["data"]["status"] == "archived"
    assert payload["data"]["operation_id"] == operation_id
    assert payload["data"]["operation_status"] == "completed"

    test_db.expire_all()
    asset = await test_db.get(OwnedGroupAsset, asset_id)
    operation = await test_db.get(OwnedGroupOperation, operation_id)
    assert asset is not None and asset.status == "archived"
    assert asset.archived_at is not None
    assert asset.member_count == 0
    assert operation is not None and operation.status == "completed"
    assert operation.completed_count == 1
    audit = await test_db.scalar(
        select(OwnedGroupAuditEvent).where(
            OwnedGroupAuditEvent.operation_id == operation_id,
            OwnedGroupAuditEvent.event_type
            == "owned_group_dissolution_manually_resolved",
        )
    )
    assert audit is not None
    assert audit.reason_code == "administrator_confirmed_dissolved"

    detail = await client.get(f"/api/owned-groups/{asset_id}")
    assert detail.status_code == 200
    assert detail.json()["pending_dissolution_review"] is False


@pytest.mark.asyncio
async def test_resolution_still_exists_restores_ready_and_audits(client, test_db):
    asset_id, operation_id = await _seed_asset(test_db, operation_status="unknown")

    resolved = await client.post(
        f"/api/owned-groups/{asset_id}/dissolution/resolution",
        json={"outcome": "still_exists", "confirmation": f"CONFIRM EXISTS {asset_id}"},
    )
    assert resolved.status_code == 200
    payload = resolved.json()
    assert payload["data"]["status"] == "ready"
    assert payload["data"]["operation_status"] == "failed"

    test_db.expire_all()
    asset = await test_db.get(OwnedGroupAsset, asset_id)
    operation = await test_db.get(OwnedGroupOperation, operation_id)
    assert asset is not None and asset.status == "ready"
    assert asset.archived_at is None
    assert operation is not None and operation.status == "failed"
    assert operation.last_error == "administrator_confirmed_group_still_exists"
    audit = await test_db.scalar(
        select(OwnedGroupAuditEvent).where(
            OwnedGroupAuditEvent.operation_id == operation_id,
            OwnedGroupAuditEvent.event_type
            == "owned_group_dissolution_manually_resolved",
        )
    )
    assert audit is not None
    assert audit.reason_code == "administrator_confirmed_group_still_exists"


@pytest.mark.asyncio
async def test_resolution_replays_idempotently_for_same_verdict(client, test_db):
    asset_id, _ = await _seed_asset(test_db, operation_status="failed")
    body = {"outcome": "still_exists", "confirmation": f"CONFIRM EXISTS {asset_id}"}

    first = await client.post(
        f"/api/owned-groups/{asset_id}/dissolution/resolution", json=body
    )
    assert first.status_code == 200
    replay = await client.post(
        f"/api/owned-groups/{asset_id}/dissolution/resolution", json=body
    )
    assert replay.status_code == 200
    assert replay.json()["data"] == first.json()["data"]

    conflicting = await client.post(
        f"/api/owned-groups/{asset_id}/dissolution/resolution",
        json={"outcome": "archived", "confirmation": f"CONFIRM DISSOLVED {asset_id}"},
    )
    assert conflicting.status_code == 409
    assert (
        conflicting.json()["detail"]["reason"]
        == "asset_not_awaiting_dissolution_review"
    )


@pytest.mark.asyncio
async def test_resolution_rejects_asset_not_awaiting_review(client, test_db):
    asset_id, _ = await _seed_asset(test_db, asset_status="ready", operation_status="failed")

    resolved = await client.post(
        f"/api/owned-groups/{asset_id}/dissolution/resolution",
        json={"outcome": "archived", "confirmation": f"CONFIRM DISSOLVED {asset_id}"},
    )
    assert resolved.status_code == 409
    assert (
        resolved.json()["detail"]["reason"] == "asset_not_awaiting_dissolution_review"
    )


@pytest.mark.asyncio
async def test_resolution_rejects_unfinished_dissolve_operation(client, test_db):
    asset_id, _ = await _seed_asset(
        test_db, asset_status="needs_attention", operation_status="queued"
    )

    resolved = await client.post(
        f"/api/owned-groups/{asset_id}/dissolution/resolution",
        json={"outcome": "archived", "confirmation": f"CONFIRM DISSOLVED {asset_id}"},
    )
    assert resolved.status_code == 409
    assert (
        resolved.json()["detail"]["reason"] == "dissolution_operation_not_reviewable"
    )


@pytest.mark.asyncio
async def test_pending_dissolution_review_flag_follows_latest_operation(client, test_db):
    flagged_id, _ = await _seed_asset(
        test_db, asset_status="needs_attention", operation_status="failed"
    )

    detail = await client.get(f"/api/owned-groups/{flagged_id}")
    assert detail.status_code == 200
    assert detail.json()["pending_dissolution_review"] is True

    listing = await client.get(
        "/api/owned-groups", params={"status": "needs_attention"}
    )
    assert listing.status_code == 200
    flagged_row = next(
        row for row in listing.json()["data"] if row["id"] == flagged_id
    )
    assert flagged_row["pending_dissolution_review"] is True
