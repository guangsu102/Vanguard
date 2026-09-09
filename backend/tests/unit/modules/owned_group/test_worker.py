from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.account.models import AccountType, TelegramAccount
from app.modules.owned_group.models import (
    OwnedGroupAsset,
    OwnedGroupOperation,
    OwnedGroupOperationItem,
)
from app.modules.owned_group.models_extra import OwnedGroupAuditEvent
from app.modules.owned_group.worker import (
    GroupCreateResult,
    ItemExecutionResult,
    PreflightResult,
    enqueue_asset_precheck,
    execute_asset_precheck,
    reconcile_owned_group_asset,
    reconcile_stale_owned_group_operations,
    run_owned_group_operation,
)


class ReadyAdapter:
    async def preflight(self, asset, owner):
        return PreflightResult(ready=True)

    async def create_group(self, asset, owner):
        return GroupCreateResult(
            success=True,
            telegram_chat_id=-100123,
            telegram_username="owned_worker_test",
        )

    async def execute_item(self, asset, operation, item):
        return ItemExecutionResult(success=True)

    async def reconcile_item(self, asset, operation, item):
        return None


class ExistingGroupAdapter(ReadyAdapter):
    def __init__(self, *, success: bool = True):
        self.success = success
        self.seen_chat_id = None

    async def create_group(self, asset, owner):
        # The recovery boundary must assign the candidate before invoking the
        # adapter.  The concrete adapter therefore takes its existing-group
        # branch and cannot issue CreateChannelRequest.
        self.seen_chat_id = asset.telegram_chat_id
        return GroupCreateResult(
            success=self.success,
            telegram_chat_id=asset.telegram_chat_id,
            reason_code="already_created" if self.success else "owner_not_group_admin",
        )


class PublicUsernameRecoveryAdapter(ReadyAdapter):
    def __init__(self, *, success: bool = True):
        self.success = success
        self.seen_username = None

    async def create_group(self, asset, owner):
        self.seen_username = asset.telegram_username
        return GroupCreateResult(
            success=self.success,
            telegram_chat_id=asset.telegram_chat_id,
            telegram_username=asset.telegram_username if self.success else None,
            public_link=(f"https://t.me/{asset.telegram_username}" if self.success else None),
            reason_code="already_created" if self.success else "username_unavailable",
        )


@pytest.mark.asyncio
async def test_precheck_never_marks_ready_without_adapter(test_db):
    owner = TelegramAccount(
        identifier="owned-worker-noop-owner",
        session_name="owned-worker-noop-owner",
        account_type=AccountType.PROMOTER,
        is_active=True,
    )
    test_db.add(owner)
    await test_db.flush()
    asset = OwnedGroupAsset(
        internal_name="owned-worker-noop",
        title="Owned Worker Noop",
        visibility="private",
        owner_account_id=owner.id,
        invite_mode="direct_invite",
        status="draft",
    )
    test_db.add(asset)
    await test_db.flush()

    await enqueue_asset_precheck(test_db, asset.id)
    result = await execute_asset_precheck(test_db, asset.id)

    assert result["status"] == "needs_attention"
    assert asset.status == "needs_attention"
    assert asset.telegram_chat_id is None


@pytest.mark.asyncio
async def test_precheck_ready_adapter_persists_chat_and_link(test_db):
    owner = TelegramAccount(
        identifier="owned-worker-ready-owner",
        session_name="owned-worker-ready-owner",
        account_type=AccountType.PROMOTER,
        is_active=True,
    )
    test_db.add(owner)
    await test_db.flush()
    asset = OwnedGroupAsset(
        internal_name="owned-worker-ready",
        title="Owned Worker Ready",
        visibility="public",
        telegram_username="owned_worker_ready",
        owner_account_id=owner.id,
        invite_mode="direct_invite",
        status="draft",
    )
    test_db.add(asset)
    await test_db.flush()

    result = await execute_asset_precheck(test_db, asset.id, adapter=ReadyAdapter())

    assert result["status"] == "ready"
    assert asset.status == "ready"
    assert asset.telegram_chat_id == -100123
    assert asset.public_link == "https://t.me/owned_worker_test"


@pytest.mark.asyncio
async def test_asset_reconcile_binds_candidate_before_adapter_and_marks_ready(test_db):
    owner = TelegramAccount(
        identifier="owned-worker-reconcile-owner",
        session_name="owned-worker-reconcile-owner",
        account_type=AccountType.PROMOTER,
        is_active=True,
    )
    test_db.add(owner)
    await test_db.flush()
    asset = OwnedGroupAsset(
        internal_name="owned-worker-reconcile",
        title="Owned Worker Reconcile",
        visibility="private",
        owner_account_id=owner.id,
        invite_mode="direct_invite",
        status="needs_attention",
    )
    test_db.add(asset)
    await test_db.flush()
    adapter = ExistingGroupAdapter()

    result = await reconcile_owned_group_asset(
        test_db,
        asset.id,
        telegram_chat_id=-100987654321,
        adapter=adapter,
        actor_id=77,
    )

    assert result["status"] == "ready"
    assert adapter.seen_chat_id == -100987654321
    assert asset.telegram_chat_id == -100987654321
    assert asset.status == "ready"
    audit = await test_db.scalar(
        select(OwnedGroupAuditEvent).where(
            OwnedGroupAuditEvent.group_asset_id == asset.id,
            OwnedGroupAuditEvent.event_type == "owned_group_asset_reconciled",
        )
    )
    assert audit is not None
    assert audit.actor_id == 77


