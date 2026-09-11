from __future__ import annotations

import json

import pytest

from app.core.group.models import Group
from app.core.user.models import User
from app.modules.guardian.id_audit import audit_guardian_group_ids
from app.modules.guardian.models import (
    GroupModerationPolicy,
    GroupPunishmentPolicy,
    GroupVerificationConfig,
    ManagedGroupBinding,
    ManagedGroupBindingStatus,
    ModerationRule,
    ModerationSensitiveKeyword,
    RuleType,
    Violation,
    ViolationAction,
    ViolationLevel,
    Whitelist,
)
from app.modules.owned_group.models import OwnedGroupAsset


def _reason_codes(report) -> set[str]:
    return {finding.reason_code for finding in report.findings}


@pytest.mark.asyncio
async def test_clean_core_id_mapping_has_no_findings(test_db):
    group = Group(group_id=-10001001, title="clean")
    user = User(telegram_id=90001001, username="clean-user")
    test_db.add_all([group, user])
    await test_db.flush()
    binding = ManagedGroupBinding(
        group_id=group.id,
        telegram_group_id=group.group_id,
        bot_account_id=101,
        binding_status=ManagedGroupBindingStatus.ACTIVE,
    )
    test_db.add(binding)
    await test_db.flush()
    test_db.add(
        OwnedGroupAsset(
            internal_name="clean",
            title="clean",
            owner_account_id=201,
            status="ready",
            telegram_chat_id=group.group_id,
            core_group_id=group.id,
            managed_binding_id=binding.id,
            guardian_bot_account_id=binding.bot_account_id,
            governance_status="managed",
        )
    )
    test_db.add_all(
        [
            GroupVerificationConfig(group_id=group.id),
            GroupModerationPolicy(group_id=group.id),
            GroupPunishmentPolicy(group_id=group.id),
            ModerationRule(
                group_id=group.id,
                rule_type=RuleType.KEYWORD,
                pattern="safe-test",
            ),
            Whitelist(group_id=group.id, whitelist_type="domain", value="example.test"),
            ModerationSensitiveKeyword(
                group_id=group.id,
                text="test",
                normalized_text="test",
                level=ViolationLevel.LOW,
            ),
            Violation(
                user_id=user.id,
                group_id=group.id,
                rule_type=RuleType.KEYWORD.value,
                action_taken=ViolationAction.WARN,
                content="clean violation content",
            ),
        ]
    )
    await test_db.flush()

    report = await audit_guardian_group_ids(test_db)

    assert report.ok is True
    assert report.exit_code == 0
    assert report.findings == ()
    assert report.scanned_counts["moderation_rule"] == 1
    assert report.scanned_counts["violation"] == 1


@pytest.mark.asyncio
async def test_binding_duplicates_multi_bot_and_core_mismatch_are_reported(test_db):
    first_group = Group(group_id=-10002001, title="first")
    second_group = Group(group_id=-10002002, title="second")
    test_db.add_all([first_group, second_group])
    await test_db.flush()
    test_db.add_all(
        [
            ManagedGroupBinding(
                group_id=first_group.id,
                telegram_group_id=first_group.group_id,
                bot_account_id=301,
            ),
            ManagedGroupBinding(
                group_id=second_group.id,
                telegram_group_id=first_group.group_id,
                bot_account_id=302,
            ),
        ]
    )
    await test_db.flush()

    report = await audit_guardian_group_ids(test_db)

    assert {
        "managed_binding_duplicate_telegram_chat",
        "managed_binding_multi_bot_for_telegram_chat",
        "managed_binding_core_telegram_mismatch",
    }.issubset(_reason_codes(report))
    assert report.exit_code == 1


@pytest.mark.asyncio
async def test_owned_asset_bridge_mismatches_are_reported(test_db):
    asset_group = Group(group_id=-10003001, title="asset")
    binding_group = Group(group_id=-10003002, title="binding")
    test_db.add_all([asset_group, binding_group])
    await test_db.flush()
    binding = ManagedGroupBinding(
        group_id=binding_group.id,
        telegram_group_id=binding_group.group_id,
        bot_account_id=401,
    )
    test_db.add(binding)
    await test_db.flush()
    asset = OwnedGroupAsset(
        internal_name="broken-bridge",
        title="broken-bridge",
        owner_account_id=501,
        status="ready",
        telegram_chat_id=-10003999,
        core_group_id=asset_group.id,
        managed_binding_id=binding.id,
        guardian_bot_account_id=402,
        governance_status="managed",
    )
    test_db.add(asset)
    await test_db.flush()

    report = await audit_guardian_group_ids(test_db)

    assert {
        "owned_asset_core_group_chat_mismatch",
        "owned_asset_binding_core_group_mismatch",
        "owned_asset_binding_chat_mismatch",
        "owned_asset_binding_bot_mismatch",
    }.issubset(_reason_codes(report))
    finding = next(
        item for item in report.findings if item.reason_code == "owned_asset_binding_bot_mismatch"
    )
    assert finding.record_ids == (asset.id,)
    assert finding.related_record_ids == (binding.id,)


