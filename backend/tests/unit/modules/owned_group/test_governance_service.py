from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, update

from app.core.account.models import (
    AccountStatus,
    AccountType,
    GuardianBotProfile,
    TelegramAccount,
)
from app.core.group.models import Group
from app.modules.guardian.models import (
    GroupModerationPolicy,
    GroupPunishmentPolicy,
    GroupVerificationConfig,
    ManagedGroupBinding,
)
from app.modules.owned_group.governance import GovernanceServiceError
from app.modules.owned_group.models import OwnedGroupAsset
from app.modules.owned_group.models_extra import OwnedBotProfile, OwnedGroupAuditEvent

governance = importlib.import_module("app.modules.owned_group.governance")


@pytest.fixture(autouse=True)
def allow_governance(monkeypatch):
    monkeypatch.setattr(
        governance,
        "require_owned_group_governance_available",
        AsyncMock(return_value=SimpleNamespace(governance_stop=False)),
    )


async def _seed_eligible(test_db, *, chat_id: int = -10091001):
    owner = TelegramAccount(
        identifier=f"owner-{abs(chat_id)}",
        session_name=f"owner-{abs(chat_id)}",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
        session_string="test-owner-session",
    )
    bot = TelegramAccount(
        identifier=f"guardian-{abs(chat_id)}",
        session_name=f"guardian-{abs(chat_id)}",
        account_type=AccountType.GUARDIAN_BOT,
        status=AccountStatus.IDLE,
        risk_level="normal",
        is_active=True,
        display_name="Governance Bot",
    )
    test_db.add_all([owner, bot])
    await test_db.flush()
    guardian_profile = GuardianBotProfile(
        account_id=bot.id,
        bot_token="123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        bot_username="governance_bot",
        bot_user_id=991001,
        enabled=True,
    )
    owned_profile = OwnedBotProfile(
        owner_account_id=owner.id,
        account_id=bot.id,
        token_ciphertext="encrypted-token-not-used-by-governance",
        bot_username="governance_bot",
        bot_user_id=991001,
        display_name="Governance Bot",
        status="verified",
        enabled=True,
    )
    asset = OwnedGroupAsset(
        internal_name=f"governance-{abs(chat_id)}",
        title="Governance Test Group",
        visibility="private",
        owner_account_id=owner.id,
        invite_mode="direct_invite",
        status="ready",
        telegram_chat_id=chat_id,
    )
    test_db.add_all([guardian_profile, owned_profile, asset])
    await test_db.commit()
    await test_db.refresh(asset)
    return asset, owner, bot, guardian_profile, owned_profile


def _install_client(
    monkeypatch,
    *,
    member: dict | None = None,
    chat_type: str = "supergroup",
    bot_user_id: int = 991001,
):
    calls: list[object] = []
    member = member or {
        "status": "administrator",
        "can_delete_messages": True,
        "can_restrict_members": True,
        "can_invite_users": True,
        "can_pin_messages": True,
    }

    class FakeClient:
        def __init__(self, config):
            calls.append(("init", config.bot_token))

        async def get_me(self):
            calls.append("get_me")
            return SimpleNamespace(
                user_id=bot_user_id,
                username="governance_bot",
                is_bot=True,
            )

        async def get_chat(self, chat_id):
            calls.append(("get_chat", chat_id))
            return SimpleNamespace(type=chat_type)

        async def get_chat_member(self, chat_id, user_id):
            calls.append(("get_chat_member", chat_id, user_id))
            return dict(member)

        async def close(self):
            calls.append("close")

    monkeypatch.setattr(governance, "TelegramClient", FakeClient)
    return calls


@pytest.mark.asyncio
async def test_locked_asset_refreshes_expire_on_commit_false_identity_map(test_db):
    asset, _owner, _bot, _guardian_profile, _owned_profile = await _seed_eligible(
        test_db, chat_id=-10091010
    )
    assert asset.status == "ready"
    await test_db.execute(
        update(OwnedGroupAsset)
        .where(OwnedGroupAsset.id == asset.id)
        .values(status="archived")
        .execution_options(synchronize_session=False)
    )
    assert asset.status == "ready"

    refreshed = await governance._locked_asset(test_db, asset.id)

    assert refreshed is asset
    assert refreshed.status == "archived"


