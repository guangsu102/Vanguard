from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select

from app.core.account.models import (
    AccountOperationConfig,
    AccountStatus,
    AccountType,
    GuardianBotProfile,
    TelegramAccount,
)
from app.core.governance_gate import GovernanceGateState
from app.core.group.models import Group
from app.core.user.models import User, UserState
from app.modules.guardian.models import (
    ManagedGroupBinding,
    ManagedGroupBindingStatus,
    ManagedGroupBotRole,
)
from app.modules.owned_group.models import OwnedGroupAsset
from app.modules.owned_group.models_extra import (
    OwnedBotProfile,
    OwnedGroupMemberObservation,
    OwnedGroupMembership,
)
from app.modules.owned_group.operations_read_model import OwnedGroupOperationsReadModel
from app.modules.owned_group.operations_schemas import MemberQuery


async def _seed(test_db):
    owner = TelegramAccount(
        identifier="stage5-owner-phone-secret",
        session_name="stage5-owner",
        session_string="stage5-owner-session-secret",
        display_name="Growth Owner",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        risk_level="normal",
        is_active=True,
    )
    bot = TelegramAccount(
        identifier="stage5-bot-secret-identifier",
        session_name="stage5-bot",
        display_name="Guardian Account",
        account_type=AccountType.GUARDIAN_BOT,
        status=AccountStatus.IDLE,
        is_active=True,
    )
    group = Group(group_id=-100551100, title="Stage 5 Group")
    test_db.add_all([owner, bot, group])
    await test_db.flush()

    binding = ManagedGroupBinding(
        group_id=group.id,
        telegram_group_id=group.group_id,
        bot_account_id=bot.id,
        binding_status=ManagedGroupBindingStatus.ACTIVE,
        bot_role=ManagedGroupBotRole.ADMIN,
    )
    owned_bot = OwnedBotProfile(
        owner_account_id=owner.id,
        account_id=bot.id,
        bot_user_id=551102,
        bot_username="stage5_guardian",
        display_name="Stage 5 Guardian",
        token_ciphertext="owned-bot-token-secret",
        status="verified",
        enabled=True,
    )
    guardian_profile = GuardianBotProfile(
        account_id=bot.id,
        bot_token="551102:THIS_IS_A_LONG_BOT_TOKEN_SECRET",
        bot_user_id=551102,
        bot_username="stage5_guardian",
        health_status="healthy",
        sync_status="synced",
        enabled=True,
    )
    test_db.add_all([binding, owned_bot, guardian_profile])
    await test_db.flush()

    asset = OwnedGroupAsset(
        internal_name="stage5-asset",
        title="Stage 5 Operations",
        visibility="private",
        owner_account_id=owner.id,
        telegram_chat_id=group.group_id,
        core_group_id=group.id,
        managed_binding_id=binding.id,
        guardian_bot_account_id=bot.id,
        status="ready",
        governance_status="managed",
        member_count=2,
    )
    test_db.add(asset)
    await test_db.flush()
    now = datetime.now(UTC)
    user = User(
        telegram_id=551103,
        username="alice_newer",
        state=UserState.ACTIVE,
        warning_count=2,
        updated_at=(now + timedelta(seconds=1)).replace(tzinfo=None),
    )
    test_db.add(user)
    test_db.add_all(
        [
            AccountOperationConfig(account_id=owner.id, operation_mode="growth"),
            OwnedGroupMembership(
                group_asset_id=asset.id,
                resource_type="user",
                resource_id=owner.id,
                telegram_user_id=551101,
                status="member_verified",
                is_admin=True,
                admin_title="Owner",
                last_verified_at=(now - timedelta(minutes=2)).replace(tzinfo=None),
            ),
            OwnedGroupMembership(
                group_asset_id=asset.id,
                resource_type="bot",
                resource_id=owned_bot.id,
                telegram_user_id=551102,
                status="admin_verified",
                is_admin=True,
                admin_title="Guardian",
                last_verified_at=(now - timedelta(minutes=1)).replace(tzinfo=None),
            ),
            OwnedGroupMemberObservation(
                group_asset_id=asset.id,
                telegram_user_id=551101,
                is_bot=False,
                username_snapshot="growth_owner",
                display_name_snapshot="Growth Owner Public",
                presence_status="present",
                last_event_type="message",
                source_bot_account_id=bot.id,
                last_update_id=1,
                first_observed_at=now - timedelta(minutes=10),
                last_observed_at=now - timedelta(minutes=5),
            ),
            OwnedGroupMemberObservation(
                group_asset_id=asset.id,
                telegram_user_id=551103,
                is_bot=False,
                username_snapshot="alice_old",
                display_name_snapshot="Alice %_ Literal",
                presence_status="left",
                last_event_type="left_chat_member",
                source_bot_account_id=bot.id,
                last_update_id=2,
                first_observed_at=now - timedelta(minutes=8),
                last_observed_at=now,
                left_at=now,
            ),
        ]
    )
    await test_db.commit()
    return asset, owner, bot


