from __future__ import annotations

import pytest
from sqlalchemy import CheckConstraint
from sqlalchemy.exc import IntegrityError

from app.core.account.models import AccountType, TelegramAccount
from app.modules.owned_group.models import OwnedGroupAsset


def test_owned_group_governance_schema_has_expected_links_and_constraints():
    table = OwnedGroupAsset.__table__
    expected_columns = {
        "core_group_id",
        "managed_binding_id",
        "guardian_bot_account_id",
        "governance_status",
        "governance_pending_at",
        "governance_enabled_at",
        "governance_last_checked_at",
        "governance_last_error_code",
        "governance_last_error_message",
    }
    assert expected_columns <= set(table.columns.keys())

    expected_foreign_keys = {
        "core_group_id": ("group.id", "fk_owned_group_assets_core_group"),
        "managed_binding_id": (
            "managed_group_binding.id",
            "fk_owned_group_assets_managed_binding",
        ),
        "guardian_bot_account_id": (
            "telegram_account.id",
            "fk_owned_group_assets_guardian_bot_account",
        ),
    }
    for column_name, (target, constraint_name) in expected_foreign_keys.items():
        foreign_key = next(iter(table.columns[column_name].foreign_keys))
        assert foreign_key.target_fullname == target
        assert foreign_key.ondelete == "SET NULL"
        assert foreign_key.constraint.name == constraint_name

    indexes = {index.name: index for index in table.indexes}
    assert indexes["idx_owned_group_assets_core_group"].unique is False
    assert indexes["uq_owned_group_assets_managed_binding"].unique is True
    assert [
        column.name for column in indexes["idx_owned_group_assets_guardian_status"].columns
    ] == [
        "guardian_bot_account_id",
        "governance_status",
    ]

    status_constraint = next(
        constraint
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
        and constraint.name == "ck_owned_group_assets_governance_status"
    )
    assert {"disabled", "pending", "managed", "degraded"} <= {
        value.strip(" '")
        for value in str(status_constraint.sqltext).split("(", 1)[1].rstrip(")").split(",")
    }

    for relationship_name in (
        "core_group",
        "managed_binding",
        "guardian_bot_account",
    ):
        assert relationship_name in OwnedGroupAsset.__dict__


@pytest.mark.asyncio
async def test_owned_group_governance_defaults_to_disabled(test_db):
    owner = TelegramAccount(
        identifier="governance-model-owner",
        session_name="governance-model-owner",
        account_type=AccountType.PROMOTER,
    )
    test_db.add(owner)
    await test_db.flush()

    asset = OwnedGroupAsset(
        internal_name="governance-default",
        title="Governance Default",
        owner_account_id=owner.id,
    )
    test_db.add(asset)
    await test_db.flush()

    assert asset.governance_status == "disabled"
    assert asset.core_group_id is None
    assert asset.managed_binding_id is None
    assert asset.guardian_bot_account_id is None
    assert asset.governance_pending_at is None
    assert asset.governance_enabled_at is None
    assert asset.governance_last_checked_at is None
    assert asset.governance_last_error_code is None
    assert asset.governance_last_error_message is None


@pytest.mark.asyncio
async def test_owned_group_governance_status_check_rejects_unknown_value(test_db):
    owner = TelegramAccount(
        identifier="governance-model-invalid-owner",
        session_name="governance-model-invalid-owner",
        account_type=AccountType.PROMOTER,
    )
    test_db.add(owner)
    await test_db.flush()
    test_db.add(
        OwnedGroupAsset(
            internal_name="governance-invalid",
            title="Governance Invalid",
            owner_account_id=owner.id,
            governance_status="unknown",
        )
    )

    with pytest.raises(IntegrityError):
        await test_db.flush()
    await test_db.rollback()
