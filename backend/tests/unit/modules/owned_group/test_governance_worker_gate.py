from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import app.workers.telegram_worker as telegram_worker_module
from app.core.account.models import (
    AccountStatus,
    AccountType,
    GuardianBotProfile,
    TelegramAccount,
)
from app.core.group.models import Group
from app.core.worker_status import TelegramWorkerRole
from app.modules.guardian.models import (
    ManagedGroupBinding,
    ManagedGroupBindingStatus,
    ManagedGroupBotRole,
)
from app.modules.guardian.sync import (
    sync_managed_group_binding as guarded_sync_managed_group_binding,
)
from app.modules.owned_group.governance_worker import (
    evaluate_guardian_member,
    resolve_governance_worker_target,
)
from app.modules.owned_group.models import OwnedGroupAsset
from app.modules.owned_group.models_extra import OwnedBotProfile, OwnedGroupAuditEvent
from app.workers.telegram_worker import TelegramWorker


async def _seed_accounts(test_db, suffix: str):
    owner = TelegramAccount(
        identifier=f"worker-owner-{suffix}",
        session_name=f"worker-owner-{suffix}",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    bot_a = TelegramAccount(
        identifier=f"worker-bot-a-{suffix}",
        session_name=f"worker-bot-a-{suffix}",
        account_type=AccountType.GUARDIAN_BOT,
        status=AccountStatus.IDLE,
        is_active=True,
    )
    bot_b = TelegramAccount(
        identifier=f"worker-bot-b-{suffix}",
        session_name=f"worker-bot-b-{suffix}",
        account_type=AccountType.GUARDIAN_BOT,
        status=AccountStatus.IDLE,
        is_active=True,
    )
    test_db.add_all([owner, bot_a, bot_b])
    await test_db.flush()
    return owner, bot_a, bot_b


async def _seed_managed_target(
    test_db,
    *,
    chat_id: int,
    owned: bool = True,
    binding_status: ManagedGroupBindingStatus = ManagedGroupBindingStatus.ACTIVE,
):
    owner, bot_a, bot_b = await _seed_accounts(test_db, str(abs(chat_id)))
    test_db.add_all(
        [
            GuardianBotProfile(
                account_id=bot_a.id,
                bot_token=f"123456:{abs(chat_id)}-worker-token",
                bot_user_id=991001,
                enabled=True,
            ),
            OwnedBotProfile(
                owner_account_id=owner.id,
                account_id=bot_a.id,
                token_ciphertext="encrypted-worker-token",
                bot_user_id=991001,
                status="verified",
                enabled=True,
            ),
        ]
    )
    group = Group(group_id=chat_id, title="Worker Gate Group")
    test_db.add(group)
    await test_db.flush()
    binding = ManagedGroupBinding(
        group_id=group.id,
        telegram_group_id=chat_id,
        bot_account_id=bot_a.id,
        binding_status=binding_status,
        bot_role=ManagedGroupBotRole.ADMIN,
    )
    test_db.add(binding)
    await test_db.flush()
    asset = None
    if owned:
        asset = OwnedGroupAsset(
            internal_name=f"worker-{abs(chat_id)}",
            title="Worker Gate Group",
            owner_account_id=owner.id,
            status="ready",
            telegram_chat_id=chat_id,
            core_group_id=group.id,
            managed_binding_id=binding.id,
            guardian_bot_account_id=bot_a.id,
            governance_status="managed",
        )
        test_db.add(asset)
    await test_db.commit()
    return asset, group, binding, bot_a, bot_b


@pytest.mark.asyncio
async def test_worker_target_requires_exact_bot_active_binding_and_owned_links(test_db):
    asset, group, binding, bot_a, bot_b = await _seed_managed_target(
        test_db, chat_id=-10092001
    )

    allowed = await resolve_governance_worker_target(
        test_db,
        telegram_chat_id=-10092001,
        bot_account_id=bot_a.id,
    )
    assert allowed.allowed is True
    assert allowed.core_group_id == group.id
    assert allowed.managed_binding_id == binding.id
    assert allowed.owned_group_asset_id == asset.id

    mismatch = await resolve_governance_worker_target(
        test_db,
        telegram_chat_id=-10092001,
        bot_account_id=bot_b.id,
    )
    assert mismatch.allowed is False
    assert mismatch.reason_code == "mismatched_guardian_bot"

    binding.binding_status = ManagedGroupBindingStatus.DEGRADED
    await test_db.commit()
    inactive = await resolve_governance_worker_target(
        test_db,
        telegram_chat_id=-10092001,
        bot_account_id=bot_a.id,
    )
    assert inactive.allowed is False
    assert inactive.reason_code == "managed_binding_not_active"


@pytest.mark.asyncio
async def test_owned_gate_stop_does_not_stop_legacy_managed_group(test_db):
    _asset, _group, _binding, bot_a, _bot_b = await _seed_managed_target(
        test_db, chat_id=-10092002
    )
    stopped_owned = await resolve_governance_worker_target(
        test_db,
        telegram_chat_id=-10092002,
        bot_account_id=bot_a.id,
        owned_gate_reason="governance_stop_enabled",
    )
    assert stopped_owned.allowed is False
    assert stopped_owned.reason_code == "governance_stop_enabled"

    _asset, group, binding, legacy_bot, _bot_b = await _seed_managed_target(
        test_db, chat_id=-10092003, owned=False
    )
    allowed_legacy = await resolve_governance_worker_target(
        test_db,
        telegram_chat_id=-10092003,
        bot_account_id=legacy_bot.id,
        owned_gate_reason="governance_stop_enabled",
    )
    assert allowed_legacy.allowed is True
    assert allowed_legacy.core_group_id == group.id
    assert allowed_legacy.managed_binding_id == binding.id


@pytest.mark.asyncio
async def test_worker_rejects_a_core_group_mapped_to_a_different_chat(test_db):
    _asset, group, _binding, bot_a, _bot_b = await _seed_managed_target(
        test_db, chat_id=-10092008
    )
    group.group_id = -10092009
    await test_db.commit()

    target = await resolve_governance_worker_target(
        test_db,
        telegram_chat_id=-10092008,
        bot_account_id=bot_a.id,
    )

    assert target.allowed is False
    assert target.reason_code == "core_group_telegram_id_mismatch"


@pytest.mark.asyncio
async def test_unbound_owned_group_is_not_an_executable_target(test_db):
    owner, _bot_a, bot_b = await _seed_accounts(test_db, "unbound")
    asset = OwnedGroupAsset(
        internal_name="worker-unbound",
        title="Unbound Group",
        owner_account_id=owner.id,
        status="ready",
        telegram_chat_id=-10092004,
        governance_status="disabled",
    )
    test_db.add(asset)
    await test_db.commit()

    target = await resolve_governance_worker_target(
        test_db,
        telegram_chat_id=-10092004,
        bot_account_id=bot_b.id,
    )
    assert target.allowed is False
    assert target.is_owned_group is True
    assert target.reason_code == "owned_group_binding_missing"


def test_member_evaluation_has_exact_permissions_and_no_raw_payload():
    evaluation = evaluate_guardian_member(
        {
            "status": "administrator",
            "can_delete_messages": True,
            "can_restrict_members": False,
            "can_invite_users": True,
            "can_pin_messages": True,
            "user": {"token": "must-not-be-persisted"},
        },
        chat_type="supergroup",
        bot_user_id=9911,
    )
    assert evaluation.passed is False
    assert evaluation.reason_code == "guardian_permissions_missing"
    assert evaluation.missing_permissions == ("can_restrict_members",)
    assert evaluation.snapshot["missing_permissions"] == ["can_restrict_members"]
    assert "user" not in evaluation.snapshot
    assert "token" not in json.dumps(evaluation.snapshot)


@pytest.mark.asyncio
async def test_dispatch_passes_core_id_to_policies_and_chat_id_to_telegram(
    test_db, monkeypatch
):
    asset, group, _binding, bot_a, bot_b = await _seed_managed_target(
        test_db, chat_id=-10092005
    )
    monkeypatch.setattr(
        telegram_worker_module,
        "owned_group_governance_gate_reason",
        AsyncMock(return_value=None),
    )
    worker = TelegramWorker(TelegramWorkerRole.GUARDIAN_BOT, worker_id="gate-test")
    projector = AsyncMock()
    monkeypatch.setattr(
        telegram_worker_module,
        "project_owned_group_member_observations",
        projector,
    )
    bot = SimpleNamespace(
        handle_new_member=AsyncMock(return_value="welcome"),
        handle_member_leave=AsyncMock(),
        handle_message=AsyncMock(return_value=True),
    )
    telegram_client = SimpleNamespace(send_message=AsyncMock())
    message = {
        "message_id": 44,
        "chat": {"id": -10092005, "type": "supergroup"},
        "from": {"id": 701, "username": "member"},
        "new_chat_members": [{"id": 702, "username": "new_member"}],
        "text": "hello",
    }
    metrics = {
        "processed_updates": 0,
        "skipped_unbound_updates": 0,
        "skipped_mismatched_bot_updates": 0,
        "skipped_inactive_updates": 0,
        "skipped_gate_updates": 0,
        "skipped_not_managed_updates": 0,
    }

    processed = await worker._dispatch_guardian_message(
        bot,
        telegram_client,
        message,
        db=test_db,
        bot_account_id=bot_a.id,
        update_id=44,
        update_kind="message",
        governance_metrics=metrics,
    )
    assert processed == 2
    projector.assert_awaited_once_with(
        message,
        group_asset_id=asset.id,
        core_group_id=group.id,
        source_bot_account_id=bot_a.id,
        update_id=44,
        update_kind="message",
    )
    assert metrics["processed_updates"] == 1
    assert bot.handle_new_member.await_args.kwargs["core_group_id"] == group.id
    assert bot.handle_new_member.await_args.kwargs["chat_id"] == asset.telegram_chat_id
    assert bot.handle_message.await_args.kwargs["core_group_id"] == group.id
    telegram_client.send_message.assert_awaited_once_with(
        asset.telegram_chat_id, "welcome"
    )

    bot.handle_message.reset_mock()
    denied = await worker._dispatch_guardian_message(
        bot,
        telegram_client,
        message,
        db=test_db,
        bot_account_id=bot_b.id,
        update_id=45,
        update_kind="message",
        governance_metrics=metrics,
    )
    assert denied == 0
    assert projector.await_count == 1
    assert metrics["skipped_mismatched_bot_updates"] == 1
    bot.handle_message.assert_not_awaited()
    audit = await test_db.scalar(
        select(OwnedGroupAuditEvent).where(
            OwnedGroupAuditEvent.group_asset_id == asset.id,
            OwnedGroupAuditEvent.event_type
            == "owned_group_governance_update_skipped",
        )
    )
    assert audit is not None
    assert audit.reason_code == "mismatched_guardian_bot"


@pytest.mark.asyncio
async def test_dispatch_resamples_stop_for_each_owned_update(test_db, monkeypatch):
    asset, _group, _binding, bot_a, _bot_b = await _seed_managed_target(
        test_db, chat_id=-10092012
    )
    gate_reason = AsyncMock(side_effect=[None, "governance_stop_enabled"])
    monkeypatch.setattr(
        telegram_worker_module, "owned_group_governance_gate_reason", gate_reason
    )
    worker = TelegramWorker(TelegramWorkerRole.GUARDIAN_BOT, worker_id="mid-stop")
    bot = SimpleNamespace(
        handle_new_member=AsyncMock(),
        handle_member_leave=AsyncMock(),
        handle_message=AsyncMock(return_value=True),
    )
    telegram_client = SimpleNamespace(send_message=AsyncMock())
    metrics = {
        "processed_updates": 0,
        "skipped_unbound_updates": 0,
        "skipped_mismatched_bot_updates": 0,
        "skipped_inactive_updates": 0,
        "skipped_gate_updates": 0,
        "skipped_not_managed_updates": 0,
    }

    first = await worker._dispatch_guardian_message(
        bot,
        telegram_client,
        {
            "message_id": 1,
            "chat": {"id": asset.telegram_chat_id, "type": "supergroup"},
            "from": {"id": 8001},
            "text": "first",
        },
        db=test_db,
        bot_account_id=bot_a.id,
        update_id=1,
        update_kind="message",
        governance_metrics=metrics,
    )
    stopped = await worker._dispatch_guardian_message(
        bot,
        telegram_client,
        {
            "message_id": 2,
            "chat": {"id": asset.telegram_chat_id, "type": "supergroup"},
            "from": {"id": 8002},
            "text": "second",
        },
        db=test_db,
        bot_account_id=bot_a.id,
        update_id=2,
        update_kind="message",
        governance_metrics=metrics,
    )

    assert first == 1
    assert stopped == 0
    assert gate_reason.await_count == 2
    assert bot.handle_message.await_count == 1
    assert metrics["processed_updates"] == 1
    assert metrics["skipped_gate_updates"] == 1
    audit = await test_db.scalar(
        select(OwnedGroupAuditEvent)
        .where(
            OwnedGroupAuditEvent.group_asset_id == asset.id,
            OwnedGroupAuditEvent.reason_code == "governance_stop_enabled",
        )
        .order_by(OwnedGroupAuditEvent.id.desc())
    )
    assert audit is not None


@pytest.mark.asyncio
async def test_observation_failure_never_blocks_guardian_handlers(test_db, monkeypatch):
    asset, group, _binding, bot_a, _bot_b = await _seed_managed_target(
        test_db, chat_id=-10092013
    )
    monkeypatch.setattr(
        telegram_worker_module,
        "owned_group_governance_gate_reason",
        AsyncMock(return_value=None),
    )
    projector = AsyncMock(side_effect=RuntimeError("observation unavailable"))
    monkeypatch.setattr(
        telegram_worker_module,
        "project_owned_group_member_observations",
        projector,
    )
    worker = TelegramWorker(TelegramWorkerRole.GUARDIAN_BOT, worker_id="obs-fail")
    bot = SimpleNamespace(
        handle_new_member=AsyncMock(),
        handle_member_leave=AsyncMock(),
        handle_message=AsyncMock(return_value=True),
    )
    telegram_client = SimpleNamespace(send_message=AsyncMock())
    message = {
        "message_id": 3,
        "date": 1_789_117_200,
        "chat": {"id": asset.telegram_chat_id, "type": "supergroup"},
        "from": {"id": 8003, "is_bot": False},
        "text": "guardian still processes this",
    }

    processed = await worker._dispatch_guardian_message(
        bot,
        telegram_client,
        message,
        db=test_db,
        bot_account_id=bot_a.id,
        update_id=3,
        update_kind="message",
    )

    assert processed == 1
    projector.assert_awaited_once_with(
        message,
        group_asset_id=asset.id,
        core_group_id=group.id,
        source_bot_account_id=bot_a.id,
        update_id=3,
        update_kind="message",
    )
    bot.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_unbound_owned_sync_does_not_call_telegram_or_create_binding(
    test_db, monkeypatch
):
    owner, bot_a, _bot_b = await _seed_accounts(test_db, "sync-unbound")
    asset = OwnedGroupAsset(
        internal_name="sync-unbound",
        title="Sync Unbound",
        owner_account_id=owner.id,
        status="ready",
        telegram_chat_id=-10092006,
        governance_status="disabled",
    )
    test_db.add(asset)
    await test_db.commit()

    @asynccontextmanager
    async def fake_db_session():
        yield test_db

    monkeypatch.setattr(telegram_worker_module, "get_db_session", fake_db_session)
    monkeypatch.setattr(
        telegram_worker_module,
        "owned_group_governance_gate_reason",
        AsyncMock(return_value=None),
    )
    client = SimpleNamespace(
        get_chat=AsyncMock(),
        get_chat_member_count=AsyncMock(),
        get_chat_member=AsyncMock(),
    )
    worker = TelegramWorker(TelegramWorkerRole.GUARDIAN_BOT, worker_id="sync-test")
    synced = await worker._sync_guardian_group_from_chat(
        SimpleNamespace(id=8, account_id=bot_a.id),
        client,
        99006,
        {"id": -10092006, "type": "supergroup", "title": "Sync Unbound"},
    )
    assert synced is False
    client.get_chat.assert_not_awaited()
    client.get_chat_member_count.assert_not_awaited()
    client.get_chat_member.assert_not_awaited()
    audit = await test_db.scalar(
        select(OwnedGroupAuditEvent).where(
            OwnedGroupAuditEvent.group_asset_id == asset.id,
            OwnedGroupAuditEvent.event_type
            == "owned_group_governance_update_skipped",
        )
    )
    assert audit is not None


@pytest.mark.asyncio
async def test_sync_rechecks_current_owned_gate_after_telegram_probe(
    test_db, monkeypatch
):
    asset, _group, _binding, bot_a, _bot_b = await _seed_managed_target(
        test_db, chat_id=-10092008
    )

    @asynccontextmanager
    async def fake_db_session():
        yield test_db

    gate_reason = AsyncMock(side_effect=[None, "governance_stop_enabled"])
    sync_binding = AsyncMock()
    monkeypatch.setattr(telegram_worker_module, "get_db_session", fake_db_session)
    monkeypatch.setattr(
        telegram_worker_module, "owned_group_governance_gate_reason", gate_reason
    )
    monkeypatch.setattr(
        telegram_worker_module, "sync_managed_group_binding", sync_binding
    )

    client = SimpleNamespace(
        get_chat=AsyncMock(
            return_value=SimpleNamespace(
                title="Became Owned",
                username=None,
                type="supergroup",
                member_count=5,
            )
        ),
        get_chat_member_count=AsyncMock(),
        get_chat_member=AsyncMock(
            return_value={
                "status": "administrator",
                "can_delete_messages": True,
                "can_restrict_members": True,
                "can_invite_users": True,
                "can_pin_messages": True,
            }
        ),
    )
    worker = TelegramWorker(TelegramWorkerRole.GUARDIAN_BOT, worker_id="race-test")

    synced = await worker._sync_guardian_group_from_chat(
        SimpleNamespace(id=10, account_id=bot_a.id),
        client,
        991001,
        {"id": -10092008, "type": "supergroup", "title": "Became Owned"},
    )

    assert synced is False
    assert gate_reason.await_count == 2
    client.get_chat_member.assert_awaited_once()
    sync_binding.assert_not_awaited()
    audit = await test_db.scalar(
        select(OwnedGroupAuditEvent)
        .where(
            OwnedGroupAuditEvent.group_asset_id == asset.id,
            OwnedGroupAuditEvent.event_type
            == "owned_group_governance_update_skipped",
        )
        .order_by(OwnedGroupAuditEvent.id.desc())
    )
    assert audit is not None
    assert audit.reason_code == "governance_stop_enabled"


@pytest.mark.asyncio
async def test_sync_rejects_asset_inserted_after_final_worker_resolution(
    test_db, monkeypatch
):
    owner, bot_a, _bot_b = await _seed_accounts(test_db, "late-owned")
    chat_id = -10092011

    @asynccontextmanager
    async def fake_db_session():
        yield test_db

    resolver = AsyncMock(wraps=resolve_governance_worker_target)
    late_asset: OwnedGroupAsset | None = None

    async def insert_owned_asset_then_sync(db, **kwargs):
        nonlocal late_asset
        late_asset = OwnedGroupAsset(
            internal_name="late-owned-worker-race",
            title="Late Owned",
            owner_account_id=owner.id,
            status="ready",
            telegram_chat_id=chat_id,
            governance_status="disabled",
        )
        db.add(late_asset)
        await db.flush()
        return await guarded_sync_managed_group_binding(db, **kwargs)

    monkeypatch.setattr(telegram_worker_module, "get_db_session", fake_db_session)
    monkeypatch.setattr(
        telegram_worker_module, "resolve_governance_worker_target", resolver
    )
    monkeypatch.setattr(
        telegram_worker_module,
        "sync_managed_group_binding",
        insert_owned_asset_then_sync,
    )
    gate_reason = AsyncMock(return_value=None)
    monkeypatch.setattr(
        telegram_worker_module, "owned_group_governance_gate_reason", gate_reason
    )
    client = SimpleNamespace(
        get_chat=AsyncMock(
            return_value=SimpleNamespace(
                title="Late Owned",
                username=None,
                type="supergroup",
                member_count=3,
            )
        ),
        get_chat_member_count=AsyncMock(),
        get_chat_member=AsyncMock(return_value={"status": "administrator"}),
    )
    worker = TelegramWorker(TelegramWorkerRole.GUARDIAN_BOT, worker_id="late-race")

    synced = await worker._sync_guardian_group_from_chat(
        SimpleNamespace(id=11, account_id=bot_a.id),
        client,
        991001,
        {"id": chat_id, "type": "supergroup", "title": "Late Owned"},
    )

    assert synced is False
    assert resolver.await_count == 2
    gate_reason.assert_not_awaited()
    assert late_asset is not None
    assert await test_db.scalar(
        select(ManagedGroupBinding.id).where(
            ManagedGroupBinding.telegram_group_id == chat_id
        )
    ) is None


@pytest.mark.asyncio
async def test_worker_permission_loss_degrades_owned_asset_and_binding(
    test_db, monkeypatch
):
    asset, _group, binding, bot_a, _bot_b = await _seed_managed_target(
        test_db, chat_id=-10092007
    )

    @asynccontextmanager
    async def fake_db_session():
        yield test_db

    monkeypatch.setattr(telegram_worker_module, "get_db_session", fake_db_session)
    monkeypatch.setattr(
        telegram_worker_module,
        "owned_group_governance_gate_reason",
        AsyncMock(return_value=None),
    )
    client = SimpleNamespace(
        get_chat=AsyncMock(
            return_value=SimpleNamespace(
                title="Worker Gate Group",
                username=None,
                type="supergroup",
                member_count=10,
            )
        ),
        get_chat_member_count=AsyncMock(),
        get_chat_member=AsyncMock(
            return_value={
                "status": "administrator",
                "can_delete_messages": True,
                "can_restrict_members": False,
                "can_invite_users": True,
                "can_pin_messages": True,
            }
        ),
    )
    metrics = {"permission_degraded": 0}
    worker = TelegramWorker(TelegramWorkerRole.GUARDIAN_BOT, worker_id="degrade-test")
    synced = await worker._sync_guardian_group_from_chat(
        SimpleNamespace(id=9, account_id=bot_a.id),
        client,
        99007,
        {"id": -10092007, "type": "supergroup", "title": "Worker Gate Group"},
        governance_metrics=metrics,
    )
    await test_db.flush()

    assert synced is True
    assert asset.governance_status == "degraded"
    assert asset.governance_last_error_code == "guardian_permissions_missing"
    assert binding.binding_status == ManagedGroupBindingStatus.DEGRADED
    assert metrics["permission_degraded"] == 1
    snapshot = json.loads(binding.permissions_snapshot)
    assert snapshot["missing_permissions"] == ["can_restrict_members"]
    assert "bot_member" not in snapshot
    audit = await test_db.scalar(
        select(OwnedGroupAuditEvent).where(
            OwnedGroupAuditEvent.group_asset_id == asset.id,
            OwnedGroupAuditEvent.event_type == "owned_group_governance_degraded",
        )
    )
    assert audit is not None
    assert audit.reason_code == "guardian_permissions_missing"
    assert "token" not in (audit.after_state or "").lower()


@pytest.mark.asyncio
async def test_runtime_get_me_identity_cannot_overwrite_profile_ids(
    test_db, monkeypatch
):
    _asset, _group, _binding, bot_a, _bot_b = await _seed_managed_target(
        test_db, chat_id=-10092010
    )
    profile = await test_db.scalar(
        select(GuardianBotProfile).where(
            GuardianBotProfile.account_id == bot_a.id
        )
    )

    @asynccontextmanager
    async def fake_db_session():
        yield test_db

    monkeypatch.setattr(telegram_worker_module, "get_db_session", fake_db_session)
    worker = TelegramWorker(
        TelegramWorkerRole.GUARDIAN_BOT, worker_id="identity-test"
    )

    await worker._assert_guardian_runtime_identity(profile, 991001)
    with pytest.raises(RuntimeError, match="guardian_bot_identity_mismatch"):
        await worker._assert_guardian_runtime_identity(profile, 991999)

    await test_db.refresh(profile)
    assert profile.bot_user_id == 991001
