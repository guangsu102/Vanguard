from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.modules.owned_group.member_classifier import (
    MemberCandidate,
    PresenceFact,
    build_member_key,
    classify_member_candidates,
    merge_presence,
)


@pytest.fixture
def asset() -> dict[str, int]:
    return {"id": 12, "owner_account_id": 5}


@pytest.mark.parametrize("mode", ["growth", "ad_only"])
def test_promoter_modes_have_one_system_ad_classification(asset, mode):
    result = classify_member_candidates(
        [
            MemberCandidate(
                relation="owned_membership_user",
                source="owned_group_membership",
                source_id=31,
                account_id=5,
                telegram_user_id=778899001,
                account_type="promoter",
                operation_mode=mode,
                raw_presence_status="member_verified",
                last_verified_at=datetime.now(UTC),
                source_time=datetime.now(UTC),
            )
        ],
        asset,
    )

    assert len(result.members) == 1
    member = result.members[0]
    assert member["member_kind"] == "system_ad_account"
    assert member["operation_mode"] == mode
    assert member["classification_status"] == "resolved"


def test_bot_priority_absorbs_matching_observation_and_keeps_parent_as_detail(asset):
    now = datetime.now(UTC)
    result = classify_member_candidates(
        [
            MemberCandidate(
                relation="owned_membership_bot",
                source="owned_group_membership",
                source_id=41,
                account_id=22,
                telegram_user_id=9002,
                owned_bot_user_id=9002,
                account_type="guardian_bot",
                parent_account_id=5,
                raw_presence_status="admin_verified",
                last_verified_at=now - timedelta(minutes=1),
                source_time=now - timedelta(minutes=1),
            ),
            MemberCandidate(
                relation="observation",
                source="guardian_observation",
                source_id=99,
                telegram_user_id=9002,
                is_bot=True,
                raw_presence_status="present",
                last_observed_at=now,
                source_time=now,
            ),
        ],
        asset,
    )

    assert len(result.members) == 1
    member = result.members[0]
    assert member["member_key"] == "system_bot:account:22"
    assert member["member_kind"] == "system_bot"
    assert member["parent_account_id"] == 5
    assert member["presence_confidence"] == "observed"


def test_system_relation_wins_over_conflicting_observation_type(asset):
    result = classify_member_candidates(
        [
            MemberCandidate(
                relation="owned_membership_bot",
                source="owned_group_membership",
                source_id=10,
                account_id=20,
                account_type="guardian_bot",
                telegram_user_id=300,
                owned_bot_user_id=300,
            ),
            MemberCandidate(
                relation="observation",
                source="guardian_observation",
                source_id=11,
                telegram_user_id=300,
                is_bot=False,
                raw_presence_status="present",
            ),
        ],
        asset,
    )

    assert len(result.members) == 1
    assert result.members[0]["member_kind"] == "system_bot"
    assert result.members[0]["classification_status"] == "resolved"


def test_observation_is_required_for_real_user_and_user_profile_is_only_supplement(asset):
    now = datetime.now(UTC)
    result = classify_member_candidates(
        [
            MemberCandidate(
                relation="observation",
                source="guardian_observation",
                source_id=76,
                source_time=now,
                telegram_user_id=123456,
                is_bot=False,
                display_name_snapshot="Alice",
                username_snapshot="@alice",
                raw_presence_status="present",
                last_observed_at=now,
                user_id=7,
                user_state="active",
                warning_count=2,
                user_profile_source_id=7,
                user_profile_source_time=now,
            )
        ],
        asset,
    )

    member = result.members[0]
    assert member["member_kind"] == "real_user"
    assert member["username"] == "alice"
    assert member["risk_scope"] == "user_global"
    assert [source["source"] for source in member["sources"]] == [
        "guardian_observation",
        "user_profile",
    ]
    assert member["primary_source"] == "guardian_observation"


def test_observation_without_identity_type_is_unresolved_not_conflict(asset):
    result = classify_member_candidates(
        [
            MemberCandidate(
                relation="observation",
                source="guardian_observation",
                source_id=81,
                telegram_user_id=123457,
                is_bot=None,
                raw_presence_status="present",
            )
        ],
        asset,
    )

    member = result.members[0]
    assert member["member_kind"] is None
    assert member["classification_status"] == "unresolved"
    assert member["classification_reason"] == "identity_incomplete"
    assert member["quality_codes"] == ["identity_incomplete"]


def test_unmanaged_observed_bot_is_unresolved_not_real_user(asset):
    result = classify_member_candidates(
        [
            MemberCandidate(
                relation="observation",
                source="guardian_observation",
                source_id=2,
                source_time=datetime.now(UTC),
                telegram_user_id=999,
                is_bot=True,
                raw_presence_status="present",
            )
        ],
        asset,
    )

    member = result.members[0]
    assert member["member_kind"] is None
    assert member["classification_status"] == "unresolved"
    assert member["classification_reason"] == "third_party_bot_unmanaged"


