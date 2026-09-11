from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.managed_groups import (
    ManagedGroupBindingCreate,
    ManagedGroupSyncConfirmedRequest,
    _reject_owned_binding_override,
    _require_owned_governance_action,
    create_managed_group_binding,
    list_managed_groups,
    require_managed_group_operator,
    sync_confirmed_groups_for_bot,
)
from app.core.account.models import (
    AccountStatus,
    AccountType,
    GuardianBotProfile,
    TelegramAccount,
)
from app.core.group.models import Group
from app.modules.guardian.models import (
    ManagedGroupBinding,
    ManagedGroupBindingStatus,
    ManagedGroupBotRole,
)
from app.modules.guardian.sync import sync_managed_group_binding
from app.modules.owned_group.models import OwnedGroupAsset

managed_groups_module = importlib.import_module("app.api.managed_groups")


@pytest.mark.asyncio
async def test_managed_group_list_derives_owned_group_source_without_copying_state(
    test_db,
    monkeypatch,
):
    owner = TelegramAccount(
        identifier="managed-source-owner",
        session_name="managed-source-owner",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    bot = TelegramAccount(
        identifier="managed-source-bot",
        session_name="managed-source-bot",
        display_name="Managed Source Bot",
        account_type=AccountType.GUARDIAN_BOT,
        status=AccountStatus.IDLE,
        is_active=True,
    )
    test_db.add_all([owner, bot])
    await test_db.flush()

    owned_group = Group(group_id=-10093001, title="Owned Managed Group")
    legacy_group = Group(group_id=-10093002, title="Legacy Managed Group")
    test_db.add_all([owned_group, legacy_group])
    await test_db.flush()
    owned_binding = ManagedGroupBinding(
        group_id=owned_group.id,
        telegram_group_id=owned_group.group_id,
        bot_account_id=bot.id,
        binding_status=ManagedGroupBindingStatus.ACTIVE,
        bot_role=ManagedGroupBotRole.ADMIN,
    )
    legacy_binding = ManagedGroupBinding(
        group_id=legacy_group.id,
        telegram_group_id=legacy_group.group_id,
        bot_account_id=bot.id,
        binding_status=ManagedGroupBindingStatus.ACTIVE,
        bot_role=ManagedGroupBotRole.ADMIN,
    )
    test_db.add_all([owned_binding, legacy_binding])
    await test_db.flush()
    asset = OwnedGroupAsset(
        internal_name="managed-source-asset",
        title="Owned Asset Title",
        owner_account_id=owner.id,
        status="ready",
        telegram_chat_id=owned_group.group_id,
        core_group_id=owned_group.id,
        managed_binding_id=owned_binding.id,
        guardian_bot_account_id=bot.id,
        governance_status="managed",
    )
    test_db.add(asset)
    await test_db.commit()

    response = await list_managed_groups(
        bot_account_id=None,
        binding_status=None,
        limit=100,
        db=test_db,
    )
    items = {item.telegram_group_id: item for item in response.data}

    assert items[-10093001].source_type == "owned_group"
    assert items[-10093001].owned_group_asset_id == asset.id
    assert items[-10093001].owned_group_asset_title == "Owned Asset Title"
    assert items[-10093002].source_type == "managed_group"
    assert items[-10093002].owned_group_asset_id is None
    assert items[-10093002].owned_group_asset_title is None

    with pytest.raises(HTTPException) as gate_error:
        monkeypatch.setattr(
            managed_groups_module,
            "owned_group_governance_gate_reason",
            AsyncMock(return_value="governance_stop_enabled"),
        )
        await _require_owned_governance_action(test_db, owned_binding)
    assert gate_error.value.status_code == 503
    assert gate_error.value.detail["reason"] == "governance_stop_enabled"

    with pytest.raises(HTTPException) as override_error:
        await _reject_owned_binding_override(test_db, owned_binding)
    assert override_error.value.status_code == 409
    assert (
        override_error.value.detail["reason"]
        == "owned_group_binding_managed_by_governance"
    )

    # Historical partial links must also fail closed by the internal core ID.
    asset.managed_binding_id = None
    asset.telegram_chat_id = -10093999
    await test_db.commit()
    with pytest.raises(HTTPException) as partial_link_error:
        await _reject_owned_binding_override(test_db, owned_binding)
    assert partial_link_error.value.status_code == 409


@pytest.mark.asyncio
async def test_managed_group_write_role_rejects_auditor():
    with pytest.raises(HTTPException) as caught:
        await require_managed_group_operator({"id": 9, "role": "auditor"})

    assert caught.value.status_code == 403
    assert caught.value.detail == {
        "reason": "owned_group_role_forbidden",
        "message": "Managed group operator access required",
        "retryable": False,
    }


@pytest.mark.asyncio
async def test_sync_confirmed_owned_only_never_creates_telegram_client(
    test_db,
    monkeypatch,
):
    owner = TelegramAccount(
        identifier="owned-only-sync-owner",
        session_name="owned-only-sync-owner",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    bot = TelegramAccount(
        identifier="owned-only-sync-bot",
        session_name="owned-only-sync-bot",
        account_type=AccountType.GUARDIAN_BOT,
        status=AccountStatus.IDLE,
        is_active=True,
    )
    test_db.add_all([owner, bot])
    await test_db.flush()
    test_db.add(
        GuardianBotProfile(
            account_id=bot.id,
            bot_token="123456:test-token",
            enabled=True,
        )
    )
    group = Group(group_id=-10093003, title="Owned Only Sync", status="active")
    test_db.add(group)
    await test_db.flush()
    asset = OwnedGroupAsset(
        internal_name="owned-only-sync-asset",
        title="Owned Only Sync",
        owner_account_id=owner.id,
        status="ready",
        telegram_chat_id=group.group_id,
        core_group_id=group.id,
        governance_status="disabled",
    )
    test_db.add(asset)
    await test_db.commit()

    class UnexpectedTelegramClient:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("owned-only sync must not create a Telegram client")

    monkeypatch.setattr(managed_groups_module, "TelegramClient", UnexpectedTelegramClient)

    response = await sync_confirmed_groups_for_bot(
        ManagedGroupSyncConfirmedRequest(bot_account_id=bot.id),
        test_db,
    )

    assert response.data == {
        "checked": 1,
        "synced": 0,
        "skipped": 1,
        "errors": [],
        "details": [
            {
                "telegram_group_id": group.group_id,
                "action": "skip_owned_group_governance_required",
                "owned_group_asset_id": asset.id,
            }
        ],
    }


@pytest.mark.asyncio
async def test_generic_binding_create_rejects_asset_classified_after_precheck(
    test_db,
    monkeypatch,
):
    owner = TelegramAccount(
        identifier="late-create-owner",
        session_name="late-create-owner",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    bot = TelegramAccount(
        identifier="late-create-bot",
        session_name="late-create-bot",
        account_type=AccountType.GUARDIAN_BOT,
        status=AccountStatus.IDLE,
        is_active=True,
    )
    test_db.add_all([owner, bot])
    await test_db.commit()
    chat_id = -10093004
    late_asset: OwnedGroupAsset | None = None

    async def classify_then_sync(db, **kwargs):
        nonlocal late_asset
        assert kwargs["reject_owned_group_auto_bind"] is True
        late_asset = OwnedGroupAsset(
            internal_name="late-generic-create-asset",
            title="Late Generic Create",
            owner_account_id=owner.id,
            status="ready",
            telegram_chat_id=chat_id,
            governance_status="disabled",
        )
        db.add(late_asset)
        await db.flush()
        return await sync_managed_group_binding(db, **kwargs)

    monkeypatch.setattr(
        managed_groups_module,
        "sync_managed_group_binding",
        classify_then_sync,
    )

    with pytest.raises(HTTPException) as caught:
        await create_managed_group_binding(
            ManagedGroupBindingCreate(
                bot_account_id=bot.id,
                telegram_group_id=chat_id,
                title="Must Not Auto Bind",
                chat_type="supergroup",
            ),
            test_db,
        )

    assert caught.value.status_code == 400
    assert caught.value.detail == (
        "Self-owned groups require explicit Guardian governance binding"
    )
    assert late_asset is not None
    assert await test_db.scalar(
        select(ManagedGroupBinding.id).where(
            ManagedGroupBinding.telegram_group_id == chat_id
        )
    ) is None


@pytest.mark.asyncio
async def test_confirmed_sync_rejects_asset_classified_after_precheck(
    test_db,
    monkeypatch,
):
    owner = TelegramAccount(
        identifier="late-scan-owner",
        session_name="late-scan-owner",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    bot = TelegramAccount(
        identifier="late-scan-bot",
        session_name="late-scan-bot",
        account_type=AccountType.GUARDIAN_BOT,
        status=AccountStatus.IDLE,
        is_active=True,
    )
    test_db.add_all([owner, bot])
    await test_db.flush()
    test_db.add(
        GuardianBotProfile(
            account_id=bot.id,
            bot_token="123456:test-token",
            enabled=True,
        )
    )
    group = Group(group_id=-10093005, title="Late Confirmed Scan", status="active")
    test_db.add(group)
    await test_db.commit()
    late_asset: OwnedGroupAsset | None = None

    class FakeTelegramClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def get_me(self):
            return SimpleNamespace(user_id=93005, username="late_scan_bot")

        async def get_chat_member(self, _chat_id, _user_id):
            return {"status": "administrator"}

        async def get_chat(self, _chat_id):
            return SimpleNamespace(
                title=group.title,
                username=None,
                member_count=0,
                type="supergroup",
            )

        async def close(self):
            return None

    async def classify_then_sync(db, **kwargs):
        nonlocal late_asset
        assert kwargs["reject_owned_group_auto_bind"] is True
        late_asset = OwnedGroupAsset(
            internal_name="late-confirmed-scan-asset",
            title="Late Confirmed Scan",
            owner_account_id=owner.id,
            status="ready",
            telegram_chat_id=group.group_id,
            core_group_id=group.id,
            governance_status="disabled",
        )
        db.add(late_asset)
        await db.flush()
        return await sync_managed_group_binding(db, **kwargs)

    monkeypatch.setattr(managed_groups_module, "TelegramClient", FakeTelegramClient)
    monkeypatch.setattr(
        managed_groups_module,
        "sync_managed_group_binding",
        classify_then_sync,
    )

    response = await sync_confirmed_groups_for_bot(
        ManagedGroupSyncConfirmedRequest(bot_account_id=bot.id),
        test_db,
    )

    assert response.data["synced"] == 0
    assert response.data["skipped"] == 1
    assert response.data["errors"] == []
    assert response.data["details"] == [
        {
            "telegram_group_id": group.group_id,
            "action": "skip_conflict",
            "error": "Self-owned groups require explicit Guardian governance binding",
        }
    ]
    assert late_asset is not None
    assert await test_db.scalar(
        select(ManagedGroupBinding.id).where(
            ManagedGroupBinding.telegram_group_id == group.group_id
        )
    ) is None
