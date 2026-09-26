"""Rejoin rejection survives account switching, links and later review queues."""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.account.models import AccountStatus, TelegramAccount
from app.core.account.risk_guard import AccountRiskAction
from app.core.account.telegram_execution import TelegramExecutionError, TelegramExecutionService
from app.core.group.models import Group, GroupAccountMembership
from app.core.settings_models import SystemSetting
from app.modules.acquisition.group_qualification import POLICY_VERSION
from app.modules.acquisition.models import GroupQualificationAudit
from app.modules.acquisition.qualification_join_gate import qualification_join_gate, rejection_facts
from app.modules.acquisition.qualification_service import (
    SETTING_KEY,
    authoritative_rule_fingerprint,
)


def entity(identity=1234567890):
    return SimpleNamespace(
        id=identity, megagroup=True, broadcast=False, title="Group", username="changed_name"
    )


async def seed(db, decision="reject", reason="members_below_50"):
    now = datetime.utcnow() - timedelta(minutes=5)
    group = Group(id=40, group_id=-1001234567890, username="old_name", title="Group")
    member = GroupAccountMembership(
        id=60,
        account_id=2,
        group_id=40,
        telegram_group_id=group.group_id,
        status="left",
        review_status="left",
        ad_status="blocked",
        joined_at=now - timedelta(days=3),
    )
    payload = {
        "policy_version": POLICY_VERSION,
        "account_id": 2,
        "group_id": 40,
        "telegram_group_id": group.group_id,
        "checked_at": now.isoformat(),
        "decision": decision,
        "reason": reason,
        "member_count": 20,
        "member_count_verified": True,
        "valid_messages": 3,
        "evidence": [],
    }
    row = GroupQualificationAudit(
        id=80,
        batch_id="precise-qualification",
        membership_id=60,
        account_id=2,
        group_id=40,
        policy_version=POLICY_VERSION,
        content_scope="text_profile",
        state="completed",
        decision=decision,
        reason=reason,
        checked_at=now,
        expires_at=now + timedelta(hours=24),
        evidence_json=json.dumps(payload),
        evidence_hash="a" * 64,
    )
    setting = SystemSetting(key=SETTING_KEY, value=json.dumps({"enabled": True}))
    db.add_all(
        [
            TelegramAccount(
                id=2,
                identifier="join-gate-2",
                session_name="join-gate-2",
                status=AccountStatus.ONLINE,
            ),
            group,
            member,
            row,
            setting,
        ]
    )
    await db.commit()
    return group, member, row, setting


async def fresh_clearance(db, row, setting, **changes):
    old = rejection_facts(row)[0]
    now = datetime.utcnow()
    payload = json.loads(row.evidence_json)
    payload.update(
        decision="technical_wait",
        reason="membership_unconfirmed",
        checked_at=now.isoformat(),
        **changes,
    )
    review = GroupQualificationAudit(
        id=81,
        batch_id="read-only-condition-recheck",
        membership_id=60,
        account_id=2,
        group_id=40,
        policy_version=POLICY_VERSION,
        content_scope="text_profile",
        state="completed",
        decision="technical_wait",
        reason="membership_unconfirmed",
        checked_at=now,
        expires_at=now + timedelta(hours=24),
        evidence_json=json.dumps(payload),
        evidence_hash="b" * 64,
    )
    clearance = {
        "group_id": 40,
        "rejection_audit_id": old["audit_id"],
        "rejection_checked_at": old["checked_at"],
        "rejection_evidence_hash": old["evidence_hash"],
        "review_audit_id": review.id,
        "review_evidence_hash": review.evidence_hash,
        "operator_id": 9,
        "reviewed_at": now.isoformat(),
    }
    setting.value = json.dumps({"enabled": True, "rejoin_clearances": [clearance]})
    db.add(review)
    await db.commit()
    return review, clearance


class JoinClient:
    def __init__(self, preview=None, target=None):
        self.preview = preview
        self.target = target or entity()
        self.requests = []

    async def get_entity(self, username):
        return self.target

    async def __call__(self, request):
        name = type(request).__name__
        self.requests.append(name)
        if name == "CheckChatInviteRequest":
            return self.preview
        return SimpleNamespace(chats=[self.target])