def test_two_system_accounts_with_same_telegram_id_become_one_conflict(asset):
    candidates = [
        MemberCandidate(
            relation="owned_membership_user",
            source="owned_group_membership",
            source_id=source_id,
            account_id=account_id,
            telegram_user_id=77,
            account_type="promoter",
        )
        for source_id, account_id in [(1, 5), (2, 6)]
    ]
    result = classify_member_candidates(candidates, asset)

    assert len(result.members) == 1
    member = result.members[0]
    assert member["member_key"] == "conflict:telegram:77"
    assert member["classification_status"] == "conflict"
    assert member["member_kind"] is None


def test_existing_system_conflict_and_other_system_identity_are_merged(asset):
    result = classify_member_candidates(
        [
            MemberCandidate(
                relation="owned_membership_user",
                source="owned_group_membership",
                source_id=1,
                account_id=30,
                account_type="promoter",
                telegram_user_id=777,
            ),
            MemberCandidate(
                relation="group_membership",
                source="group_account_membership",
                source_id=30,
                account_id=30,
                account_type="promoter",
                telegram_user_id=778,
            ),
            MemberCandidate(
                relation="owned_membership_bot",
                source="owned_group_membership",
                source_id=2,
                account_id=31,
                account_type="guardian_bot",
                telegram_user_id=777,
                owned_bot_user_id=777,
            ),
        ],
        asset,
    )

    shared = [item for item in result.members if item["telegram_user_id"] == 777]
    assert len(shared) == 1
    assert shared[0]["member_key"] == "conflict:telegram:777"
    assert shared[0]["classification_status"] == "conflict"


def test_bot_three_source_identity_conflict_does_not_leak_observations_as_real_users(asset):
    candidates = [
        MemberCandidate(
            relation="owned_membership_bot",
            source="owned_group_membership",
            source_id=10,
            account_id=20,
            telegram_user_id=101,
            owned_bot_user_id=102,
            guardian_bot_user_id=103,
            account_type="guardian_bot",
        )
    ]
    candidates.extend(
        MemberCandidate(
            relation="observation",
            source="guardian_observation",
            source_id=identity,
            source_time=datetime.now(UTC),
            telegram_user_id=identity,
            is_bot=False,
            raw_presence_status="present",
        )
        for identity in (101, 102, 103)
    )

    result = classify_member_candidates(candidates, asset)

    assert len(result.members) == 1
    member = result.members[0]
    assert member["member_key"] == "conflict:system_bot:account:20"
    assert member["classification_status"] == "conflict"
    assert member["account_id"] == 20
    assert member["telegram_user_id"] is None
    assert not any(item["member_kind"] == "real_user" for item in result.members)
    assert len(member["sources"]) == 4


def test_presence_uses_newest_fact_and_observation_wins_equal_timestamp():
    now = datetime.now(UTC)
    result = merge_presence(
        [
            PresenceFact(
                source="owned_group_membership",
                raw_status="member_verified",
                observed_at=now,
                verified_at=now,
            ),
            PresenceFact(
                source="guardian_observation",
                raw_status="left",
                observed_at=now,
                left_at=now,
            ),
        ]
    )

    assert result.presence_status == "left"
    assert result.presence_confidence == "observed"
    assert result.quality_codes == ("presence_source_conflict",)


def test_presence_merge_normalizes_naive_and_aware_timestamps():
    result = merge_presence(
        [
            PresenceFact(
                source="owned_group_membership",
                raw_status="member_verified",
                observed_at=datetime(2026, 1, 2),
            ),
            PresenceFact(
                source="guardian_observation",
                raw_status="left",
                observed_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
        ]
    )

    assert result.presence_status == "present"
    assert result.presence_confidence == "verified"


def test_permissions_snapshot_input_is_ignored(asset):
    result = classify_member_candidates(
        [
            {
                "relation": "observation",
                "source": "guardian_observation",
                "source_id": 1,
                "telegram_user_id": 123,
                "is_bot": False,
                "raw_presence_status": "present",
                "source_time": datetime.now(UTC),
                "permissions_snapshot": '{"can_delete_messages": true}',
            }
        ],
        asset,
    )

    assert "permissions" not in result.members[0]
    assert "permissions_snapshot" not in result.members[0]


def test_stable_member_keys_do_not_use_profile_ids():
    assert (
        build_member_key(
            {"member_kind": "system_bot", "account_id": 44, "owned_bot_profile_id": 900}
        )
        == "system_bot:account:44"
    )
