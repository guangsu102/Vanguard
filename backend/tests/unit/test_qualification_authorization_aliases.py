"""Authorization must resolve this account's unique membership across legacy IDs."""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.account.models import AccountStatus, TelegramAccount
from app.core.group.models import Group, GroupAccountMembership
from app.core.settings_models import SystemSetting
from app.modules.acquisition import qualification_actions as actions
from app.modules.acquisition import qualification_service as service
from app.modules.acquisition.group_qualification import POLICY_VERSION
from app.modules.acquisition.models import GroupQualificationAudit
from app.modules.owned_group.models import OwnedGroupAsset

RAW = 1234567890
MARKED = -1_000_000_000_000 - RAW


async def accounts_and_policy(db, **settings):
    db.add_all([
        TelegramAccount(id=account_id, identifier=f"alias-account-{account_id}",
                        session_name=f"alias-account-{account_id}", status=AccountStatus.ONLINE,
                        is_active=True, risk_level="normal")
        for account_id in (2, 3)
    ])
    db.add(SystemSetting(key=service.SETTING_KEY, value=json.dumps({
        "enabled": True, "execute_exits": True, "account_ids": [2, 3], **settings,
    })))
    await db.commit()


def add_relation(db, group_id, telegram_id, membership_id=None, *, account_id=2,
                 status="joined", audit=True, username="AliasTarget", minutes=2):
    now = datetime.utcnow()
    group = Group(id=group_id, group_id=telegram_id, title="Alias target", username=username)
    db.add(group)
    if membership_id is None:
        return group, None, None
    member = GroupAccountMembership(id=membership_id, account_id=account_id, group_id=group.id,
        telegram_group_id=telegram_id, status=status, joined_at=now-timedelta(days=1),
        ad_status="blocked", review_status="exit_pending" if status == "joined" else "left",
        review_next_at=now-timedelta(minutes=minutes) if status == "joined" else None,
        leave_attempts=0)
    db.add(member)
    row = None
    if audit:
        row = GroupQualificationAudit(batch_id=f"alias-{membership_id}", membership_id=membership_id,
            account_id=account_id, group_id=group.id, policy_version=POLICY_VERSION,
            content_scope="text_profile", state="completed", decision="reject",
            reason="members_below_50", membership_joined_at=member.joined_at,
            checked_at=now, expires_at=now+timedelta(hours=24), evidence_json=json.dumps({
                "account_id": account_id, "group_id": group.id, "telegram_group_id": telegram_id,
                "policy_version": POLICY_VERSION, "content_scope": "text_profile",
                "decision": "reject", "quality_status": "reject", "evidence": [],
                "reason": "members_below_50", "member_count": 12,
                "member_count_verified": True, "permissions": {"member": True},
            }))
        db.add(row)
    return group, member, row


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [RAW, MARKED, "https://t.me/ALIASTARGET"])
async def test_other_accounts_alias_copy_cannot_shadow_own_membership(test_db, target):
    await accounts_and_policy(test_db)
    # Insert the other account first: group ordering must not decide authorization.
    add_relation(test_db, 40, RAW, 60, account_id=3)
    own_group, own_member, own_audit = add_relation(test_db, 41, MARKED, 61)
    await test_db.commit()
    result = await service.current_authorization(test_db, 2, target)
    assert isinstance(result, tuple) and len(result) == 3
    row, group, member = result
    assert row.id == own_audit.id and group.id == own_group.id and member.id == own_member.id
    assert member.account_id == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [RAW, MARKED, "@aliastarget"])