def executor(db, client):
    guard = SimpleNamespace(
        db=db,
        check_and_reserve=AsyncMock(return_value=SimpleNamespace(allowed=True)),
        record_failure=AsyncMock(),
        record_success=AsyncMock(),
    )
    return TelegramExecutionService(guard), SimpleNamespace(account_id=3, client=client)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["auto", "public", "private"])
async def test_rejected_group_cannot_rejoin_via_other_account_or_renamed_link(test_db, route):
    await seed(test_db)
    client = JoinClient(preview=SimpleNamespace(chat=entity(), megagroup=True, broadcast=False))
    service, account = executor(test_db, client)
    attempted = []
    with pytest.raises(TelegramExecutionError, match="qualification_rejoin_requires"):
        if route == "auto":
            await service.join_group(
                account,
                SimpleNamespace(username="changed_name"),
                on_join_request_attempted=lambda: attempted.append(True),
            )
        else:
            await service.join_group_by_link(
                account,
                "https://t.me/changed_name"
                if route == "public"
                else "https://t.me/+PrivateInvite123",
                source="owned_group_resource_fake",
                on_join_request_attempted=lambda: attempted.append(True),
            )
    assert attempted == []
    assert not {"JoinChannelRequest", "ImportChatInviteRequest"}.intersection(client.requests)


@pytest.mark.asyncio
async def test_private_unknown_identity_pauses_before_import(test_db):
    await seed(test_db)
    client = JoinClient(preview=SimpleNamespace(megagroup=True, broadcast=False))
    service, account = executor(test_db, client)
    with pytest.raises(TelegramExecutionError, match="qualification_join_identity_unknown"):
        await service.join_group_by_link(account, "https://t.me/+PrivateInvite123")
    assert client.requests == ["CheckChatInviteRequest"]


@pytest.mark.asyncio
async def test_owned_resource_join_keeps_independent_action(test_db):
    await seed(test_db)
    client = JoinClient(preview=SimpleNamespace(megagroup=True, broadcast=False))
    service, account = executor(test_db, client)
    await service.join_group_by_link(
        account, "https://t.me/+PrivateInvite123", action=AccountRiskAction.OWNED_GROUP_JOIN
    )
    assert client.requests == ["CheckChatInviteRequest", "ImportChatInviteRequest"]


@pytest.mark.asyncio
async def test_missing_flag_pauses_ordinary_private_invites(test_db):
    client = JoinClient(preview=SimpleNamespace(megagroup=True, broadcast=False))
    service, account = executor(test_db, client)
    with pytest.raises(TelegramExecutionError, match="qualification_disabled"):
        await service.join_group_by_link(account, "https://t.me/+PrivateInvite123")
    assert "ImportChatInviteRequest" not in client.requests


@pytest.mark.asyncio
async def test_feature_disabled_pauses_ordinary_rejoin(test_db):
    _, _, _, setting = await seed(test_db)
    setting.value = json.dumps({"enabled": False})
    await test_db.commit()
    assert await qualification_join_gate(test_db, entity()) == "qualification_disabled"


@pytest.mark.asyncio
async def test_manual_left_without_qualification_rejection_is_not_blocked(test_db):
    await seed(test_db, decision="observe", reason="manual_read")
    assert await qualification_join_gate(test_db, entity()) is None
    assert await qualification_join_gate(test_db, entity(987654321)) is None


@pytest.mark.asyncio
async def test_later_queued_audit_does_not_erase_rejection(test_db):
    _, _, row, setting = await seed(test_db)
    review, _ = await fresh_clearance(test_db, row, setting, member_count=80)
    review.state = "queued"
    review.checked_at = None
    await test_db.commit()
    assert (
        await qualification_join_gate(test_db, entity())
        == "qualification_rejoin_requires_changed_evidence_clearance"
    )


