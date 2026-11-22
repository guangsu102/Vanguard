"""Operational reporting uses durable facts and never queues qualification work."""
from datetime import datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock
import json

import pytest
from sqlalchemy import func, select

from app.api import group_qualification as api
from app.core.account.models import AccountOutboundAttempt
from app.core.group.models import GroupAccountMembership
from app.modules.acquisition import qualification_engine as engine
from app.modules.acquisition.models import (
    AutoJoinAttempt, GroupQualificationAudit, QualificationActiveGrant, QualificationWork,
)

NOW = datetime(2026, 11, 5, 10)
HOUR_AGO = NOW - timedelta(hours=1)


def member(identity=101):
    return GroupAccountMembership(id=identity, account_id=34, group_id=identity,
        telegram_group_id=-identity, status="joined", joined_at=HOUR_AGO)


def audit():
    return GroupQualificationAudit(id=999, batch_id="report-only", membership_id=101,
        account_id=34, group_id=101, policy_version="v1", state="completed",
        decision="trial", evidence_json="{}", checked_at=HOUR_AGO, membership_joined_at=HOUR_AGO)


def work():
    return QualificationWork(membership_id=101, account_id=34, group_id=101,
        scope_key="scope", policy_version="v1", profile_fingerprint="profile",
        state="WAIT_BUDGET", reason="telegram_read_budget", next_step_reads=1,
        ready_at=NOW + timedelta(minutes=1), updated_at=NOW)


@pytest.mark.asyncio
async def test_disabled_reporting_does_not_touch_new_tables(monkeypatch):
    monkeypatch.setattr(engine, "enabled_for", lambda account_id: False)
    db = Obj(execute=AsyncMock(side_effect=AssertionError("No new table reads")))
    assert await api.operational_states(db, [Obj(account_id=34)]) == {}
    assert (await api.flow_summary(db, 34, NOW))["business"]["available"] is False
    db.execute.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("work_ids", [[], [7, 8]])
async def test_manual_review_response_counts_new_work_without_fabricating_audits(monkeypatch, work_ids):
    from unittest.mock import Mock
    db = Obj(get=AsyncMock(return_value=Obj(value=json.dumps({"work_ids": work_ids}))), commit=AsyncMock())
    monkeypatch.setattr(api, "queue_reviews", AsyncMock(return_value=[5]))
    monkeypatch.setattr("app.core.scheduler.tasks.group_qualification_task.apply_async", Mock())
    result = await api.create_reviews(api.ReviewRequest(account_ids=[34]), "review-batch", db)
    assert result["data"]["count"] == 1 + len(work_ids)
    assert result["data"]["audit_ids"] == [5] and result["data"]["work_ids"] == work_ids


@pytest.mark.asyncio
async def test_active_authorization_and_waiting_job_are_separate_and_read_only(test_db, monkeypatch):
    from app.core.account.models import AccountType, TelegramAccount
    from app.core.group.models import Group
    from app.core.settings_models import SystemSetting
    from app.modules.acquisition.qualification_service import SETTING_KEY
    from app.modules.acquisition.group_qualification import POLICY_VERSION
    from app.modules.acquisition.qualification_system_identity import identity_fingerprint

    monkeypatch.setattr(engine, "enabled_for", lambda account_id: True)
    row = audit()
    row.policy_version = POLICY_VERSION
    row.evidence_json = json.dumps({"system_identity_coverage": True,
        "system_identity_fingerprint": identity_fingerprint({34}, {34: 3400}, []),
        "account_id": 34, "group_id": 101, "telegram_group_id": -101,
        "content_scope": "text_profile", "policy_version": POLICY_VERSION})
    # The engine owns scope validation; this API test checks projection of an
    # independently validated grant while unrelated work is still waiting.
    monkeypatch.setattr(engine, "_scope_key", lambda *args: "scope")
    monkeypatch.setattr("app.modules.acquisition.qualification_lifetime.authorization_current", lambda *args: True)
    test_db.add_all([TelegramAccount(id=34, identifier="api-fixture", session_name="api-fixture",
        account_type=AccountType.PROMOTER), SystemSetting(key=SETTING_KEY,
        value=json.dumps({"system_account_user_ids": {"34": 3400}, "system_user_ids": []})),
        Group(id=101, group_id=-101, title="API scoped fixture"), member(), row, work(), QualificationActiveGrant(
        membership_id=101, account_id=34, group_id=101, audit_id=999,
        scope_key="scope", policy_version=POLICY_VERSION, profile_fingerprint="profile")])
    await test_db.flush()
    enqueue = AsyncMock(side_effect=AssertionError("Reporting must not enqueue"))
    monkeypatch.setattr(engine, "enqueue", enqueue)
    states = await api.operational_states(test_db, [row])
    value = api.serialize(row, operational=states[101])
    assert value["authorization"]["state"] == "active"
    assert value["authorization"]["grant_audit_id"] == 999
    assert value["job"]["state"] == "WAIT_BUDGET"
    assert value["blocker"]["required_reads"] == 1
    assert value["blocker"]["available_reads"] is None
    assert value["decision"] == "trial" and value["state"] == "completed"
    assert await test_db.scalar(select(func.count(QualificationWork.id))) == 1
    enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_reporting_error_is_savepoint_isolated_and_never_becomes_missing_grant(test_db, monkeypatch):
    monkeypatch.setattr(engine, "enabled_for", lambda account_id: True)
    monkeypatch.setattr(engine, "runtime_for_memberships", AsyncMock(side_effect=RuntimeError("storage unavailable")))
    test_db.add(member())
    await test_db.flush()
    result = await api.operational_states(test_db, [Obj(membership_id=101, account_id=34)])
    assert result[101]["authorization"]["state"] == "unavailable"
    assert result[101]["blocker"]["code"] == "runtime_unavailable"
    assert await test_db.scalar(select(func.count(GroupAccountMembership.id))) == 1