@pytest.mark.asyncio
async def test_bind_builds_one_explicit_bridge_with_core_policy_keys(test_db, monkeypatch):
    asset, _owner, bot, _guardian_profile, _owned_profile = await _seed_eligible(test_db)
    calls = _install_client(monkeypatch)

    result = await governance.bind_governance(
        test_db,
        asset.id,
        bot.id,
        {"id": 71, "role": "admin"},
        "test-bind-success",
    )

    assert result["governance_status"] == "managed"
    assert result["telegram_chat_id"] == -10091001
    assert result["core_group_id"] is not None
    assert result["managed_binding_id"] is not None
    assert result["permission_probe"]["missing_permissions"] == []
    assert result["capabilities"]["verification"] is True
    assert result["reused"] is False
    assert calls == [
        ("init", "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
        "get_me",
        ("get_chat", -10091001),
        ("get_chat_member", -10091001, 991001),
        "close",
    ]

    core_group = await test_db.get(Group, result["core_group_id"])
    binding = await test_db.get(ManagedGroupBinding, result["managed_binding_id"])
    assert core_group is not None and core_group.group_id == -10091001
    assert binding is not None and binding.group_id == core_group.id
    assert binding.telegram_group_id == -10091001
    assert binding.bot_account_id == bot.id
    for model in (GroupVerificationConfig, GroupModerationPolicy, GroupPunishmentPolicy):
        row = await test_db.scalar(select(model).where(model.group_id == core_group.id))
        assert row is not None

    audit_types = set(
        (
            await test_db.scalars(
                select(OwnedGroupAuditEvent.event_type).where(
                    OwnedGroupAuditEvent.group_asset_id == asset.id
                )
            )
        ).all()
    )
    assert "owned_group_governance_bind_started" in audit_types
    assert "owned_group_governance_bound" in audit_types


@pytest.mark.asyncio
async def test_same_bot_bind_is_idempotent_without_second_probe(test_db, monkeypatch):
    asset, _owner, bot, _guardian_profile, _owned_profile = await _seed_eligible(test_db)
    calls = _install_client(monkeypatch)
    first = await governance.bind_governance(
        test_db, asset.id, bot.id, {"id": 72}, "test-bind-first"
    )
    calls.clear()

    second = await governance.bind_governance(
        test_db, asset.id, bot.id, {"id": 72}, "test-bind-retry"
    )

    assert second["reused"] is True
    assert second["core_group_id"] == first["core_group_id"]
    assert second["managed_binding_id"] == first["managed_binding_id"]
    assert calls == []
    assert await test_db.scalar(select(func.count(Group.id))) == 1
    assert await test_db.scalar(select(func.count(ManagedGroupBinding.id))) == 1


@pytest.mark.asyncio
async def test_missing_permission_degrades_and_returns_exact_list(test_db, monkeypatch):
    asset, _owner, bot, _guardian_profile, _owned_profile = await _seed_eligible(test_db)
    calls = _install_client(
        monkeypatch,
        member={
            "status": "administrator",
            "can_delete_messages": True,
            "can_restrict_members": False,
            "can_invite_users": True,
            "can_pin_messages": True,
        },
    )

    with pytest.raises(GovernanceServiceError) as caught:
        await governance.bind_governance(
            test_db, asset.id, bot.id, {"id": 73}, "test-bind-permission"
        )

    assert caught.value.reason == "guardian_permissions_missing"
    assert caught.value.missing_permissions == ["can_restrict_members"]
    await test_db.refresh(asset)
    assert asset.governance_status == "degraded"
    assert asset.governance_pending_at is None
    assert asset.governance_last_error_code == "guardian_permissions_missing"
    assert asset.managed_binding_id is None
    assert calls[-1] == "close"
    audit_types = set(
        await test_db.scalars(
            select(OwnedGroupAuditEvent.event_type).where(
                OwnedGroupAuditEvent.group_asset_id == asset.id
            )
        )
    )
    assert "owned_group_governance_degraded" in audit_types


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("member", "chat_type", "expected_reason"),
    [
        ({"status": "left"}, "supergroup", "guardian_bot_not_member"),
        (
            {"status": "member"},
            "supergroup",
            "guardian_bot_not_admin",
        ),
        (
            {
                "status": "administrator",
                "can_delete_messages": True,
                "can_restrict_members": True,
                "can_invite_users": True,
                "can_pin_messages": True,
            },
            "group",
            "telegram_chat_type_unsupported",
        ),
    ],
)
async def test_live_probe_rejects_invalid_membership_or_chat_type(
    test_db,
    monkeypatch,
    member,
    chat_type,
    expected_reason,
):
    asset, _owner, bot, _guardian_profile, _owned_profile = await _seed_eligible(
        test_db,
        chat_id={
            "guardian_bot_not_member": -10091011,
            "guardian_bot_not_admin": -10091012,
            "telegram_chat_type_unsupported": -10091013,
        }[expected_reason],
    )
    calls = _install_client(monkeypatch, member=member, chat_type=chat_type)

    with pytest.raises(GovernanceServiceError) as caught:
        await governance.bind_governance(
            test_db,
            asset.id,
            bot.id,
            {"id": 78},
            f"test-{expected_reason}",
        )

    assert caught.value.reason == expected_reason
    await test_db.refresh(asset)
    assert asset.governance_status == "degraded"
    assert asset.governance_pending_at is None
    assert asset.governance_last_error_code == expected_reason
    assert asset.managed_binding_id is None
    assert calls[-1] == "close"


