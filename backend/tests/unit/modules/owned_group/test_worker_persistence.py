from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.account.models import AccountStatus, AccountType, TelegramAccount
from app.core.config import settings
from app.core.p0_safety_gate import SafetyGateDecision, SafetyGateState
from app.modules.owned_group.models import (
    OwnedGroupAsset,
    OwnedGroupOperation,
    OwnedGroupOperationItem,
)
from app.modules.owned_group.models_extra import (
    OwnedGroupAdminAssignment,
    OwnedGroupMembership,
)
from app.modules.owned_group.worker import (
    ItemExecutionResult,
    run_owned_group_operation,
    run_owned_group_worker_tick,
)


class ResultAdapter:
    def __init__(self, result: ItemExecutionResult):
        self.result = result
        self.calls = 0

    async def execute_item(self, asset, operation, item):
        self.calls += 1
        return self.result

    async def preflight(self, asset, owner):
        raise AssertionError("not used")

    async def create_group(self, asset, owner):
        raise AssertionError("not used")

    async def reconcile_item(self, asset, operation, item):
        return None


async def _seed(test_db, *, admin_required: bool = False):
    owner = TelegramAccount(
        identifier="owned-persist-owner",
        session_name="owned-persist-owner",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        session_string="owner-session",
        is_active=True,
    )
    member = TelegramAccount(
        identifier="owned-persist-member",
        session_name="owned-persist-member",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        session_string="member-session",
        is_active=True,
    )
    test_db.add_all([owner, member])
    await test_db.flush()
    asset = OwnedGroupAsset(
        internal_name="owned-persist-asset",
        title="Owned Persist Asset",
        visibility="private",
        owner_account_id=owner.id,
        invite_mode="direct_invite",
        status="ready",
        telegram_chat_id=-100456,
    )
    test_db.add(asset)
    await test_db.flush()
    operation = OwnedGroupOperation(
        group_asset_id=asset.id,
        operation_type="join",
        status="queued",
        selection_snapshot="[]",
        selection_snapshot_hash="a" * 64,
        config_snapshot=json.dumps({"batch_size": 5, "max_attempts": 2}),
        config_snapshot_hash="b" * 64,
        idempotency_key="owned-persist-operation",
    )
    test_db.add(operation)
    await test_db.flush()
    item = OwnedGroupOperationItem(
        operation_id=operation.id,
        resource_type="user",
        resource_id=member.id,
        status="pending",
        admin_required=admin_required,
        admin_permissions=json.dumps({"invite_users": True}) if admin_required else None,
        admin_title="Operator" if admin_required else None,
    )
    test_db.add(item)
    if admin_required:
        test_db.add(
            OwnedGroupAdminAssignment(
                group_asset_id=asset.id,
                resource_type="user",
                resource_id=member.id,
                permissions_snapshot=json.dumps({"invite_users": True}),
                admin_title="Operator",
                status="pending",
            )
        )
    await test_db.flush()
    return asset, operation, item, owner, member


@pytest.mark.asyncio
async def test_worker_persists_membership_admin_and_count(test_db):
    asset, operation, item, owner, member = await _seed(test_db, admin_required=True)
    adapter = ResultAdapter(
        ItemExecutionResult(
            status="admin_verified",
            success=True,
            reason_code="admin_verified",
            telegram_user_id=7002,
        )
    )

    result = await run_owned_group_operation(test_db, operation.id, adapter=adapter)

    assert result["status"] == "completed"
    assert adapter.calls == 1
    membership = await test_db.scalar(
        select(OwnedGroupMembership).where(
            OwnedGroupMembership.group_asset_id == asset.id,
            OwnedGroupMembership.resource_id == member.id,
        )
    )
    assert membership is not None
    assert membership.telegram_user_id == 7002
    assert membership.status == "admin_verified"
    assert membership.is_admin is True
    assert asset.member_count == 2  # owner + the verified member
    assignment = await test_db.scalar(
        select(OwnedGroupAdminAssignment).where(
            OwnedGroupAdminAssignment.group_asset_id == asset.id,
            OwnedGroupAdminAssignment.resource_id == member.id,
        )
    )
    assert assignment is not None
    assert assignment.status == "verified"
    assert assignment.verified_at is not None


