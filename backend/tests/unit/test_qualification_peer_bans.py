"""Cross-account bans follow a proven peer namespace, not a database core row."""

import hashlib
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from telethon.tl.types import ChatEmpty

from app.core.account.models import AccountStatus, TelegramAccount
from app.core.group.models import Group, GroupAccountMembership
from app.core.settings_models import SystemSetting
from app.modules.acquisition.group_qualification import POLICY_VERSION
from app.modules.acquisition.models import GroupQualificationAudit
from app.modules.acquisition.qualification_join_gate import qualification_join_gate
from app.modules.acquisition.qualification_service import (
    SETTING_KEY,
    authoritative_rule_fingerprint,
    confirmed_group_bans,
    send_gate,
)

RAW = 1234567890
MARKED = {"chat": -RAW, "channel": -1_000_000_000_000 - RAW}
KINDS = {"chat": "basic_group", "channel": "supergroup"}


def entity(namespace):
    if namespace == "chat":
        return ChatEmpty(RAW)
    return SimpleNamespace(id=RAW, megagroup=True)


def audit(db, core, account_id, membership_id, *, namespace, decision="reject"):
    now = datetime.utcnow() - timedelta(minutes=10 if decision == "reject" else 1)
    data = {
        "account_id": account_id,
        "group_id": core.id,
        "telegram_group_id": core.group_id,
        "raw_peer_id": RAW,
        "policy_version": POLICY_VERSION,
        "content_scope": "text_profile",
        "profile_fingerprint": hashlib.sha256(b"").hexdigest(),
        "checked_at": now.isoformat(),
        "decision": decision,
        "reason": "ads_forbidden" if decision == "reject" else "ordinary_advertisers_verified",
        "group_level_advertising_ban": decision == "reject",
        "evidence": [{"source": "full_about", "text": "No advertising"}]
        if decision == "reject"
        else [],
    }
    if namespace:
        data["group_type"] = KINDS[namespace]
    row = GroupQualificationAudit(
        batch_id=f"peer-{core.id}-{membership_id}",
        membership_id=membership_id,
        account_id=account_id,
        group_id=core.id,
        policy_version=POLICY_VERSION,
        content_scope="text_profile",
        state="completed",
        decision=decision,
        reason=data["reason"],
        checked_at=now,
        expires_at=now + timedelta(hours=24),
        evidence_hash="b" * 64,
        evidence_json=json.dumps(data),
    )
    db.add(row)
    return row


async def seed(db, *, own_id, own_namespace, banned_id, banned_namespace):
    now = datetime.utcnow()
    own = Group(id=40, group_id=own_id, username="current_peer", title="Current peer")
    other = Group(id=41, group_id=banned_id, title="Historic peer")
    member = GroupAccountMembership(
        id=60,
        group_id=own.id,
        account_id=2,
        telegram_group_id=own_id,
        status="joined",
        review_status="approved",
        ad_status="active",
        joined_at=now - timedelta(days=4),
    )
    db.add_all(
        [
            TelegramAccount(
                id=2,
                identifier="namespace-2",
                session_name="namespace-2",
                status=AccountStatus.ONLINE,
                is_active=True,
                risk_level="normal",
            ),
            TelegramAccount(
                id=3,
                identifier="namespace-3",
                session_name="namespace-3",
                status=AccountStatus.ONLINE,
                is_active=True,
                risk_level="normal",
            ),
            own,
            other,
            member,
            GroupAccountMembership(
                id=61,
                group_id=other.id,
                account_id=3,
                telegram_group_id=banned_id,
                status="left",
                review_status="left",
                ad_status="blocked",
            ),
            SystemSetting(
                key=SETTING_KEY,
                value=json.dumps(
                    {"enabled": True, "rollout_accounts": {"2": {"phase": "dynamic"}}, "system_account_user_ids": {"2": 200, "3": 300}}
                ),
            ),
            SystemSetting(key="automation.ad_delivery_execution", value='{"enabled":true}'),
        ]
    )
    current = audit(db, own, 2, 60, namespace=own_namespace, decision="trial")
    current.membership_joined_at = member.joined_at
    banned = audit(db, other, 3, 61, namespace=banned_namespace)
    await db.commit()
    from app.modules.acquisition.qualification_service import policy
    from app.modules.acquisition.qualification_system_identity import covered_system_ids

    _, fingerprint = await covered_system_ids(db, await policy(db), account_id=2)
    current_data = json.loads(current.evidence_json)
    current_data["system_identity_coverage"] = True
    current_data["system_identity_fingerprint"] = fingerprint
    current_data["system_user_ids"] = [200, 300]
    current_data["evidence"] = [
        {
            "source": "recent_promotional_message",
            "message_id": 101 + index,
            "sender_id": 501 + index,
            "sender_role": "ordinary",
            "text": f"Sale service contact https://seller-{index}.example",
            "age_hours": 30,
            "accessible": True,
            "warning_search_complete": True,
            "warning_reply_ids": [],
        }
        for index in range(2)
    ]
    current.evidence_json = json.dumps(current_data)
    await db.commit()
    return own, other, current, banned