@pytest.mark.asyncio
async def test_durable_exit_history_blocks_after_previous_checks_are_pruned(test_db):
    _, _, row, _ = await seed(test_db)
    facts = rejection_facts(row)
    facts[0]["exit_confirmed_at"] = datetime.utcnow().isoformat()
    row.decision = "observe"
    row.evidence_json = json.dumps({"qualification_exit_history": facts})
    await test_db.commit()
    assert (
        await qualification_join_gate(test_db, entity())
        == "qualification_rejoin_requires_changed_evidence_clearance"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason,changes",
    [
        ("members_below_50", {"member_count": 50, "member_count_verified": True}),
        ("messages_below_5_in_72h", {"valid_messages": 5}),
    ],
)
async def test_condition_improvement_and_explicit_clearance_permit_rejoin_without_send_approval(
    test_db, reason, changes
):
    _, _, row, setting = await seed(test_db, reason=reason)
    await fresh_clearance(test_db, row, setting, **changes)
    assert await qualification_join_gate(test_db, entity()) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption", ["hash", "time", "operator", "expired", "technical", "unchanged"]
)
async def test_clearance_requires_exact_current_evidence_and_actual_condition_change(
    test_db, corruption
):
    _, _, row, setting = await seed(test_db)
    review, clearance = await fresh_clearance(test_db, row, setting, member_count=80)
    if corruption == "hash":
        clearance["review_evidence_hash"] = "not-the-reviewed-evidence"
    elif corruption == "time":
        clearance["rejection_checked_at"] = datetime.utcnow().isoformat()
    elif corruption == "operator":
        clearance["operator_id"] = True
    elif corruption == "expired":
        review.expires_at = datetime.utcnow() - timedelta(seconds=1)
    else:
        data = json.loads(review.evidence_json)
        data.update(
            {"technical_errors": ["TimeoutError"]}
            if corruption == "technical"
            else {"member_count": 20}
        )
        review.evidence_json = json.dumps(data)
    setting.value = json.dumps({"enabled": True, "rejoin_clearances": [clearance]})
    await test_db.commit()
    assert (
        await qualification_join_gate(test_db, entity())
        == "qualification_rejoin_requires_changed_evidence_clearance"
    )


@pytest.mark.asyncio
async def test_group_rule_ban_is_not_cleared_by_another_account_or_changed_count(test_db):
    _, _, row, setting = await seed(test_db, reason="ads_forbidden")
    data = json.loads(row.evidence_json)
    data.update(
        group_level_advertising_ban=True, evidence=[{"source": "full_about", "text": "禁止广告"}]
    )
    row.evidence_json = json.dumps(data)
    await test_db.commit()
    await fresh_clearance(test_db, row, setting, member_count=80)
    assert (
        await qualification_join_gate(test_db, entity())
        == "qualification_group_rule_prohibits_rejoin"
    )


@pytest.mark.asyncio
async def test_changed_rules_and_existing_strict_ban_clearance_permit_rejoin(test_db):
    _, _, row, setting = await seed(test_db, reason="ads_forbidden")
    data = json.loads(row.evidence_json)
    data.update(
        group_level_advertising_ban=True, evidence=[{"source": "full_about", "text": "禁止广告"}]
    )
    row.evidence_json = json.dumps(data)
    await test_db.commit()
    old_rules = authoritative_rule_fingerprint(data)
    review, _ = await fresh_clearance(test_db, row, setting, member_count=80)
    current = json.loads(review.evidence_json)
    current.update(
        group_level_advertising_ban=False,
        reason="explicit_permission_verified",
        decision="allowed",
        evidence=[{"source": "full_about", "text": "允许普通成员广告"}],
    )
    review.evidence_json = json.dumps(current)
    review.decision = "allowed"
    review.reason = "explicit_permission_verified"
    setting.value = json.dumps(
        {
            "enabled": True,
            "group_ban_clearances": [
                {
                    "group_id": 40,
                    "ban_rule_fingerprint": old_rules,
                    "review_audit_id": review.id,
                    "review_evidence_hash": review.evidence_hash,
                    "operator_id": 9,
                    "reviewed_at": datetime.utcnow().isoformat(),
                }
            ],
        }
    )
    await test_db.commit()
    assert await qualification_join_gate(test_db, entity()) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_id", [9876543210, -1009876543210, -1234567890])