@pytest.mark.asyncio
async def test_flow_counts_real_receipts_and_preserves_unrecorded_join_stages(test_db, monkeypatch):
    monkeypatch.setattr(engine, "enabled_for", lambda account_id: True)
    test_db.add_all([member(), member(102)])  # A new member may not have a final audit yet.
    joins = [
        AutoJoinAttempt(account_id=34, telegram_action_attempted=True, request_sent_at=HOUR_AGO,
            request_state="outcome_unknown", status="pending"),
        AutoJoinAttempt(account_id=34, telegram_action_attempted=True, request_sent_at=HOUR_AGO,
            sdk_entered_at=HOUR_AGO, platform_responded_at=HOUR_AGO, joined_at=HOUR_AGO,
            status="success"),
        AutoJoinAttempt(account_id=34, telegram_action_attempted=False, request_sent_at=HOUR_AGO,
            status="pending"),
        AutoJoinAttempt(account_id=2, telegram_action_attempted=True, request_sent_at=HOUR_AGO,
            sdk_entered_at=HOUR_AGO, status="pending"),
    ]
    test_db.add_all(joins)
    for identity, state, message_id, category in (("ok", "succeeded", 7, "ad"),
            ("missing", "succeeded", None, "ad"), ("unknown", "unknown", None, "ad"),
            ("other", "succeeded", 8, "verification")):
        test_db.add(AccountOutboundAttempt(account_id=34, attempt_key=identity, category=category,
            target_key=identity, state=state, message_id=message_id, attempted_at=HOUR_AGO))
    await test_db.flush()
    result = await api.flow_summary(test_db, 34, NOW)
    assert result["authorization"]["by_state"] == {"missing": 2}
    assert result["authorization"]["scope"] == "joined_memberships"
    assert [row["membership_id"] for row in result["current_work"]["items"]] == [101, 102]
    assert result["current_work"]["items"][0]["authorization"]["state"] == "missing"
    business = result["business"]
    assert business["available"] and business["ads"] == {"attempted": 3, "succeeded": 1, "unknown": 1}
    assert business["joins"] == {"intent": 2, "sdk_entered": 1, "platform_responded": 1,
        "membership_confirmed": 1, "unknown": 1, "sdk_entry_unrecorded": 1,
        "platform_response_unrecorded": 1}
    assert business["join_stage_coverage"] == "recorded_facts_only;historical_null_not_inferred"


@pytest.mark.parametrize("state,remaining,expected", [("ready", 0, 0), ("ready", 7, 7),
    ("cooldown", 7, 0), ("unavailable", 7, None), ("ready", None, None)])
def test_current_work_budget_projects_known_zero_and_preserves_unknown(state, remaining, expected):
    item = {"job": {"state": "WAIT_BUDGET", "kind": "initial"}, "blocker": {"required_reads": 2}}
    flow = {"current_work": {"items": [item]}}
    window = {"limit": 100, "used": 93, "remaining": 7, "ttl_seconds": 30}
    api.project_work_budget(flow, {"state": state, "review": {"remaining": remaining, "retry_after_seconds": 30},
        "windows": {"critical_review_hour": window}}, NOW)
    assert item["blocker"]["available_reads"] == expected
    assert item["blocker"]["budget_windows"] == {"critical_review_hour": window}
    assert item["blocker"]["budget_purpose"] == "group_qualification"
    assert item["blocker"]["retry_is_prediction"] is True
    assert item["blocker"]["required_reads"] == 2


@pytest.mark.asyncio
async def test_read_runtime_reuses_one_budget_snapshot_for_every_current_job(monkeypatch):
    from app.core.config import settings
    import app.core.account.rpc_governor as governor

    monkeypatch.setattr(settings, "BUDGET_LEDGER_ENABLED", False)
    jobs = [{"job": {"state": "WAIT_BUDGET", "kind": kind}, "blocker": {"required_reads": 2}}
        for kind in ["initial", "renewal"]*20]
    monkeypatch.setattr(api, "flow_summary", AsyncMock(return_value={"current_work": {"items": jobs}}))
    monkeypatch.setattr(api, "sample_summary", AsyncMock(return_value={"available": False}))
    monkeypatch.setattr("app.modules.acquisition.review_read_runtime.evidence_status", AsyncMock(return_value={"enabled": False}))
    monkeypatch.setattr("app.modules.acquisition.review_storage_budget.verify", lambda paths: {})
    monkeypatch.setattr("app.core.account.read_metrics.snapshot", AsyncMock(return_value={"available": False}))
    snapshot = AsyncMock(return_value={"state": "ready", "critical_workloads": {"review": {"remaining": 9, "retry_after_seconds": 0}},
        "lanes": {"ad_refresh": {"remaining": 6, "retry_after_seconds": 20}},
        "usage": {"minute": {"limit": 20, "used": 2, "remaining": 18}}})
    monkeypatch.setattr(governor, "snapshot", snapshot)
    db = Obj(execute=AsyncMock(side_effect=AssertionError("projection must not query per job")))
    result = (await api.read_runtime(account_id=34, db=db))["data"]
    assert snapshot.await_count == 1 and not db.execute.await_count
    assert [item["blocker"]["available_reads"] for item in jobs] == [9, 6]*20
    assert jobs[1]["blocker"]["budget_purpose"] == "ad_qualification_refresh"
    assert result["budget"]["windows"]["minute"] == {"limit": 20, "used": 2, "remaining": 18}