@pytest.mark.asyncio
async def test_probe_timeout_degrades_with_stable_error(test_db, monkeypatch):
    asset, _owner, bot, _guardian_profile, _owned_profile = await _seed_eligible(
        test_db, chat_id=-10091014
    )
    calls: list[str] = []

    class TimeoutClient:
        def __init__(self, _config):
            calls.append("init")

        async def get_me(self):
            calls.append("get_me")
            raise TimeoutError("token=secret https://api.telegram.org/bot123:secret")

        async def close(self):
            calls.append("close")

    monkeypatch.setattr(governance, "TelegramClient", TimeoutClient)

    with pytest.raises(GovernanceServiceError) as caught:
        await governance.bind_governance(
            test_db,
            asset.id,
            bot.id,
            {"id": 79},
            "test-probe-timeout",
        )

    assert caught.value.reason == "telegram_probe_timeout"
    assert "secret" not in caught.value.message.lower()
    await test_db.refresh(asset)
    assert asset.governance_status == "degraded"
    assert asset.governance_last_error_code == "telegram_probe_timeout"
    assert "secret" not in (asset.governance_last_error_message or "").lower()
    assert calls == ["init", "get_me", "close"]


@pytest.mark.asyncio
async def test_probe_result_is_discarded_when_chat_changes_concurrently(
    test_db,
    monkeypatch,
):
    asset, _owner, bot, _guardian_profile, _owned_profile = await _seed_eligible(
        test_db, chat_id=-10091015
    )
    original_probe = governance.probe_guardian_permissions
    _install_client(monkeypatch)

    async def probe_after_concurrent_change(asset_arg, eligible, *, source):
        probe = await original_probe(asset_arg, eligible, source=source)
        await test_db.execute(
            update(OwnedGroupAsset)
            .where(OwnedGroupAsset.id == asset.id)
            .values(telegram_chat_id=-10091999)
            .execution_options(synchronize_session=False)
        )
        await test_db.commit()
        return probe

    monkeypatch.setattr(
        governance,
        "probe_guardian_permissions",
        probe_after_concurrent_change,
    )

    with pytest.raises(GovernanceServiceError) as caught:
        await governance.bind_governance(
            test_db,
            asset.id,
            bot.id,
            {"id": 80},
            "test-concurrent-chat-change",
        )

    assert caught.value.reason == "governance_state_changed"
    await test_db.refresh(asset)
    assert asset.telegram_chat_id == -10091999
    assert asset.core_group_id is None
    assert asset.managed_binding_id is None
    assert await test_db.scalar(select(func.count(ManagedGroupBinding.id))) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["bind", "reconcile"])