def _service(test_db) -> OwnedGroupOperationsReadModel:
    async def gate():
        return GovernanceGateState(
            governance_stop=False,
            backend_available=True,
        )

    async def messaging(_db):
        return {"enabled": False, "dryRun": True}

    return OwnedGroupOperationsReadModel(
        test_db,
        governance_gate_reader=gate,
        messaging_settings_reader=messaging,
    )


@pytest.mark.asyncio
async def test_summary_and_members_use_one_deduplicated_read_model(test_db, monkeypatch):
    asset, owner, _bot = await _seed(test_db)
    monkeypatch.setattr(
        "app.modules.owned_group.operations_read_model.is_owned_group_governance_enabled",
        lambda: True,
    )
    service = _service(test_db)

    summary = await service.get_operations_center(asset.id, role="operator")
    members, total, coverage, member_summary = await service.list_members(
        asset.id,
        MemberQuery(),
    )

    assert total == 3
    assert summary.member_summary.counts_by_kind.model_dump() == {
        "real_user": 1,
        "system_ad_account": 1,
        "system_bot": 1,
    }
    assert member_summary.counts_by_kind == summary.member_summary.counts_by_kind
    assert coverage.coverage_status == "collecting"
    assert coverage.is_complete is False
    assert summary.asset.managed_resource_count == 2
    assert summary.permissions.manage_orchestration is True
    assert summary.permissions.manage_messaging is False
    assert not any("telegram_member_count" in item.model_dump() for item in members)
    owner_row = next(item for item in members if item.account_id == owner.id)
    assert owner_row.member_kind == "system_ad_account"
    assert owner_row.telegram_role == "owner"
    assert owner_row.operation_mode == "growth"


@pytest.mark.asyncio
async def test_search_is_literal_or_exact_and_summary_remains_unfiltered(test_db, monkeypatch):
    asset, owner, _bot = await _seed(test_db)
    monkeypatch.setattr(
        "app.modules.owned_group.operations_read_model.is_owned_group_governance_enabled",
        lambda: True,
    )
    service = _service(test_db)

    literal, literal_total, _coverage, summary = await service.list_members(
        asset.id,
        MemberQuery(q="%_"),
    )
    numeric, numeric_total, *_ = await service.list_members(
        asset.id,
        MemberQuery(q="551103"),
    )
    account, account_total, *_ = await service.list_members(
        asset.id,
        MemberQuery(q=f"account:{owner.id}"),
    )

    assert literal_total == 1
    assert literal[0].display_name == "Alice %_ Literal"
    assert numeric_total == 1 and numeric[0].telegram_user_id == 551103
    assert account_total == 1 and account[0].account_id == owner.id
    assert summary.counts_by_kind.system_bot == 1


