"""Precisely scoped exits remain bounded after settings or audit decisions change."""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update

from app.core.account.models import AccountStatus, TelegramAccount
from app.core.group.models import Group, GroupAccountMembership
from app.core.settings_models import SystemSetting
from app.modules.acquisition import qualification_actions as actions
from app.modules.acquisition import qualification_service as service
from app.modules.acquisition.group_qualification import POLICY_VERSION
from app.modules.acquisition.models import GroupQualificationAudit


async def seed(db, **scope):
    now = datetime.utcnow()
    account = TelegramAccount(id=2, identifier="exit-scope-2", session_name="exit-scope-2",
                              status=AccountStatus.ONLINE, is_active=True, risk_level="normal")
    group = Group(id=40, group_id=1234567890, username="ExitScope", title="Exit scope")
    member = GroupAccountMembership(id=60, group_id=40, telegram_group_id=group.group_id,
                                    account_id=2, status="joined", joined_at=now-timedelta(days=2),
                                    review_status="exit_pending", ad_status="blocked",
                                    review_next_at=now-timedelta(minutes=1))
    row = GroupQualificationAudit(batch_id="exit-scope", membership_id=60, account_id=2,
        group_id=40, policy_version=POLICY_VERSION, content_scope="text_profile", state="completed",
        decision="reject", reason="members_below_50", membership_joined_at=member.joined_at,
        checked_at=now, expires_at=now+timedelta(hours=24), evidence_json=json.dumps({
            "decision":"reject", "quality_status":"reject", "evidence":[], "account_id":2,
            "group_id":40, "telegram_group_id":group.group_id, "policy_version":POLICY_VERSION,
            "content_scope":"text_profile", "coverage":[{"complete":True},{"complete":True}],
            "ordinary_advertising_last_48h_count":0, "rules_incomplete":False,
            "collected_at":now.isoformat(), "permissions":{"member":True},
            "technical_errors":[], "unknowns":[], "exit_group_rule_ban":True,
            "group_level_advertising_ban":True,
            "advertising_audit":{"ad_allowed":False,"policy_mode":"forbidden"},
            "member_count_verified":True,"member_count":49,"online_count":1,
            "online_count_source":"full_chat.online_count"}))
    setting = SystemSetting(key=service.SETTING_KEY, value=json.dumps({
        "enabled":True, "execute_exits":True, "account_ids":[2,3], **scope}))
    db.add_all([account, group, member, row, setting])
    await db.commit()
    return group, member, row, setting


@pytest.mark.asyncio
@pytest.mark.parametrize("scope,expected", [
    ({}, True),
    ({"exit_membership_ids":[60], "exit_reason_allowlist":["members_below_50"]}, True),
    ({"exit_membership_ids":[60]}, True),
    ({"exit_reason_allowlist":["members_below_50"]}, True),
    ({"exit_membership_ids":[]}, False),
    ({"exit_reason_allowlist":[]}, False),
    ({"exit_membership_ids":[61]}, False),
    ({"exit_reason_allowlist":["messages_below_5_in_72h"]}, False),
])
async def test_exit_authorization_requires_configured_membership_and_reason(test_db, scope, expected):
    group, member, _, _ = await seed(test_db, **scope)
    assert (await service.authorize_leave(test_db, 2, group, member))[0] is expected


@pytest.mark.parametrize("scope", [
    {"exit_membership_ids":None}, {"exit_membership_ids":"60"}, {"exit_membership_ids":[True]},
    {"exit_membership_ids":[0]}, {"exit_membership_ids":[-1]}, {"exit_membership_ids":["60"]},
    {"exit_membership_ids":[60, None]}, {"exit_reason_allowlist":"members_below_50"},
    {"exit_reason_allowlist":None}, {"exit_reason_allowlist":[""]},
    {"exit_reason_allowlist":[" "]}, {"exit_reason_allowlist":[" members_below_50"]},
    {"exit_reason_allowlist":[1]},
])
def test_malformed_scope_cannot_silently_expand_authorization(scope):
    with pytest.raises(ValueError, match="qualification_exit_scope_invalid"):
        service.exit_scope_filters(scope)


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", [{"exit_membership_ids":[]}, {"exit_reason_allowlist":[]},
                                   {"exit_membership_ids":None}, {"exit_reason_allowlist":[""]}])
async def test_disabled_scope_stops_runner_before_member_queries(monkeypatch, scope):
    monkeypatch.setattr(actions, "policy", AsyncMock(return_value={
        "enabled":True, "execute_exits":True, "account_ids":[2], **scope}))
    db = SimpleNamespace(scalars=AsyncMock())
    actor = SimpleNamespace(db=db, _leave_group=AsyncMock())
    result = await actions.run_exits(actor)
    assert result["processed"] == 0
    db.scalars.assert_not_awaited()
    actor._leave_group.assert_not_awaited()


