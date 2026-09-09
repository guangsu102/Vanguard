from __future__ import annotations

import pytest

from app.core.account.models import AccountType, TelegramAccount
from app.modules.owned_group.models import (
    OwnedGroupAsset,
    OwnedGroupOperation,
    OwnedGroupOperationItem,
)
from app.modules.owned_group.worker import ItemExecutionResult, run_owned_group_operation


class InviteOnlyAdapter:
    async def execute_item(self, asset, operation, item):
        return ItemExecutionResult(status="invite_sent")

    async def preflight(self, asset, owner):
        raise AssertionError("not used")

    async def create_group(self, asset, owner):
        raise AssertionError("not used")

    async def reconcile_item(self, asset, operation, item):
        return None


class TransientAdapter:
    async def execute_item(self, asset, operation, item):
        return ItemExecutionResult(transient=True, reason_code="network_timeout")

    async def preflight(self, asset, owner):
        raise AssertionError("not used")

    async def create_group(self, asset, owner):
        raise AssertionError("not used")

    async def reconcile_item(self, asset, operation, item):
        return None


async def _seed(test_db, *, admin_required: bool = False):
    owner = TelegramAccount(
        identifier=f"owned-edge-owner-{admin_required}",
        session_name=f"owned-edge-owner-{admin_required}",
        account_type=AccountType.PROMOTER,
        is_active=True,
    )
    member = TelegramAccount(
        identifier=f"owned-edge-member-{admin_required}",
        session_name=f"owned-edge-member-{admin_required}",
        account_type=AccountType.PROMOTER,
        is_active=True,
    )
    test_db.add_all([owner, member])
    await test_db.flush()
    asset = OwnedGroupAsset(
        internal_name=f"owned-edge-{admin_required}",
        title="Owned Edge",
        visibility="private",
        owner_account_id=owner.id,
        invite_mode="direct_invite",
        status="ready",
        telegram_chat_id=-100321,
    )
    test_db.add(asset)
    await test_db.flush()
    operation = OwnedGroupOperation(
        group_asset_id=asset.id,
        operation_type="join",
        status="queued",
        selection_snapshot="[]",
        selection_snapshot_hash="1" * 64,
        config_snapshot='{"batch_size": 5, "max_attempts": 2}',
        config_snapshot_hash="2" * 64,
        idempotency_key=f"owned-edge-{admin_required}",
    )
    test_db.add(operation)
    await test_db.flush()
    item = OwnedGroupOperationItem(
        operation_id=operation.id,
        resource_type="user",
        resource_id=member.id,
        status="pending",
        admin_required=admin_required,
    )
    test_db.add(item)
    await test_db.flush()
    return operation, item


@pytest.mark.asyncio
async def test_invite_sent_does_not_complete_operation(test_db):
    operation, item = await _seed(test_db)
    result = await run_owned_group_operation(test_db, operation.id, adapter=InviteOnlyAdapter())
    assert item.status == "invite_sent"
    assert result["status"] in {"running", "unknown"}
    assert result["status"] != "completed"


@pytest.mark.asyncio
async def test_transient_failure_schedules_retry(test_db):
    operation, item = await _seed(test_db)
    result = await run_owned_group_operation(test_db, operation.id, adapter=TransientAdapter())
    assert item.status == "failed_transient"
    assert item.next_retry_at is not None
    assert result["status"] == "queued"