@pytest.mark.asyncio
async def test_user_profile_is_safe_supplement_and_secrets_never_enter_dto(test_db, monkeypatch):
    asset, _owner, _bot = await _seed(test_db)
    monkeypatch.setattr(
        "app.modules.owned_group.operations_read_model.is_owned_group_governance_enabled",
        lambda: True,
    )

    members, *_ = await _service(test_db).list_members(asset.id, MemberQuery())
    user = next(item for item in members if item.member_kind == "real_user")
    encoded = user.model_dump_json()

    assert user.username == "alice_newer"
    assert [source.source for source in user.sources][-1] == "user_profile"
    assert "stage5-owner-session-secret" not in encoded
    assert "LONG_BOT_TOKEN_SECRET" not in encoded
    assert "owned-bot-token-secret" not in encoded
    assert "permissions_snapshot" not in encoded


@pytest.mark.asyncio
async def test_gets_do_not_mutate_observation_rows(test_db, monkeypatch):
    asset, *_ = await _seed(test_db)
    monkeypatch.setattr(
        "app.modules.owned_group.operations_read_model.is_owned_group_governance_enabled",
        lambda: True,
    )
    before = await test_db.scalar(select(func.count(OwnedGroupMemberObservation.id)))

    service = _service(test_db)
    await service.get_operations_center(asset.id, role="auditor")
    await service.list_members(asset.id, MemberQuery(limit=1))

    after = await test_db.scalar(select(func.count(OwnedGroupMemberObservation.id)))
    assert before == after == 2


@pytest.mark.asyncio
async def test_gate_backend_unavailable_degrades_coverage(test_db, monkeypatch):
    asset, *_ = await _seed(test_db)
    monkeypatch.setattr(
        "app.modules.owned_group.operations_read_model.is_owned_group_governance_enabled",
        lambda: True,
    )

    async def unavailable_gate():
        return GovernanceGateState(governance_stop=False, backend_available=False)

    service = OwnedGroupOperationsReadModel(
        test_db,
        governance_gate_reader=unavailable_gate,
        messaging_settings_reader=_service(test_db)._messaging_settings_reader,
    )
    _members, _total, coverage, _summary = await service.list_members(
        asset.id,
        MemberQuery(),
    )
    operations = await service.get_operations_center(asset.id, role="operator")

    assert coverage.coverage_status == "degraded"
    assert "governance_gate_backend_unavailable" in coverage.blocking_reasons
    assert operations.sections.governance.state == "degraded"
    assert operations.sections.governance.can_execute is False
    assert operations.sections.activities.state == "degraded"
    assert operations.sections.activities.can_execute is False
    assert "governance_gate_backend_unavailable" in (
        operations.sections.governance.blocking_reasons
    )


@pytest.mark.asyncio
async def test_governance_stop_keeps_history_readable_and_marks_observation_paused(
    test_db,
    monkeypatch,
):
    asset, *_ = await _seed(test_db)
    monkeypatch.setattr(
        "app.modules.owned_group.operations_read_model.is_owned_group_governance_enabled",
        lambda: True,
    )

    async def stopped_gate():
        return GovernanceGateState(governance_stop=True, backend_available=True)

    service = OwnedGroupOperationsReadModel(
        test_db,
        governance_gate_reader=stopped_gate,
        messaging_settings_reader=_service(test_db)._messaging_settings_reader,
    )
    members, _total, coverage, _summary = await service.list_members(
        asset.id,
        MemberQuery(),
    )

    assert members
    assert coverage.coverage_status == "paused"
    assert "member_observation_paused" in coverage.blocking_reasons
    assert "governance_stop_enabled" in coverage.blocking_reasons


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "asset_status",
    ["draft", "prechecking", "creating", "create_failed", "needs_attention"],
)
async def test_non_ready_asset_makes_governance_and_activities_unavailable(
    test_db,
    monkeypatch,
    asset_status,
):
    asset, *_ = await _seed(test_db)
    asset.status = asset_status
    asset.telegram_chat_id -= 1
    await test_db.commit()
    monkeypatch.setattr(
        "app.modules.owned_group.operations_read_model.is_owned_group_governance_enabled",
        lambda: True,
    )

    operations = await _service(test_db).get_operations_center(asset.id, role="operator")

    assert operations.member_summary.coverage.coverage_status == "degraded"
    assert operations.sections.governance.state == "unavailable"
    assert operations.sections.governance.can_execute is False
    assert "asset_not_ready" in operations.sections.governance.blocking_reasons
    assert operations.sections.activities.state == "unavailable"
    assert operations.sections.activities.can_execute is False
    assert "asset_not_ready" in operations.sections.activities.blocking_reasons