@pytest.mark.asyncio
async def test_policy_legacy_telegram_and_missing_core_ids_are_reported(test_db):
    group = Group(group_id=-10004001, title="policy")
    test_db.add(group)
    await test_db.flush()
    test_db.add_all(
        [
            GroupVerificationConfig(group_id=group.group_id),
            GroupModerationPolicy(group_id=987001),
            GroupPunishmentPolicy(group_id=987002),
            ModerationRule(
                group_id=group.group_id,
                rule_type=RuleType.KEYWORD,
                pattern="legacy",
            ),
            Whitelist(group_id=987003, whitelist_type="domain", value="legacy.test"),
            ModerationSensitiveKeyword(
                group_id=group.group_id,
                text="legacy",
                normalized_text="legacy",
            ),
        ]
    )
    await test_db.flush()

    report = await audit_guardian_group_ids(test_db)

    assert report.finding_counts["policy_group_id_legacy_telegram"] == 3
    assert report.finding_counts["policy_group_id_missing_core_group"] == 3
    assert report.scanned_counts["group_verification_config"] == 1
    assert report.scanned_counts["moderation_sensitive_keyword"] == 1


@pytest.mark.asyncio
async def test_violation_group_ids_report_legacy_orphan_and_ambiguous_rows(test_db):
    core_group = Group(group_id=-10004501, title="violation-core")
    user = User(telegram_id=90004501, username="violation-user")
    test_db.add_all([core_group, user])
    await test_db.flush()

    telegram_alias_group = Group(
        group_id=core_group.id,
        title="telegram-id-collides-with-core-id",
    )
    test_db.add(telegram_alias_group)
    await test_db.flush()

    legacy = Violation(
        user_id=user.id,
        group_id=core_group.group_id,
        rule_type=RuleType.KEYWORD.value,
        action_taken=ViolationAction.WARN,
        content="legacy-secret-content",
    )
    orphan = Violation(
        user_id=user.id,
        group_id=987004,
        rule_type=RuleType.DOMAIN.value,
        action_taken=ViolationAction.MUTE,
        content="orphan-secret-content",
    )
    ambiguous = Violation(
        user_id=user.id,
        group_id=core_group.id,
        rule_type=RuleType.FREQUENCY.value,
        action_taken=ViolationAction.BAN,
        content="ambiguous-secret-content",
    )
    test_db.add_all([legacy, orphan, ambiguous])
    await test_db.flush()

    report = await audit_guardian_group_ids(test_db)

    violation_findings = {
        finding.reason_code: finding
        for finding in report.findings
        if finding.entity == "violation"
    }
    assert violation_findings[
        "violation_group_id_legacy_telegram"
    ].record_ids == (legacy.id,)
    assert violation_findings[
        "violation_group_id_missing_core_group"
    ].record_ids == (orphan.id,)
    assert violation_findings[
        "violation_group_id_ambiguous_core_or_telegram"
    ].record_ids == (ambiguous.id,)
    assert report.scanned_counts["violation"] == 3

    payload_text = report.to_json()
    assert str(core_group.group_id) not in payload_text
    assert "secret-content" not in payload_text


@pytest.mark.asyncio
async def test_report_json_is_stable_and_secret_free(test_db):
    group = Group(group_id=-10005001, title="do-not-emit")
    test_db.add(group)
    await test_db.flush()
    test_db.add(GroupModerationPolicy(group_id=group.group_id))
    await test_db.flush()

    report = await audit_guardian_group_ids(test_db)
    payload_text = report.to_json()
    payload = json.loads(payload_text)

    assert payload["ok"] is False
    assert payload["total_findings"] == 1
    assert "-10005001" not in payload_text
    assert "token" not in payload_text.lower()
    assert "session" not in payload_text.lower()
    assert "proxy" not in payload_text.lower()
    assert "invite" not in payload_text.lower()
