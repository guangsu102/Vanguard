from __future__ import annotations

import json

import pytest

from app.core.account.models import AccountType, TelegramAccount
from app.core.security import get_current_user
from app.main import app
from app.modules.owned_group.models import (
    OwnedGroupAsset,
    OwnedGroupOperation,
    OwnedGroupOperationItem,
)


@pytest.fixture(autouse=True)
def override_authenticated_user():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 9021,
        "username": "owned-group-redaction-test",
        "role": "auditor",
    }
    yield
    app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_operation_detail_and_items_redact_persisted_secret_text(client, test_db):
    owner = TelegramAccount(
        identifier="owned-redaction-owner",
        session_name="owned-redaction-owner",
        account_type=AccountType.PROMOTER,
        is_active=True,
    )
    test_db.add(owner)
    await test_db.flush()
    asset = OwnedGroupAsset(
        internal_name="owned-redaction-asset",
        title="Owned Redaction Asset",
        visibility="private",
        owner_account_id=owner.id,
        status="ready",
    )
    test_db.add(asset)
    await test_db.flush()
    operation = OwnedGroupOperation(
        group_asset_id=asset.id,
        operation_type="join",
        status="failed",
        selection_snapshot=json.dumps([{"resource_type": "user", "resource_id": owner.id}]),
        selection_snapshot_hash="a" * 64,
        config_snapshot=json.dumps(
            {
                "batch_size": 5,
                "bot_token": "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcd",
                "invite_link": "https://t.me/+AbCdEfGh12345678",
            }
        ),
        config_snapshot_hash="b" * 64,
        idempotency_key="owned-redaction-operation",
        planned_count=1,
        last_error="token=123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcd",
    )
    test_db.add(operation)
    await test_db.flush()
    test_db.add(
        OwnedGroupOperationItem(
            operation_id=operation.id,
            resource_type="user",
            resource_id=owner.id,
            status="failed_permanent",
            error_message="invite=https://t.me/joinchat/AbCdEfGh12345678",
        )
    )
    await test_db.flush()

    detail = await client.get(
        f"/api/owned-groups/{asset.id}/operations/{operation.id}"
    )
    assert detail.status_code == 200
    body = detail.json()
    assert body["config_snapshot"]["bot_token"] == "[REDACTED]"
    assert "+AbCdEfGh12345678" not in detail.text
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZabcd" not in detail.text

    items = await client.get(f"/api/owned-groups/operations/{operation.id}/items")
    assert items.status_code == 200
    assert "AbCdEfGh12345678" not in items.text
