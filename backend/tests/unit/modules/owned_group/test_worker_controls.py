from __future__ import annotations

import pytest

from app.core.account.models import AccountType, TelegramAccount
from app.modules.owned_group.models import (
    OwnedGroupAsset,
    OwnedGroupOperation,
    OwnedGroupOperationItem,
)
from app.modules.owned_group.worker import (
    pause_owned_group_operation,
    resume_owned_group_operation,
    retry_owned_group_operation,
    stop_owned_group_operation,
)


async def _seed_operation(test_db, *, status: str = "queued", item_status: str = "pending"):
    owner = TelegramAccount(
        identifier=f"owned-control-owner-{status}-{item_status}",
        session_name=f"owned-control-owner-{status}-{item_status}",
        account_type=AccountType.PROMOTER,
        is_active=True,
    )
    test_db.add(owner)
    await test_db.flush()
    asset = OwnedGroupAsset(
        internal_name=f"owned-control-{status}-{item_status}",
        title="Owned Control",
        visibility="private",
        owner_account_id=owner.id,
        invite_mode="direct_invite",
        status="ready",
    )
    test_db.add(asset)
    await test_db.flush()
    operation = OwnedGroupOperation(
        group_asset_id=asset.id,
        operation_type="join",
        status=status,
        selection_snapshot="[]",
        selection_snapshot_hash="e" * 64,
        config_snapshot="{}",
        config_snapshot_hash="f" * 64,
        idempotency_key=f"owned-control-{status}-{item_status}",
        planned_count=1,
    )
    test_db.add(operation)
    await test_db.flush()
    item = OwnedGroupOperationItem(
        operation_id=operation.id,
        resource_type="user",
        resource_id=owner.id,
        status=item_status,
    )
    test_db.add(item)
    await test_db.flush()
    return operation, item


@pytest.mark.asyncio
async def test_pause_resume_and_stop_controls(test_db):
    operation, _ = await _seed_operation(test_db)

    await pause_owned_group_operation(test_db, operation.id, actor_id=7)
    assert operation.status == "paused"
    await resume_owned_group_operation(test_db, operation.id, actor_id=7)
    assert operation.status == "queued"
    await stop_owned_group_operation(test_db, operation.id, actor_id=7)
    assert operation.status == "stopped"


@pytest.mark.asyncio
async def test_retry_resets_failed_item_and_queues_operation(test_db):
    operation, item = await _seed_operation(
        test_db, status="failed", item_status="failed_permanent"
    )

    await retry_owned_group_operation(test_db, operation.id, actor_id=7)

    assert operation.status == "queued"
    assert item.status == "pending"
    assert item.reason_code is None
    assert item.error_message is None


@pytest.mark.asyncio
async def test_resume_refuses_unknown_item_until_reconciled(test_db):
    operation, item = await _seed_operation(
        test_db, status="paused", item_status="unknown"
    )

    with pytest.raises(ValueError, match="Reconcile all in-flight or UNKNOWN"):
        await resume_owned_group_operation(test_db, operation.id, actor_id=7)

    assert operation.status == "paused"
    assert item.status == "unknown"


@pytest.mark.asyncio
async def test_stop_paused_inflight_operation_waits_for_reconcile(test_db):
    operation, item = await _seed_operation(
        test_db, status="paused", item_status="invite_sent"
    )

    await stop_owned_group_operation(test_db, operation.id, actor_id=7)

    assert operation.status == "stopping"
    assert item.status == "invite_sent"