async def test_orphan_alias_group_cannot_hide_unique_owned_relation(test_db, target):
    await accounts_and_policy(test_db)
    add_relation(test_db, 40, RAW)
    own_group, own_member, own_audit = add_relation(test_db, 41, MARKED, 61)
    await test_db.commit()
    row, group, member = await service.current_authorization(test_db, 2, target)
    assert (row.id, group.id, member.id) == (own_audit.id, own_group.id, own_member.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("duplicate_status", ["joined", "left"])
async def test_multiple_own_alias_relations_are_ambiguous_even_with_left_history(test_db, duplicate_status):
    await accounts_and_policy(test_db)
    add_relation(test_db, 40, RAW, 60)
    add_relation(test_db, 41, MARKED, 61, status=duplicate_status)
    await test_db.commit()
    result = await service.current_authorization(test_db, 2, RAW)
    assert tuple(result) == (None, None, None)
    assert result.reason == "qualification_membership_ambiguous"


@pytest.mark.asyncio
async def test_group_without_own_membership_has_explicit_missing_reason(test_db):
    await accounts_and_policy(test_db)
    add_relation(test_db, 40, RAW, 60, account_id=3)
    await test_db.commit()
    result = await service.current_authorization(test_db, 2, RAW)
    assert tuple(result) == (None, None, None)
    assert result.reason == "qualification_membership_missing"


@pytest.mark.asyncio
async def test_membership_telegram_binding_must_match_resolved_group(test_db):
    await accounts_and_policy(test_db)
    _, member, _ = add_relation(test_db, 40, RAW, 60)
    member.telegram_group_id = 9876543210
    await test_db.commit()
    result = await service.current_authorization(test_db, 2, RAW)
    assert tuple(result) == (None, None, None)
    assert result.reason == "qualification_membership_identity_changed"


@pytest.mark.asyncio
async def test_missing_review_is_distinct_from_missing_membership(test_db):
    await accounts_and_policy(test_db)
    own_group, own_member, _ = add_relation(test_db, 40, RAW, 60, audit=False)
    await test_db.commit()
    result = await service.current_authorization(test_db, 2, RAW)
    row, group, member = result
    assert row is None and group.id == own_group.id and member.id == own_member.id
    assert result.reason == "qualification_review_required"


@pytest.mark.asyncio
async def test_alias_resolution_does_not_override_manual_protection(test_db):
    await accounts_and_policy(test_db, protected_membership_ids=[61])
    add_relation(test_db, 40, RAW, 60, account_id=3)
    group, member, _ = add_relation(test_db, 41, MARKED, 61)
    await test_db.commit()
    assert await service.authorize_leave(test_db, 2, group, member) == (
        False, "qualification_protected",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_kind,reason", [
    ("missing_review", "qualification_review_required"),
    ("ambiguous", "qualification_membership_ambiguous"),
])
async def test_unresolvable_queue_head_is_stopped_and_next_candidate_advances(test_db, monkeypatch, blocked_kind, reason):
    await accounts_and_policy(test_db, exit_membership_ids=[60, 62],
                              exit_reason_allowlist=["members_below_50"])
    _, blocked, _ = add_relation(test_db, 40, RAW, 60, audit=blocked_kind != "missing_review", minutes=10)
    if blocked_kind == "ambiguous":
        add_relation(test_db, 41, MARKED, 61, status="left")
    next_group, next_member, _ = add_relation(test_db, 42, 999888777, 62, username="NextTarget")
    await test_db.commit()
    monkeypatch.setattr(actions, "get_settings", lambda: SimpleNamespace(APP_ENV="test"))
    assess = AsyncMock(return_value=SimpleNamespace(verification_details={"qualification_decision": "reject"}))
    monkeypatch.setattr(actions, "assess", assess)
    leave = AsyncMock(return_value=None)
    actor = SimpleNamespace(db=test_db, _leave_group=leave,
                            _discovered_group_from_model=lambda group: group)
    result = await actions.run_exits(actor, limit=2)
    assert blocked.review_status == "exit_pending"
    assert blocked.ad_status == "blocked" and blocked.review_next_at > datetime.utcnow()
    assert blocked.leave_error == "exit_" + reason
    assert blocked.leave_attempts == 0 and blocked.leave_confirmed_at is None
    assert assess.await_count == 1 and assess.await_args.args[2].id == next_group.id
    leave.assert_awaited_once_with(2, next_group)
    assert next_member.status == "left" and next_member.leave_confirmed_at is not None
    assert result["processed"] >= 1
    # A later runner sees no due unresolved head and cannot repeat the successful leave.
    await actions.run_exits(actor, limit=2)
    assert assess.await_count == 1 and leave.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bound_id,target", [(-RAW, MARKED), (MARKED, -RAW)])
async def test_explicit_peer_namespace_does_not_match_other_kind_via_raw_copy(test_db, bound_id, target):
    await accounts_and_policy(test_db)
    _, member, _ = add_relation(test_db, 40, RAW, 60)
    member.telegram_group_id = bound_id
    await test_db.commit()
    result = await service.current_authorization(test_db, 2, target)
    assert tuple(result) == (None, None, None)
    assert result.reason == "qualification_membership_identity_changed"


@pytest.mark.asyncio
@pytest.mark.parametrize("orphan_id,member_id", [(RAW, MARKED), (MARKED, RAW)])
async def test_owned_asset_on_orphan_alias_protects_actual_membership(test_db, orphan_id, member_id):
    await accounts_and_policy(test_db)
    orphan, _, _ = add_relation(test_db, 40, orphan_id)
    group, member, _ = add_relation(test_db, 41, member_id, 61)
    # No direct Telegram ID on the asset: protection must follow core-group aliases.
    test_db.add(OwnedGroupAsset(internal_name="owned-alias", title="Owned alias", owner_account_id=3,
                               core_group_id=orphan.id, telegram_chat_id=None))
    await test_db.commit()
    _, resolved_group, resolved_member = await service.current_authorization(test_db, 2, member_id)
    assert (resolved_group.id, resolved_member.id) == (group.id, member.id)
    assert await service.authorize_leave(test_db, 2, group, member) == (
        False, "qualification_protected",
    )