async def test_stop_during_probe_is_resampled_inside_lock_before_managed_write(
    test_db,
    monkeypatch,
    operation,
):
    asset, _owner, bot, _guardian_profile, _owned_profile = await _seed_eligible(
        test_db,
        chat_id=-10091016 if operation == "bind" else -10091017,
    )
    if operation == "reconcile":
        asset.guardian_bot_account_id = bot.id
        asset.governance_status = "degraded"
        await test_db.commit()

    events: list[str] = []
    telegram_calls = _install_client(monkeypatch)
    original_lock = governance.acquire_telegram_chat_transaction_lock
    original_probe = governance.probe_guardian_permissions

    async def tracked_probe(asset_arg, eligible, *, source):
        probe = await original_probe(asset_arg, eligible, source=source)
        events.append("probe_finished")
        return probe

    async def tracked_lock(db, chat_id):
        await original_lock(db, chat_id)
        events.append("chat_lock_acquired")

    async def stop_on_post_probe_gate():
        events.append("gate_checked")
        if events.count("gate_checked") == 2:
            assert telegram_calls[-1] == "close"
            raise HTTPException(
                status_code=503,
                detail={
                    "reason": "governance_stop_enabled",
                    "message": "Owned-group Guardian governance is stopped",
                    "retryable": True,
                },
            )
        return SimpleNamespace(governance_stop=False)

    monkeypatch.setattr(governance, "probe_guardian_permissions", tracked_probe)
    monkeypatch.setattr(governance, "acquire_telegram_chat_transaction_lock", tracked_lock)
    monkeypatch.setattr(
        governance,
        "require_owned_group_governance_available",
        stop_on_post_probe_gate,
    )

    correlation_id = f"test-{operation}-stop-during-probe"
    with pytest.raises(GovernanceServiceError) as caught:
        if operation == "bind":
            await governance.bind_governance(
                test_db,
                asset.id,
                bot.id,
                {"id": 81},
                correlation_id,
            )
        else:
            await governance.reconcile_governance(
                test_db,
                asset.id,
                {"id": 81},
                correlation_id,
            )

    assert events == [
        "gate_checked",
        "probe_finished",
        "chat_lock_acquired",
        "gate_checked",
    ]
    assert caught.value.reason == "governance_stop_enabled"
    assert caught.value.status_code == 503
    assert caught.value.retryable is True
    assert caught.value.correlation_id == correlation_id

    await test_db.refresh(asset)
    assert asset.governance_status == "degraded"
    assert asset.governance_pending_at is None
    assert asset.governance_last_error_code == "governance_stop_enabled"
    assert asset.core_group_id is None
    assert asset.managed_binding_id is None
    assert await test_db.scalar(select(func.count(Group.id))) == 0
    assert await test_db.scalar(select(func.count(ManagedGroupBinding.id))) == 0

    audit_events = (
        await test_db.scalars(
            select(OwnedGroupAuditEvent)
            .where(OwnedGroupAuditEvent.group_asset_id == asset.id)
            .order_by(OwnedGroupAuditEvent.id)
        )
    ).all()
    assert [event.event_type for event in audit_events] == [
        f"owned_group_governance_{operation}_started",
        f"owned_group_governance_{operation}_failed",
        "owned_group_governance_degraded",
    ]
    assert all(event.correlation_id == correlation_id for event in audit_events)
    assert audit_events[-1].reason_code == "governance_stop_enabled"


@pytest.mark.asyncio
async def test_unexpected_error_after_pending_is_safely_degraded(test_db, monkeypatch):
    asset, _owner, bot, _guardian_profile, _owned_profile = await _seed_eligible(
        test_db, chat_id=-10091009
    )
    monkeypatch.setattr(
        governance,
        "probe_guardian_permissions",
        AsyncMock(side_effect=RuntimeError("secret token and raw Telegram URL")),
    )

    with pytest.raises(GovernanceServiceError) as caught:
        await governance.bind_governance(
            test_db, asset.id, bot.id, {"id": 77}, "test-unexpected-error"
        )

    assert caught.value.reason == "governance_internal_error"
    assert "secret" not in caught.value.message.lower()
    await test_db.refresh(asset)
    assert asset.governance_status == "degraded"
    assert asset.governance_pending_at is None
    assert asset.governance_last_error_code == "governance_internal_error"
    assert "secret" not in (asset.governance_last_error_message or "").lower()