@pytest.mark.asyncio
async def test_failed_manual_asset_reconcile_clears_candidate_and_still_blocks_create_retry(
    test_db,
):
    owner = TelegramAccount(
        identifier="owned-worker-reconcile-failed-owner",
        session_name="owned-worker-reconcile-failed-owner",
        account_type=AccountType.PROMOTER,
        is_active=True,
    )
    test_db.add(owner)
    await test_db.flush()
    asset = OwnedGroupAsset(
        internal_name="owned-worker-reconcile-failed",
        title="Owned Worker Reconcile Failed",
        visibility="private",
        owner_account_id=owner.id,
        invite_mode="direct_invite",
        status="needs_attention",
    )
    test_db.add(asset)
    await test_db.flush()

    result = await reconcile_owned_group_asset(
        test_db,
        asset.id,
        telegram_chat_id=-100111222333,
        adapter=ExistingGroupAdapter(success=False),
    )

    assert result["status"] == "needs_attention"
    assert result["reason_code"] == "owner_not_group_admin"
    assert asset.telegram_chat_id is None
    with pytest.raises(ValueError, match="explicit Telegram reconciliation"):
        await enqueue_asset_precheck(test_db, asset.id)


@pytest.mark.asyncio
async def test_public_asset_reconcile_can_replace_failed_username(test_db):
    owner = TelegramAccount(
        identifier="owned-worker-public-reconcile-owner",
        session_name="owned-worker-public-reconcile-owner",
        account_type=AccountType.PROMOTER,
        is_active=True,
    )
    test_db.add(owner)
    await test_db.flush()
    asset = OwnedGroupAsset(
        internal_name="owned-worker-public-reconcile",
        title="Owned Worker Public Reconcile",
        visibility="public",
        telegram_username="occupied_name",
        public_link=None,
        owner_account_id=owner.id,
        invite_mode="direct_invite",
        status="needs_attention",
        telegram_chat_id=-100444555666,
    )
    test_db.add(asset)
    await test_db.flush()
    adapter = PublicUsernameRecoveryAdapter()

    result = await reconcile_owned_group_asset(
        test_db,
        asset.id,
        telegram_username="@available_name",
        adapter=adapter,
    )

    assert result["status"] == "ready"
    assert adapter.seen_username == "available_name"
    assert asset.telegram_username == "available_name"
    assert asset.public_link == "https://t.me/available_name"


@pytest.mark.asyncio
async def test_failed_public_username_reconcile_restores_previous_metadata(test_db):
    owner = TelegramAccount(
        identifier="owned-worker-public-reconcile-fail-owner",
        session_name="owned-worker-public-reconcile-fail-owner",
        account_type=AccountType.PROMOTER,
        is_active=True,
    )
    test_db.add(owner)
    await test_db.flush()
    asset = OwnedGroupAsset(
        internal_name="owned-worker-public-reconcile-fail",
        title="Owned Worker Public Reconcile Fail",
        visibility="public",
        telegram_username="original_name",
        public_link="https://t.me/original_name",
        owner_account_id=owner.id,
        invite_mode="direct_invite",
        status="needs_attention",
        telegram_chat_id=-100111333555,
    )
    test_db.add(asset)
    await test_db.flush()
    adapter = PublicUsernameRecoveryAdapter(success=False)

    result = await reconcile_owned_group_asset(
        test_db,
        asset.id,
        telegram_username="replacement_name",
        adapter=adapter,
    )

    assert result["status"] == "needs_attention"
    assert result["reason_code"] == "username_unavailable"
    assert adapter.seen_username == "replacement_name"
    assert asset.telegram_username == "original_name"
    assert asset.public_link == "https://t.me/original_name"


