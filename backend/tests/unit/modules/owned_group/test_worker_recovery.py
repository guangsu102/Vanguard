from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.core.account.models import AccountType, TelegramAccount
from app.modules.owned_group.models import (
    OwnedGroupAsset,
    OwnedGroupOperation,
    OwnedGroupOperationItem,
)
from app.modules.owned_group.worker import (
    GroupCreateResult,
    ItemExecutionResult,
    PreflightResult,
    reconcile_owned_group_operation,
    reconcile_stale_owned_group_assets,
    run_owned_group_operation,
)


class CountingAdapter:
    def __init__(self, result: ItemExecutionResult | None = None):
        self.result = result or ItemExecutionResult(success=True)
        self.calls = 0

    async def preflight(self, asset, owner):
        return PreflightResult(ready=True)

    async def create_group(self, asset, owner):
        return GroupCreateResult(success=True, telegram_chat_id=-100777)

    async def execute_item(self, asset, operation, item):
        self.calls += 1
        return self.result

    async def reconcile_item(self, asset, operation, item):
        return ItemExecutionResult(success=True)


async def _seed_operation(test_db, *, operation_status: str, item_status: str):
    owner = TelegramAccount(
        identifier=f"owned-recovery-owner-{operation_status}-{item_status}",
        session_name=f"owned-recovery-owner-{operation_status}-{item_status}",
        account_type=AccountType.PROMOTER,
        is_active=True,
    )
    test_db.add(owner)
    await test_db.flush()
    asset = OwnedGroupAsset(
        internal_name=f"owned-recovery-{operation_status}-{item_status}",
        title="Owned Recovery",
        visibility="private",
        owner_account_id=owner.id,
        invite_mode="direct_invite",
        status="ready",
        telegram_chat_id=-100778,
    )
    test_db.add(asset)
    await test_db.flush()
    operation = OwnedGroupOperation(
        group_asset_id=asset.id,
        operation_type="join",
        status=operation_status,
        selection_snapshot="[]",
        selection_snapshot_hash="a" * 64,
        config_snapshot='{"batch_size": 5, "max_attempts": 2}',
        config_snapshot_hash="b" * 64,
        idempotency_key=f"owned-recovery-{operation_status}-{item_status}",
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
    return asset, operation, item


@pytest.mark.asyncio
async def test_unknown_operation_requires_reconcile_before_replay(test_db):
    _, operation, _ = await _seed_operation(
        test_db, operation_status="unknown", item_status="pending"
    )
    adapter = CountingAdapter()
    result = await run_owned_group_operation(test_db, operation.id, adapter=adapter)
    assert result["status"] == "unknown"
    assert adapter.calls == 0


@pytest.mark.asyncio
async def test_stopping_inflight_operation_finalizes_only_after_reconcile(test_db):
    asset, operation, item = await _seed_operation(
        test_db, operation_status="stopping", item_status="invite_sent"
    )
    result = await run_owned_group_operation(test_db, operation.id, adapter=CountingAdapter())
    assert result["status"] == "stopping"
    assert item.status == "invite_sent"

    result = await reconcile_owned_group_operation(test_db, operation.id, adapter=CountingAdapter())
    assert result["status"] == "stopped"
    assert operation.status == "stopped"
    assert item.status == "member_verified"


@pytest.mark.asyncio
async def test_batch_with_remaining_pending_items_is_requeued(test_db):
    _, operation, item = await _seed_operation(
        test_db, operation_status="queued", item_status="pending"
    )
    second = OwnedGroupOperationItem(
        operation_id=operation.id,
        resource_type="user",
        resource_id=item.resource_id + 1,
        status="pending",
    )
    test_db.add(second)
    await test_db.flush()
    adapter = CountingAdapter()

    result = await run_owned_group_operation(test_db, operation.id, adapter=adapter, max_items=1)

    assert result["status"] == "queued"
    assert result["pending_count"] == 1
    assert adapter.calls == 1

    result = await run_owned_group_operation(test_db, operation.id, adapter=adapter, max_items=1)
    assert result["status"] == "completed"
    assert adapter.calls == 2


@pytest.mark.asyncio
async def test_stale_creating_asset_is_quarantined(test_db):
    owner = TelegramAccount(
        identifier="owned-recovery-stale-owner",
        session_name="owned-recovery-stale-owner",
        account_type=AccountType.PROMOTER,
        is_active=True,
    )
    test_db.add(owner)
    await test_db.flush()
    asset = OwnedGroupAsset(
        internal_name="owned-recovery-stale",
        title="Owned Recovery Stale",
        visibility="private",
        owner_account_id=owner.id,
        invite_mode="direct_invite",
        status="creating",
        updated_at=datetime.utcnow() - timedelta(hours=1),
    )
    test_db.add(asset)
    await test_db.flush()

    result = await reconcile_stale_owned_group_assets(test_db, stale_after_seconds=60)

    assert result == {"assets": 1}
    assert asset.status == "needs_attention"
