from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.account.models import AccountType, TelegramAccount
from app.core.security import get_current_user
from app.main import app
from app.modules.owned_group.models import OwnedGroupAsset, OwnedGroupOperation
from app.modules.owned_group.models_extra import OwnedGroupAuditEvent


@pytest.fixture(autouse=True)
def override_admin_user():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 9101,
        "username": "failed-draft-admin",
        "role": "admin",
    }
    yield
    app.dependency_overrides.pop(get_current_user, None)


async def _seed_failed_draft(test_db, *, operation_status: str):
    owner = TelegramAccount(
        identifier=f"failed-draft-owner-{operation_status}",
        session_name=f"failed-draft-owner-{operation_status}",
        account_type=AccountType.PROMOTER,
        is_active=True,
    )
    test_db.add(owner)
    await test_db.flush()
    asset = OwnedGroupAsset(
        internal_name=f"failed-draft-{operation_status}",
        title="Failed local draft",
        visibility="private",
        owner_account_id=owner.id,
        invite_mode="direct_invite",
        status="needs_attention",
        governance_status="disabled",
    )
    test_db.add(asset)
    await test_db.flush()
    operation = OwnedGroupOperation(
        group_asset_id=asset.id,
        operation_type="create_and_onboard",
        status=operation_status,
        selection_snapshot="[]",
        selection_snapshot_hash="a" * 64,
        config_snapshot="{}",
        config_snapshot_hash="b" * 64,
        idempotency_key=f"failed-draft-{operation_status}-operation",
    )
    test_db.add(operation)
    await test_db.flush()
    audit = OwnedGroupAuditEvent(
        event_type="operation_failed",
        group_asset_id=asset.id,
        operation_id=operation.id,
        result="failed",
    )
    test_db.add(audit)
    await test_db.commit()
    return asset.id, operation.id, audit.id


@pytest.mark.asyncio
async def test_delete_failed_draft_requires_confirmation_and_preserves_audit(
    client,
    test_db,
):
    asset_id, operation_id, audit_id = await _seed_failed_draft(
        test_db,
        operation_status="failed",
    )

    missing_confirmation = await client.delete(f"/api/owned-groups/{asset_id}")
    assert missing_confirmation.status_code == 409
    assert missing_confirmation.json()["detail"]["reason"] == "delete_confirmation_required"
    assert await test_db.get(OwnedGroupAsset, asset_id) is not None

    deleted = await client.delete(
        f"/api/owned-groups/{asset_id}",
        params={"confirm_no_telegram_group": "true"},
    )
    assert deleted.status_code == 200
    assert deleted.json()["data"] == {"id": asset_id}
    test_db.expire_all()
    assert await test_db.get(OwnedGroupAsset, asset_id) is None
    assert await test_db.get(OwnedGroupOperation, operation_id) is None

    original_audit = await test_db.get(OwnedGroupAuditEvent, audit_id)
    assert original_audit is not None
    assert original_audit.group_asset_id is None
    assert original_audit.operation_id is None
    deletion_audit = await test_db.scalar(
        select(OwnedGroupAuditEvent).where(
            OwnedGroupAuditEvent.event_type == "asset_failed_draft_deleted",
            OwnedGroupAuditEvent.resource_type == "owned_group_asset",
            OwnedGroupAuditEvent.resource_id == asset_id,
        )
    )
    assert deletion_audit is not None
    assert deletion_audit.actor_id == 9101


@pytest.mark.asyncio
async def test_delete_failed_draft_rejects_nonterminal_operation(client, test_db):
    asset_id, _, _ = await _seed_failed_draft(
        test_db,
        operation_status="queued",
    )

    response = await client.delete(
        f"/api/owned-groups/{asset_id}",
        params={"confirm_no_telegram_group": "true"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "asset_operation_not_terminal"
    assert await test_db.get(OwnedGroupAsset, asset_id) is not None