async def test_auto_join_rejects_stale_username_or_wrong_namespace(test_db, stored_id):
    test_db.add(SystemSetting(key=SETTING_KEY, value=json.dumps({"enabled": True})))
    await test_db.commit()
    client = JoinClient(target=entity())
    service, account = executor(test_db, client)
    attempted = []
    with pytest.raises(TelegramExecutionError, match="join_target_identity_changed"):
        await service.join_group(
            account,
            SimpleNamespace(username="changed_name", group_id=stored_id),
            on_join_request_attempted=lambda: attempted.append(True),
        )
    assert attempted == []
    assert client.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_id", [1234567890, -1001234567890])
async def test_auto_join_accepts_matching_legacy_or_marked_identity(test_db, stored_id):
    test_db.add(SystemSetting(key=SETTING_KEY, value=json.dumps({"enabled": True})))
    await test_db.commit()
    client = JoinClient(target=entity())
    service, account = executor(test_db, client)
    attempted = []
    await service.join_group(
        account,
        SimpleNamespace(username="changed_name", group_id=stored_id),
        on_join_request_attempted=lambda: attempted.append(True),
    )
    assert attempted == [True]
    assert client.requests == ["JoinChannelRequest"]


@pytest.mark.asyncio
async def test_gate_read_failure_never_sends_or_marks_request_attempted():
    db = SimpleNamespace(get=AsyncMock(side_effect=ConnectionError("database unavailable")))
    client = JoinClient()
    service, account = executor(db, client)
    attempted = []
    with pytest.raises(TelegramExecutionError, match="qualification_join_check_unavailable"):
        await service.join_group(
            account,
            SimpleNamespace(username="changed_name"),
            on_join_request_attempted=lambda: attempted.append(True),
        )
    assert client.requests == [] and attempted == []


@pytest.mark.asyncio
async def test_ban_in_durable_exit_snapshot_survives_missing_short_history(test_db):
    _, _, row, _ = await seed(test_db, reason="ads_forbidden")
    data = json.loads(row.evidence_json)
    data.update(
        group_level_advertising_ban=True, evidence=[{"source": "full_about", "text": "禁止广告"}]
    )
    row.evidence_json = json.dumps(data)
    facts = rejection_facts(row)
    row.decision = "observe"
    row.evidence_json = json.dumps({"qualification_exit_history": facts})
    await test_db.commit()
    assert (
        await qualification_join_gate(test_db, entity())
        == "qualification_group_rule_prohibits_rejoin"
    )


@pytest.mark.asyncio
async def test_already_joined_private_link_is_read_only_even_when_rejoin_blocked(test_db):
    from telethon.tl.types import ChatInviteAlready

    await seed(test_db)
    client = JoinClient(preview=ChatInviteAlready(chat=entity()))
    service, account = executor(test_db, client)
    await service.join_group_by_link(account, "https://t.me/+PrivateInvite123")
    assert client.requests == ["CheckChatInviteRequest"]


@pytest.mark.asyncio
@pytest.mark.parametrize("history_only", [False, True])
async def test_v1_rejection_and_durable_history_survive_policy_upgrade(test_db, history_only):
    _, _, row, _ = await seed(test_db)
    row.policy_version = "pp-ai-qualification-v1"
    payload = json.loads(row.evidence_json)
    payload["policy_version"] = row.policy_version
    row.evidence_json = json.dumps(payload)
    facts = rejection_facts(row)
    assert len(facts) == 1 and facts[0]["policy_version"] == "pp-ai-qualification-v1"
    if history_only:
        row.policy_version = POLICY_VERSION
        row.decision = "observe"
        row.evidence_json = json.dumps({"qualification_exit_history": facts})
    await test_db.commit()
    assert (
        await qualification_join_gate(test_db, entity())
        == "qualification_rejoin_requires_changed_evidence_clearance"
    )