@pytest.mark.asyncio
async def test_worker_sanitizes_adapter_error_and_honors_retry_delay(test_db):
    _asset, operation, item, _owner, _member = await _seed(test_db)
    adapter = ResultAdapter(
        ItemExecutionResult(
            status="failed_transient",
            transient=True,
            reason_code="flood_wait",
            retry_after_seconds=300,
            error_message="bot_token=123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcd invite=https://t.me/+PrivateHash123456",
        )
    )

    before = datetime.utcnow()
    result = await run_owned_group_operation(test_db, operation.id, adapter=adapter)

    assert result["status"] == "queued"
    assert item.status == "failed_transient"
    assert item.next_retry_at is not None
    assert item.next_retry_at >= before + timedelta(seconds=299)
    assert operation.schedule_at is not None
    # The configured/default batch interval is longer than this FloodWait, so
    # the operation itself must not be reclaimed before the next batch window.
    assert operation.schedule_at >= before + timedelta(seconds=599)
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZabcd" not in (item.error_message or "")
    assert "PrivateHash123456" not in (item.error_message or "")


@pytest.mark.asyncio
async def test_worker_honors_flood_wait_longer_than_one_day(test_db):
    _asset, operation, item, _owner, _member = await _seed(test_db)
    adapter = ResultAdapter(
        ItemExecutionResult(
            status="failed_transient",
            transient=True,
            reason_code="flood_wait",
            retry_after_seconds=172800,
        )
    )

    before = datetime.utcnow()
    await run_owned_group_operation(test_db, operation.id, adapter=adapter)

    assert item.next_retry_at is not None
    # The server-provided FloodWait is authoritative; it must not be truncated
    # to the old one-day application cap.
    assert item.next_retry_at >= before + timedelta(seconds=172799)
    assert operation.schedule_at is not None
    assert operation.schedule_at >= before + timedelta(seconds=172799)


@pytest.mark.asyncio
async def test_worker_tick_runs_only_one_batch_before_interval(test_db, monkeypatch):
    _asset, operation, _item, _owner, _member = await _seed(test_db)
    operation.config_snapshot = json.dumps(
        {"batch_size": 1, "batch_interval_seconds": 600, "max_attempts": 2}
    )
    second_member = TelegramAccount(
        identifier="owned-persist-member-two",
        session_name="owned-persist-member-two",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        session_string="member-two-session",
        is_active=True,
    )
    test_db.add(second_member)
    await test_db.flush()
    test_db.add(
        OwnedGroupOperationItem(
            operation_id=operation.id,
            resource_type="user",
            resource_id=second_member.id,
            status="pending",
            admin_required=False,
        )
    )
    await test_db.flush()

    async def open_gate():
        return SafetyGateState(global_stop=False, backend_available=True)

    monkeypatch.setattr("app.modules.owned_group.worker.get_safety_gate_state", open_gate)
    monkeypatch.setattr(
        "app.modules.owned_group.worker.is_owned_group_module_enabled", lambda: True
    )
    adapter = ResultAdapter(ItemExecutionResult(success=True))
    before = datetime.utcnow()

    result = await run_owned_group_worker_tick(
        test_db,
        limit=10,
        adapter=adapter,
    )

    assert result["status"] == "ok"
    assert len(result["operations"]) == 1
    assert adapter.calls == 1
    assert operation.status == "queued"
    assert operation.schedule_at is not None
    assert operation.schedule_at >= before + timedelta(seconds=599)
    pending_items = (
        await test_db.scalars(
            select(OwnedGroupOperationItem).where(
                OwnedGroupOperationItem.operation_id == operation.id,
                OwnedGroupOperationItem.status == "pending",
            )
        )
    ).all()
    assert len(pending_items) == 1


@pytest.mark.asyncio
async def test_strict_worker_precheck_blocks_adapter_before_call(test_db, monkeypatch):
    _asset, operation, item, _owner, _member = await _seed(test_db)
    adapter = ResultAdapter(ItemExecutionResult(success=True))

    async def deny(*_args, **_kwargs):
        return SafetyGateDecision(False, "resource_eligibility_failed")

    monkeypatch.setattr("app.modules.owned_group.worker.precheck_owned_group_resources", deny)
    monkeypatch.setattr(settings, "OWNED_GROUP_EXECUTION_ENABLED", True)

    result = await run_owned_group_operation(
        test_db,
        operation.id,
        adapter=adapter,
        strict_precheck=True,
    )

    assert result["status"] == "queued"
    assert item.status == "failed_transient"
    assert item.reason_code == "resource_eligibility_failed"
    assert adapter.calls == 0