@pytest.mark.asyncio
async def test_operation_worker_completes_items_and_is_idempotent(test_db):
    owner = TelegramAccount(
        identifier="owned-worker-operation-owner",
        session_name="owned-worker-operation-owner",
        account_type=AccountType.PROMOTER,
        is_active=True,
    )
    member = TelegramAccount(
        identifier="owned-worker-operation-member",
        session_name="owned-worker-operation-member",
        account_type=AccountType.PROMOTER,
        is_active=True,
    )
    test_db.add_all([owner, member])
    await test_db.flush()
    asset = OwnedGroupAsset(
        internal_name="owned-worker-operation",
        title="Owned Worker Operation",
        visibility="private",
        owner_account_id=owner.id,
        invite_mode="direct_invite",
        status="ready",
        telegram_chat_id=-100987,
    )
    test_db.add(asset)
    await test_db.flush()
    operation = OwnedGroupOperation(
        group_asset_id=asset.id,
        operation_type="join",
        status="queued",
        selection_snapshot="[]",
        selection_snapshot_hash="a" * 64,
        config_snapshot='{"batch_size": 5, "max_attempts": 2}',
        config_snapshot_hash="b" * 64,
        idempotency_key="owned-worker-operation-1",
        planned_count=1,
    )
    test_db.add(operation)
    await test_db.flush()
    item = OwnedGroupOperationItem(
        operation_id=operation.id,
        resource_type="user",
        resource_id=member.id,
        status="pending",
    )
    test_db.add(item)
    await test_db.flush()

    result = await run_owned_group_operation(test_db, operation.id, adapter=ReadyAdapter())
    assert result["status"] == "completed"
    assert item.status == "member_verified"
    assert item.attempts == 1

    second = await run_owned_group_operation(test_db, operation.id, adapter=ReadyAdapter())
    assert second["status"] == "completed"
    assert item.attempts == 1


@pytest.mark.asyncio
async def test_stale_running_operation_becomes_unknown(test_db):
    asset = OwnedGroupAsset(
        internal_name="owned-worker-stale",
        title="Owned Worker Stale",
        visibility="private",
        owner_account_id=1,
        invite_mode="direct_invite",
        status="ready",
    )
    test_db.add(asset)
    await test_db.flush()
    operation = OwnedGroupOperation(
        group_asset_id=asset.id,
        operation_type="join",
        status="running",
        selection_snapshot="[]",
        selection_snapshot_hash="c" * 64,
        config_snapshot="{}",
        config_snapshot_hash="d" * 64,
        idempotency_key="owned-worker-stale-1",
        updated_at=datetime.utcnow() - timedelta(hours=1),
    )
    test_db.add(operation)
    await test_db.flush()
    item = OwnedGroupOperationItem(
        operation_id=operation.id,
        resource_type="user",
        resource_id=2,
        status="in_progress",
    )
    test_db.add(item)
    await test_db.flush()

    result = await reconcile_stale_owned_group_operations(test_db, stale_after_seconds=60)
    assert result == {"operations": 1, "items": 1}
    assert operation.status == "unknown"
    assert item.status == "unknown"


@pytest.mark.asyncio
async def test_stale_paused_operation_with_inflight_item_becomes_unknown(test_db):
    asset = OwnedGroupAsset(
        internal_name="owned-worker-stale-paused",
        title="Owned Worker Stale Paused",
        visibility="private",
        owner_account_id=1,
        invite_mode="direct_invite",
        status="ready",
    )
    test_db.add(asset)
    await test_db.flush()
    operation = OwnedGroupOperation(
        group_asset_id=asset.id,
        operation_type="join",
        status="paused",
        selection_snapshot="[]",
        selection_snapshot_hash="e" * 64,
        config_snapshot="{}",
        config_snapshot_hash="f" * 64,
        idempotency_key="owned-worker-stale-paused-1",
        updated_at=datetime.utcnow() - timedelta(hours=1),
    )
    test_db.add(operation)
    await test_db.flush()
    item = OwnedGroupOperationItem(
        operation_id=operation.id,
        resource_type="user",
        resource_id=2,
        status="invite_sent",
    )
    test_db.add(item)
    await test_db.flush()

    result = await reconcile_stale_owned_group_operations(test_db, stale_after_seconds=60)

    assert result == {"operations": 1, "items": 1}
    assert operation.status == "unknown"
    assert item.status == "unknown"


@pytest.mark.asyncio
async def test_stale_stopping_without_unresolved_items_finalizes(test_db):
    asset = OwnedGroupAsset(
        internal_name="owned-worker-stale-stopping",
        title="Owned Worker Stale Stopping",
        visibility="private",
        owner_account_id=1,
        invite_mode="direct_invite",
        status="ready",
    )
    test_db.add(asset)
    await test_db.flush()
    operation = OwnedGroupOperation(
        group_asset_id=asset.id,
        operation_type="join",
        status="stopping",
        selection_snapshot="[]",
        selection_snapshot_hash="1" * 64,
        config_snapshot="{}",
        config_snapshot_hash="2" * 64,
        idempotency_key="owned-worker-stale-stopping-1",
        updated_at=datetime.utcnow() - timedelta(hours=1),
    )
    test_db.add(operation)
    await test_db.flush()
    test_db.add(
        OwnedGroupOperationItem(
            operation_id=operation.id,
            resource_type="user",
            resource_id=2,
            status="member_verified",
        )
    )
    await test_db.flush()

    result = await reconcile_stale_owned_group_operations(test_db, stale_after_seconds=60)

    assert result == {"operations": 1, "items": 0}
    assert operation.status == "stopped"
