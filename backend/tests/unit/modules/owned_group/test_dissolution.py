from __future__ import annotations

import importlib
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.account.models import AccountStatus, AccountType, TelegramAccount
from app.core.p0_safety_gate import SafetyGateDecision
from app.core.security import get_current_user
from app.main import app
from app.modules.owned_group.models import OwnedGroupAsset, OwnedGroupOperation
from app.modules.owned_group.models_extra import OwnedGroupAuditEvent
from app.modules.owned_group.telegram_adapter import TelethonOwnedGroupTelegramAdapter
from app.modules.owned_group.worker import (
    GroupDissolveResult,
    reconcile_owned_group_operation,
    reconcile_stale_owned_group_operations,
    retry_owned_group_operation,
    run_owned_group_operation,
)


@pytest.fixture(autouse=True)
def override_admin_user():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 9201,
        "username": "dissolution-admin",
        "role": "admin",
    }
    yield
    app.dependency_overrides.pop(get_current_user, None)


async def _seed_asset(
    test_db,
    *,
    asset_status: str = "ready",
    operation_status: str | None = None,
    operation_type: str = "dissolve",
):
    suffix = f"{asset_status}-{operation_status or 'none'}-{operation_type}"
    owner = TelegramAccount(
        identifier=f"dissolution-owner-{suffix}",
        session_name=f"dissolution-owner-{suffix}",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    test_db.add(owner)
    await test_db.flush()
    asset = OwnedGroupAsset(
        internal_name=f"dissolution-{suffix}",
        title="Dissolution Test Group",
        visibility="private",
        owner_account_id=owner.id,
        invite_mode="direct_invite",
        status=asset_status,
        telegram_chat_id=-(1000000000000 + owner.id),
        governance_status="disabled",
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
            selection_snapshot_hash="a" * 64,
            config_snapshot=json.dumps({"operation": operation_type, "max_attempts": 1}),
            config_snapshot_hash="b" * 64,
            idempotency_key=f"dissolution-{suffix}-{owner.id}",
            planned_count=1 if operation_type == "dissolve" else 0,
        )
        test_db.add(operation)
        await test_db.flush()
    await test_db.commit()
    return asset.id, operation.id if operation is not None else None


@pytest.mark.asyncio
async def test_queue_dissolution_requires_exact_phrase_and_is_idempotent(client, test_db, monkeypatch):
    monkeypatch.setattr(importlib.import_module("app.api.owned_groups"), "require_owned_group_module_enabled", lambda: None)
    asset_id, _ = await _seed_asset(test_db)
    idempotency_key = "dissolution-request-0001"

    wrong = await client.post(
        f"/api/owned-groups/{asset_id}/dissolution",
        headers={"Idempotency-Key": idempotency_key},
        json={"confirmation": f"INVALID ACTION {asset_id}"},
    )
    assert wrong.status_code == 409
    assert wrong.json()["detail"]["reason"] == "dissolution_confirmation_invalid"

    queued = await client.post(
        f"/api/owned-groups/{asset_id}/dissolution",
        headers={"Idempotency-Key": idempotency_key},
        json={"confirmation": f"DISSOLVE {asset_id}"},
    )
    assert queued.status_code == 202
    payload = queued.json()
    assert payload["operation_type"] == "dissolve"
    assert payload["status"] == "queued"
    assert payload["planned_count"] == 1

    test_db.expire_all()
    asset = await test_db.get(OwnedGroupAsset, asset_id)
    operation = await test_db.get(OwnedGroupOperation, payload["id"])
    assert asset is not None and asset.status == "dissolving"
    assert operation is not None and operation.operation_type == "dissolve"
    assert operation.status == "queued"
    audit = await test_db.scalar(
        select(OwnedGroupAuditEvent).where(
            OwnedGroupAuditEvent.operation_id == operation.id,
            OwnedGroupAuditEvent.event_type == "owned_group_dissolution_queued",
        )
    )
    assert audit is not None

    replay = await client.post(
        f"/api/owned-groups/{asset_id}/dissolution",
        headers={"Idempotency-Key": idempotency_key},
        json={"confirmation": f"DISSOLVE {asset_id}"},
    )
    assert replay.status_code == 202
    assert replay.json()["id"] == operation.id


@pytest.mark.asyncio
async def test_queue_dissolution_rejects_active_member_operation(client, test_db, monkeypatch):
    monkeypatch.setattr(importlib.import_module("app.api.owned_groups"), "require_owned_group_module_enabled", lambda: None)
    asset_id, _ = await _seed_asset(
        test_db,
        operation_status="queued",
        operation_type="join",
    )

    response = await client.post(
        f"/api/owned-groups/{asset_id}/dissolution",
        headers={"Idempotency-Key": "dissolution-request-0002"},
        json={"confirmation": f"DISSOLVE {asset_id}"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "owned_group_operation_active"
    test_db.expire_all()
    asset = await test_db.get(OwnedGroupAsset, asset_id)
    assert asset is not None and asset.status == "ready"


class DissolveAdapter:
    def __init__(self, result: GroupDissolveResult):
        self.result = result
        self.calls = 0

    async def dissolve_group(self, _asset):
        self.calls += 1
        return self.result


@pytest.mark.asyncio
async def test_dissolution_worker_archives_only_after_explicit_adapter_success(test_db):
    asset_id, operation_id = await _seed_asset(
        test_db,
        asset_status="dissolving",
        operation_status="queued",
    )
    assert operation_id is not None
    adapter = DissolveAdapter(GroupDissolveResult(success=True))

    result = await run_owned_group_operation(test_db, operation_id, adapter=adapter)

    assert result["status"] == "completed"
    assert adapter.calls == 1
    asset = await test_db.get(OwnedGroupAsset, asset_id)
    operation = await test_db.get(OwnedGroupOperation, operation_id)
    assert asset is not None and asset.status == "archived"
    assert asset.archived_at is not None
    assert operation is not None and operation.completed_count == 1
    assert operation.failed_count == 0


@pytest.mark.asyncio
async def test_uncertain_dissolution_requires_manual_review_and_cannot_replay(test_db):
    asset_id, operation_id = await _seed_asset(
        test_db,
        asset_status="dissolving",
        operation_status="queued",
    )
    assert operation_id is not None
    adapter = DissolveAdapter(
        GroupDissolveResult(
            success=False,
            unknown=True,
            reason_code="unknown_needs_reconcile",
        )
    )

    result = await run_owned_group_operation(test_db, operation_id, adapter=adapter)
    assert result["status"] == "unknown"
    assert adapter.calls == 1
    asset = await test_db.get(OwnedGroupAsset, asset_id)
    assert asset is not None and asset.status == "needs_attention"

    repeated = await run_owned_group_operation(test_db, operation_id, adapter=adapter)
    assert repeated["status"] == "unknown"
    assert adapter.calls == 1
    with pytest.raises(ValueError, match="cannot be retried"):
        await retry_owned_group_operation(test_db, operation_id)
    with pytest.raises(ValueError, match="manual Telegram verification"):
        await reconcile_owned_group_operation(test_db, operation_id, adapter=adapter)


@pytest.mark.asyncio
async def test_stale_running_dissolution_is_quarantined_without_retry(test_db):
    asset_id, operation_id = await _seed_asset(
        test_db,
        asset_status="dissolving",
        operation_status="running",
    )
    assert operation_id is not None
    operation = await test_db.get(OwnedGroupOperation, operation_id)
    assert operation is not None
    operation.updated_at = datetime.utcnow() - timedelta(hours=1)
    await test_db.commit()

    result = await reconcile_stale_owned_group_operations(test_db, stale_after_seconds=60)

    assert result == {"operations": 1, "items": 0}
    test_db.expire_all()
    asset = await test_db.get(OwnedGroupAsset, asset_id)
    operation = await test_db.get(OwnedGroupOperation, operation_id)
    assert asset is not None and asset.status == "needs_attention"
    assert operation is not None and operation.status == "unknown"
    assert operation.last_error == "dissolution_worker_heartbeat_stale"


class AdapterTestPool:
    def __init__(self, wrapper):
        self.wrapper = wrapper
        self.acquired: list[int] = []
        self.released: list[int] = []

    async def add_account_from_db(self, _account):
        return self.wrapper

    async def acquire_by_id(self, account_id, **_kwargs):
        self.acquired.append(int(account_id))
        return self.wrapper

    async def release(self, wrapper):
        self.released.append(int(wrapper.account_id))


class AdapterTestDb:
    def __init__(self, owner):
        self.owner = owner

    async def get(self, model, key):
        if model is TelegramAccount and int(key) == int(self.owner.id):
            return self.owner
        return None


class AdapterTestClient:
    def __init__(self):
        self.requests: list[object] = []
        self.entity = SimpleNamespace(id=12345, megagroup=True, broadcast=False)
        participant_type = type("ChannelParticipantCreator", (), {})
        self.participant = participant_type()
        self.participant.user_id = 2001

    async def get_entity(self, _value):
        return self.entity

    async def get_me(self):
        return SimpleNamespace(id=2001)

    async def __call__(self, request):
        self.requests.append(request)
        if request.__class__.__name__ == "GetParticipantRequest":
            return SimpleNamespace(participant=self.participant)
        return True


@pytest.mark.asyncio
async def test_telegram_adapter_deletes_only_creator_owned_supergroup(monkeypatch):
    monkeypatch.setattr("app.modules.owned_group.telegram_adapter.is_safety_gate_enabled", lambda: True)
    owner = TelegramAccount(
        id=1,
        identifier="dissolution-adapter-owner",
        session_name="dissolution-adapter-owner",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
        session_string="test-session",
    )
    client = AdapterTestClient()
    wrapper = SimpleNamespace(account_id=owner.id, client=client)
    pool = AdapterTestPool(wrapper)

    async def allowed_precheck(*_args, **_kwargs):
        return SafetyGateDecision(True, "eligible")

    adapter = TelethonOwnedGroupTelegramAdapter(
        AdapterTestDb(owner),
        pool,
        execution_enabled_reader=lambda: True,
        safety_state_reader=lambda: SimpleNamespace(global_stop=False, backend_available=True),
        safety_precheck=allowed_precheck,
        execution_service=SimpleNamespace(),
        require_redis_safety=False,
    )

    result = await adapter.dissolve_group(
        OwnedGroupAsset(
            id=10,
            internal_name="dissolution-adapter-asset",
            title="Dissolution Adapter Group",
            visibility="private",
            owner_account_id=owner.id,
            invite_mode="direct_invite",
            telegram_chat_id=12345,
        )
    )

    assert result.success is True
    assert result.reason_code == "telegram_group_deleted"
    assert pool.acquired == [owner.id]
    assert pool.released == [owner.id]
    deletion = next(
        request
        for request in client.requests
        if request.__class__.__name__ == "DeleteChannelRequest"
    )
    assert deletion.channel is client.entity