@pytest.mark.asyncio
async def test_runner_selects_only_exact_membership_ids(test_db, monkeypatch):
    monkeypatch.setattr(actions, "get_settings", lambda: SimpleNamespace(APP_ENV="test"))
    group, selected, _, _ = await seed(test_db, exit_membership_ids=[60],
                                    exit_reason_allowlist=["members_below_50"])
    excluded = GroupAccountMembership(id=61, group_id=group.id, telegram_group_id=group.group_id,
        account_id=3, status="joined", review_status="exit_pending", ad_status="blocked",
        joined_at=datetime.utcnow()-timedelta(days=2), review_next_at=datetime.utcnow()-timedelta(hours=1))
    test_db.add(excluded)
    await test_db.commit()
    assess = AsyncMock(return_value=SimpleNamespace(verification_details={"qualification_decision":"observe"}))
    monkeypatch.setattr(actions, "assess", assess)
    actor = SimpleNamespace(db=test_db, _leave_group=AsyncMock())
    await actions.run_exits(actor, limit=10)
    assert assess.await_count == 1
    assert assess.await_args.args[1] == 2
    assert selected.review_status == "review_2h"
    assert excluded.review_status == "exit_pending"
    actor._leave_group.assert_not_awaited()


@pytest.mark.asyncio
async def test_exit_refreshes_settings_even_when_old_setting_is_cached(test_db):
    group, member, _, cached = await seed(test_db, exit_membership_ids=[60])
    assert json.loads(cached.value)["exit_membership_ids"] == [60]
    await test_db.execute(update(SystemSetting).where(SystemSetting.key == service.SETTING_KEY)
        .values(value=json.dumps({"enabled":True, "execute_exits":True, "account_ids":[2],
                                  "exit_membership_ids":[]}))
        .execution_options(synchronize_session=False))
    assert json.loads(cached.value)["exit_membership_ids"] == [60]
    allowed, reason = await service.authorize_leave(test_db, 2, group, member)
    assert not allowed and reason == "qualification_membership_outside_exit_scope"
    assert json.loads(cached.value)["exit_membership_ids"] == []


@pytest.mark.asyncio
async def test_exit_refreshes_latest_audit_reason_before_whitelist_decision(test_db):
    group, member, row, _ = await seed(test_db, exit_membership_ids=[60],
                                    exit_reason_allowlist=["members_below_50", "messages_below_5_in_72h"])
    await test_db.execute(update(GroupQualificationAudit).where(GroupQualificationAudit.id == row.id)
        .values(reason="account_permanent_send_restriction")
        .execution_options(synchronize_session=False))
    assert row.reason == "members_below_50"
    allowed, reason = await service.authorize_leave(test_db, 2, group, member)
    assert not allowed and reason == "qualification_exit_reason_outside_configured_scope"
    assert row.reason == "account_permanent_send_restriction"


@pytest.mark.asyncio
async def test_scope_does_not_override_manual_protection(test_db):
    group, member, _, _ = await seed(test_db, exit_membership_ids=[60],
        exit_reason_allowlist=["members_below_50"], protected_membership_ids=[60])
    assert await service.authorize_leave(test_db, 2, group, member) == (False, "qualification_protected")


@pytest.mark.asyncio
async def test_all_account_exit_scope_overrides_legacy_review_account_list(test_db):
    group, member, _, setting = await seed(test_db)
    setting.value = json.dumps({
        "enabled": True, "execute_exits": True,
        "account_ids": [3], "exit_all_accounts": True,
    })
    await test_db.commit()
    assert (await service.authorize_leave(test_db, 2, group, member))[0] is True
    assert json.loads(setting.value)["account_ids"] == [3]


@pytest.mark.asyncio
async def test_all_account_exit_runner_selects_member_outside_legacy_list(test_db, monkeypatch):
    monkeypatch.setattr(actions, "get_settings", lambda: SimpleNamespace(APP_ENV="test"))
    group, member, _, setting = await seed(test_db)
    setting.value = json.dumps({
        "enabled": True, "execute_exits": True,
        "account_ids": [3], "exit_all_accounts": True,
    })
    await test_db.commit()
    assess = AsyncMock(return_value=SimpleNamespace(verification_details={"qualification_decision": "observe"}))
    monkeypatch.setattr(actions, "assess", assess)
    actor = SimpleNamespace(db=test_db, _leave_group=AsyncMock())
    await actions.run_exits(actor, limit=1)
    assert assess.await_count == 1
    assert assess.await_args.args[1] == member.account_id
    actor._leave_group.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("changes,expected", [
    ({"member_count": 50, "online_count": 2, "exit_group_rule_ban": False}, True),
    ({"coverage": [{"complete": False}, {"complete": False}],
      "ordinary_advertising_last_48h_count": 1, "exit_group_rule_ban": False,
      "online_count": 2, "unknowns": ["history_hidden"],
      "technical_errors": ["history:TimeoutError"]}, True),
    ({"coverage": [{"complete": False}, {"complete": False}],
      "ordinary_advertising_last_48h_count": 1, "member_count": 50}, True),
    ({"coverage": [{"complete": False}, {"complete": False}],
      "ordinary_advertising_last_48h_count": 1, "member_count": 50,
      "exit_group_rule_ban": False, "online_count": 2}, False),
])
async def test_exit_authorization_keeps_three_boolean_arms_independent(test_db, changes, expected):
    group, member, row, _ = await seed(test_db)
    proof = json.loads(row.evidence_json)
    proof.update(changes)
    row.evidence_json = json.dumps(proof)
    await test_db.flush()
    assert (await service.authorize_leave(test_db, 2, group, member))[0] is expected