@pytest.mark.asyncio
async def test_profile_identity_conflict_stops_before_telegram(test_db, monkeypatch):
    asset, _owner, bot, _guardian_profile, owned_profile = await _seed_eligible(test_db)
    owned_profile.bot_user_id = 123456
    await test_db.commit()
    client = AsyncMock(side_effect=AssertionError("Telegram must not be called"))
    monkeypatch.setattr(governance, "TelegramClient", client)

    with pytest.raises(GovernanceServiceError) as caught:
        await governance.bind_governance(
            test_db, asset.id, bot.id, {"id": 74}, "test-identity-conflict"
        )

    assert caught.value.reason == "guardian_bot_identity_mismatch"
    client.assert_not_called()
    await test_db.refresh(asset)
    assert asset.governance_status == "degraded"
    assert asset.governance_last_error_code == "guardian_bot_identity_mismatch"
    assert await test_db.scalar(select(func.count(ManagedGroupBinding.id))) == 0
    audit_types = set(
        await test_db.scalars(
            select(OwnedGroupAuditEvent.event_type).where(
                OwnedGroupAuditEvent.group_asset_id == asset.id
            )
        )
    )
    assert "owned_group_governance_bind_failed" in audit_types
    assert "owned_group_governance_degraded" in audit_types


@pytest.mark.asyncio
async def test_reconcile_recovers_degraded_asset_without_duplicate_rows(test_db, monkeypatch):
    asset, _owner, bot, _guardian_profile, _owned_profile = await _seed_eligible(test_db)
    _install_client(
        monkeypatch,
        member={
            "status": "member",
            "can_delete_messages": False,
            "can_restrict_members": False,
            "can_invite_users": False,
            "can_pin_messages": False,
        },
    )
    with pytest.raises(GovernanceServiceError) as caught:
        await governance.bind_governance(
            test_db, asset.id, bot.id, {"id": 75}, "test-degrade"
        )
    assert caught.value.reason == "guardian_bot_not_admin"

    _install_client(monkeypatch)
    recovered = await governance.reconcile_governance(
        test_db, asset.id, {"id": 75}, "test-recover"
    )

    assert recovered["governance_status"] == "managed"
    assert await test_db.scalar(select(func.count(Group.id))) == 1
    assert await test_db.scalar(select(func.count(ManagedGroupBinding.id))) == 1


@pytest.mark.asyncio
async def test_reconcile_replaces_legacy_binding_snapshot_with_safe_fields(
    test_db, monkeypatch
):
    asset, _owner, bot, _guardian_profile, _owned_profile = await _seed_eligible(
        test_db, chat_id=-10091008
    )
    _install_client(monkeypatch)
    bound = await governance.bind_governance(
        test_db, asset.id, bot.id, {"id": 76}, "test-safe-snapshot-bind"
    )
    binding = await test_db.get(ManagedGroupBinding, bound["managed_binding_id"])
    binding.permissions_snapshot = (
        '{"bot_member":{"token":"must-disappear"},"raw":"secret-url",'
        '"legacy_key":"legacy-value"}'
    )
    await test_db.commit()

    await governance.reconcile_governance(
        test_db, asset.id, {"id": 76}, "test-safe-snapshot-reconcile"
    )
    await test_db.refresh(binding)

    snapshot = governance.json.loads(binding.permissions_snapshot)
    assert snapshot["source"] == "owned_group_governance_reconcile"
    assert snapshot["missing_permissions"] == []
    assert "bot_member" not in snapshot
    assert "raw" not in snapshot
    assert "legacy_key" not in snapshot
    assert "token" not in binding.permissions_snapshot


@pytest.mark.asyncio
async def test_get_status_is_read_only_and_reports_stale_pending(test_db, monkeypatch):
    asset, _owner, bot, _guardian_profile, _owned_profile = await _seed_eligible(test_db)
    asset.guardian_bot_account_id = bot.id
    asset.governance_status = "pending"
    asset.governance_pending_at = governance._now() - governance.PENDING_STALE_AFTER * 2
    await test_db.commit()
    client = AsyncMock(side_effect=AssertionError("GET must not call Telegram"))
    monkeypatch.setattr(governance, "TelegramClient", client)
    before_updated_at = asset.updated_at

    result = await governance.get_governance_status(test_db, asset.id)

    assert result["governance_status"] == "pending"
    assert result["stale_pending"] is True
    client.assert_not_called()
    await test_db.refresh(asset)
    assert asset.updated_at == before_updated_at


@pytest.mark.asyncio
async def test_candidate_list_is_backend_filtered_and_secret_free(test_db):
    asset, _owner, bot, _guardian_profile, _owned_profile = await _seed_eligible(test_db)

    candidates = await governance.list_eligible_guardian_bots(test_db, asset.id)

    assert len(candidates) == 1
    assert candidates[0]["guardian_bot_account_id"] == bot.id
    assert candidates[0]["owned_profile_status"] == "verified"
    assert "token" not in str(candidates).lower()