@pytest.mark.asyncio
async def test_binding_loads_owned_bot_profile_by_account_without_owned_membership(
    test_db,
    monkeypatch,
):
    asset, _owner, bot = await _seed(test_db)
    await test_db.execute(
        delete(OwnedGroupMembership).where(OwnedGroupMembership.resource_type == "bot")
    )
    owned_profile = await test_db.scalar(
        select(OwnedBotProfile).where(OwnedBotProfile.account_id == bot.id)
    )
    owned_profile.bot_user_id = 999551102
    await test_db.commit()
    monkeypatch.setattr(
        "app.modules.owned_group.operations_read_model.is_owned_group_governance_enabled",
        lambda: True,
    )

    members, *_ = await _service(test_db).list_members(asset.id, MemberQuery())

    conflict = next(item for item in members if item.classification_status == "conflict")
    assert conflict.member_key == f"conflict:system_bot:account:{bot.id}"
    assert conflict.account_id == bot.id
    assert conflict.telegram_user_id is None
    assert "telegram_identity_conflict" in conflict.quality_codes


@pytest.mark.asyncio
async def test_auditor_role_does_not_report_execution_gate_disabled(
    test_db,
    monkeypatch,
):
    asset, *_ = await _seed(test_db)
    monkeypatch.setattr(
        "app.modules.owned_group.operations_read_model.is_owned_group_governance_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        "app.modules.owned_group.operations_read_model.settings.OWNED_GROUP_MODULE_ENABLED",
        True,
    )
    monkeypatch.setattr(
        "app.modules.owned_group.operations_read_model.settings.OWNED_GROUP_EXECUTION_ENABLED",
        True,
    )

    operations = await _service(test_db).get_operations_center(asset.id, role="auditor")

    assert operations.sections.orchestration.can_execute is False
    assert "owned_group_execution_disabled" not in (
        operations.sections.orchestration.blocking_reasons
    )


def test_last_observed_sort_falls_back_to_source_created_at():
    older = {
        "member_key": "telegram:2",
        "last_observed_at": None,
        "last_verified_at": None,
        "joined_at": None,
        "_sort_created_at": datetime(2026, 1, 1),
    }
    newer = {
        "member_key": "telegram:1",
        "last_observed_at": None,
        "last_verified_at": None,
        "joined_at": None,
        "_sort_created_at": datetime(2026, 1, 2, tzinfo=UTC),
    }

    result = OwnedGroupOperationsReadModel._sort_members(
        [older, newer],
        "last_observed_desc",
    )

    assert [item["member_key"] for item in result] == ["telegram:1", "telegram:2"]


def test_last_observed_sort_uses_coalesce_field_priority_not_max_timestamp():
    observation_first = {
        "member_key": "telegram:1",
        "last_observed_at": datetime(2026, 1, 1, tzinfo=UTC),
        "last_verified_at": datetime(2026, 1, 10, tzinfo=UTC),
        "joined_at": None,
        "_sort_created_at": None,
    }
    newer_observation = {
        "member_key": "telegram:2",
        "last_observed_at": datetime(2026, 1, 2, tzinfo=UTC),
        "last_verified_at": None,
        "joined_at": None,
        "_sort_created_at": None,
    }

    result = OwnedGroupOperationsReadModel._sort_members(
        [observation_first, newer_observation],
        "last_observed_desc",
    )

    assert [item["member_key"] for item in result] == ["telegram:2", "telegram:1"]