@pytest.mark.asyncio
@pytest.mark.parametrize("namespace", ["chat", "channel"])
@pytest.mark.parametrize("raw_is_current", [True, False])
async def test_cross_account_core_alias_ban_blocks_send_and_rejoin(
    test_db, namespace, raw_is_current
):
    own, _, _, _ = await seed(
        test_db,
        own_id=RAW if raw_is_current else MARKED[namespace],
        own_namespace=namespace,
        banned_id=MARKED[namespace] if raw_is_current else RAW,
        banned_namespace=namespace,
    )
    assert await send_gate(test_db, 2, own.group_id, "Read my profile", None) == (
        "qualification_group_rule_prohibits"
    )
    assert await qualification_join_gate(test_db, entity(namespace)) == (
        "qualification_group_rule_prohibits_rejoin"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("history_kind", ["new_queue", "overwritten", "exit_snapshot"])
async def test_new_queue_and_pruned_history_cannot_replace_old_alias_ban(test_db, history_kind):
    own, other, _, banned = await seed(
        test_db,
        own_id=MARKED["channel"],
        own_namespace="channel",
        banned_id=RAW,
        banned_namespace="channel",
    )
    original = json.loads(banned.evidence_json)
    if history_kind == "new_queue":
        newer = audit(test_db, other, 3, 61, namespace="chat", decision="observe")
        newer.batch_id = "later-review"
        newer.state = "queued"
        newer.checked_at = None
        newer.evidence_json = "{}"
    else:
        banned.state = "queued"
        banned.checked_at = None
        banned.decision = "observe"
        # The latest raw-row payload claims another namespace. Historical facts
        # retain the namespace they were confirmed for and cannot be reassigned.
        data = {"telegram_group_id": RAW, "group_type": "basic_group"}
        if history_kind == "overwritten":
            data["confirmed_group_bans"] = confirmed_group_bans(original)
        else:
            data["qualification_exit_history"] = [{"snapshot": original}]
        banned.evidence_json = json.dumps(data)
    await test_db.commit()
    assert await send_gate(test_db, 2, own.group_id, "Read my profile", None) == (
        "qualification_group_rule_prohibits"
    )
    assert await qualification_join_gate(test_db, entity("channel")) == (
        "qualification_group_rule_prohibits_rejoin"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("namespace", ["chat", "channel"])
@pytest.mark.parametrize("raw_side", ["current", "historic", "neither"])
async def test_identical_raw_numbers_in_different_namespaces_do_not_share_bans(
    test_db, namespace, raw_side
):
    other_namespace = "chat" if namespace == "channel" else "channel"
    own, _, _, _ = await seed(
        test_db,
        own_id=RAW if raw_side == "current" else MARKED[namespace],
        own_namespace=namespace,
        banned_id=RAW if raw_side == "historic" else MARKED[other_namespace],
        banned_namespace=other_namespace,
    )
    assert await send_gate(test_db, 2, own.group_id, "Read my profile", None) is None
    assert await qualification_join_gate(test_db, entity(namespace)) is None


@pytest.mark.asyncio
async def test_legacy_raw_ban_without_namespace_is_unknown_not_inherited_or_ignored(test_db):
    own, _, _, _ = await seed(
        test_db,
        own_id=MARKED["channel"],
        own_namespace="channel",
        banned_id=RAW,
        banned_namespace=None,
    )
    assert await send_gate(test_db, 2, own.group_id, "Read my profile", None) == (
        "qualification_group_identity_unknown"
    )
    assert await qualification_join_gate(test_db, entity("channel")) == (
        "qualification_join_identity_unknown"
    )


@pytest.mark.asyncio
async def test_unknown_current_raw_namespace_with_two_marked_peers_pauses(test_db):
    own, _, _, _ = await seed(
        test_db,
        own_id=RAW,
        own_namespace=None,
        banned_id=MARKED["channel"],
        banned_namespace="channel",
    )
    test_db.add(Group(id=42, group_id=MARKED["chat"], title="Unrelated basic group"))
    await test_db.commit()
    assert await send_gate(test_db, 2, own.group_id, "Read my profile", None) == (
        "qualification_group_identity_unknown"
    )
    assert await qualification_join_gate(test_db, SimpleNamespace(id=RAW)) == (
        "qualification_join_identity_unknown"
    )


@pytest.mark.asyncio
async def test_contradictory_historical_namespace_pauses_instead_of_clearing(test_db):
    own, _, _, _ = await seed(
        test_db,
        own_id=RAW,
        own_namespace="channel",
        banned_id=MARKED["channel"],
        banned_namespace="chat",
    )
    assert await send_gate(test_db, 2, own.group_id, "Read my profile", None) == (
        "qualification_group_identity_unknown"
    )
    assert await qualification_join_gate(test_db, entity("channel")) == (
        "qualification_join_identity_unknown"
    )


def test_same_rule_text_keeps_separate_namespace_ban_facts_after_overwrite():
    now = datetime.utcnow()
    first = {
        "telegram_group_id": RAW,
        "raw_peer_id": RAW,
        "group_type": "basic_group",
        "group_level_advertising_ban": True,
        "checked_at": now.isoformat(),
        "evidence": [{"source": "full_about", "text": "No advertising"}],
    }
    later = {
        **first,
        "group_type": "supergroup",
        "checked_at": (now + timedelta(hours=1)).isoformat(),
        "confirmed_group_bans": confirmed_group_bans(first),
    }
    facts = confirmed_group_bans(later)
    assert len(facts) == 2
    assert {fact["group_type"] for fact in facts} == {"basic_group", "supergroup"}
    assert confirmed_group_bans({"confirmed_group_bans": facts}) == facts


@pytest.mark.asyncio
async def test_explicit_changed_rules_clearance_may_cross_only_proven_aliases(test_db):
    own, _, current, banned = await seed(
        test_db,
        own_id=MARKED["channel"],
        own_namespace="channel",
        banned_id=RAW,
        banned_namespace="channel",
    )
    current.decision = "allowed"
    current.reason = "explicit_permission_verified"
    data = json.loads(current.evidence_json)
    data.update(
        decision=current.decision,
        reason=current.reason,
        evidence=[{"source": "full_about", "text": "Ordinary members may advertise"}, *data["evidence"]],
    )
    current.evidence_json = json.dumps(data)
    setting = await test_db.get(SystemSetting, SETTING_KEY)
    setting.value = json.dumps(
        {
            "enabled": True,
            "rollout_accounts": {"2": {"phase": "dynamic"}},
            "system_account_user_ids": {"2": 200, "3": 300},
            "group_ban_clearances": [
                {
                    "group_id": own.id,
                    "ban_rule_fingerprint": authoritative_rule_fingerprint(
                        json.loads(banned.evidence_json)
                    ),
                    "review_audit_id": current.id,
                    "review_evidence_hash": current.evidence_hash,
                    "operator_id": 9,
                    "reviewed_at": datetime.utcnow().isoformat(),
                }
            ],
        }
    )
    await test_db.commit()
    assert await send_gate(test_db, 2, own.group_id, "Read my profile", None) is None
    assert await qualification_join_gate(test_db, entity("channel")) is None


@pytest.mark.asyncio
async def test_different_namespace_review_cannot_clear_channel_ban(test_db):
    own, _, _, banned = await seed(
        test_db,
        own_id=MARKED["channel"],
        own_namespace="channel",
        banned_id=RAW,
        banned_namespace="channel",
    )
    unrelated = Group(id=42, group_id=MARKED["chat"], title="Unrelated basic group")
    test_db.add(unrelated)
    review = audit(test_db, unrelated, 3, 62, namespace="chat", decision="allowed")
    data = json.loads(review.evidence_json)
    data.update(
        reason="explicit_permission_verified",
        evidence=[{"source": "full_about", "text": "Ordinary members may advertise"}],
    )
    review.evidence_json = json.dumps(data)
    await test_db.flush()
    setting = await test_db.get(SystemSetting, SETTING_KEY)
    setting.value = json.dumps(
        {
            "enabled": True,
            "rollout_accounts": {"2": {"phase": "dynamic"}},
            "group_ban_clearances": [
                {
                    "group_id": unrelated.id,
                    "ban_rule_fingerprint": authoritative_rule_fingerprint(
                        json.loads(banned.evidence_json)
                    ),
                    "review_audit_id": review.id,
                    "review_evidence_hash": review.evidence_hash,
                    "operator_id": 9,
                    "reviewed_at": datetime.utcnow().isoformat(),
                }
            ],
        }
    )
    await test_db.commit()
    assert await send_gate(test_db, 2, own.group_id, "Read my profile", None) == (
        "qualification_group_rule_prohibits"
    )
    assert await qualification_join_gate(test_db, entity("channel")) == (
        "qualification_group_rule_prohibits_rejoin"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "historic_namespace,expected",
    [
        ("chat", None),
        (None, "qualification_join_identity_unknown"),
    ],
)
async def test_non_rule_rejection_also_requires_matching_namespace(
    test_db, historic_namespace, expected
):
    _, _, _, rejected = await seed(
        test_db,
        own_id=MARKED["channel"],
        own_namespace="channel",
        banned_id=RAW,
        banned_namespace=historic_namespace,
    )
    data = json.loads(rejected.evidence_json)
    data.update(group_level_advertising_ban=False, reason="members_below_50", member_count=20)
    rejected.evidence_json = json.dumps(data)
    rejected.reason = "members_below_50"
    await test_db.commit()
    assert await qualification_join_gate(test_db, entity("channel")) == expected


@pytest.mark.asyncio
async def test_explicit_target_namespace_cannot_override_raw_core_snapshot(test_db):
    own, _, _, _ = await seed(
        test_db,
        own_id=RAW,
        own_namespace="channel",
        banned_id=MARKED["chat"],
        banned_namespace="chat",
    )
    # Account 2 only belongs to the raw core row, whose evidence proves Channel.
    # An explicit basic Chat target may not borrow that channel approval.
    assert await send_gate(test_db, 2, MARKED["chat"], "Read my profile", None) == (
        "qualification_group_identity_unknown"
    )
