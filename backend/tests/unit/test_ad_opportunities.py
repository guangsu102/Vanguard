"""Due obligations stay in the denominator through waits and restart recovery."""
from datetime import datetime, timedelta
import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.settings_models import SystemSetting
from app.modules.acquisition import ad_opportunities as meter
from app.modules.acquisition import qualification_engine as engine
from app.modules.acquisition.models import AccountAdBinding, AdCampaign, AdDeliveryLog, AdDeliveryScheduleState
from tests.unit.test_qualification_engine import baseline


@pytest.fixture(autouse=True)
def rollout(monkeypatch):
    monkeypatch.setattr(settings, "GROWTH_EFFICIENCY_ENABLED", True)
    monkeypatch.setattr(settings, "GROWTH_EFFICIENCY_ACCOUNT_IDS", "2")
    monkeypatch.setattr(engine, "_publish_wakes", AsyncMock())


async def due_fixture(db):
    account, group, member, _, _ = await baseline(db)
    now = datetime.utcnow()
    campaign = AdCampaign(id=88, name="due-fixture", enabled=True, delivery_policy="growth",
        target_group_ids=json.dumps([group.id]), min_wait_after_join_minutes=0)
    binding = AccountAdBinding(account_id=account.id, ad_campaign_id=campaign.id, enabled=True)
    schedule = AdDeliveryScheduleState(account_id=account.id, campaign_id=campaign.id,
        group_id=group.id, telegram_group_id=group.group_id, next_due_at=now-timedelta(minutes=10))
    db.add_all([campaign, binding, schedule])
    await db.commit()
    return now, member, schedule


async def opportunity_rows(db):
    return list((await db.scalars(select(SystemSetting).where(SystemSetting.key.startswith(meter.PREFIX)))).all())


def receipt_context(member, **changes):
    return json.dumps({"membership_joined_at": member.joined_at.isoformat(), "membership_id": member.id,
        "telegram_group_id": member.telegram_group_id, "account_id": member.account_id,
        "audit_id": 1, "policy_version": "pp-ai-qualification-v3", **changes})


@pytest.mark.asyncio
async def test_budget_software_retries_and_restart_keep_one_persistent_due_obligation(test_db):
    now, _, state = await due_fixture(test_db)
    assert (await meter.collect_due(test_db, now))["new"] == 1
    await test_db.commit()
    initial = json.loads((await opportunity_rows(test_db))[0].value)
    state.next_due_at, state.last_reason = now+timedelta(hours=5), "telegram_read_budget"
    await test_db.commit()
    assert (await meter.collect_due(test_db, now+timedelta(seconds=30)))["new"] == 0
    await meter.note_result(test_db, 2, {"reason": "account_operation_lease_busy"}, now)
    await test_db.commit()
    test_db.expunge_all()  # Rebuild observations from durable rows, not Python state.
    assert (await meter.collect_due(test_db, now+timedelta(seconds=60)))["new"] == 0
    rows = await opportunity_rows(test_db)
    current = json.loads(rows[0].value)
    assert len(rows) == 1 and current["id"] == initial["id"] and current["due_at"] == initial["due_at"]
    result = await meter.summary(test_db, 2, now+timedelta(seconds=60))
    assert result["due"] == result["pending"] == 1 and result["completed"] == 0
    assert not result["complete"] and result["completion_rate"] is None


@pytest.mark.asyncio
async def test_only_linked_real_successful_message_completes_the_due_obligation(test_db):
    now, member, state = await due_fixture(test_db)
    await meter.collect_due(test_db, now)
    log = AdDeliveryLog(account_id=2, group_id=member.group_id, telegram_group_id=member.telegram_group_id,
        ad_campaign_id=88, status="unknown", qualification_context_json=receipt_context(member))
    test_db.add(log)
    await test_db.flush()
    await meter.observe_log(test_db, log)
    value = json.loads((await opportunity_rows(test_db))[0].value)
    assert value["state"] == "pending" and value["last_reason"] == "external_outcome_unknown"
    assert json.loads(log.qualification_context_json)["ad_opportunity_id"] == value["id"]
    log.status, log.sent_at = "success", now
    await meter.observe_log(test_db, log)
    assert json.loads((await opportunity_rows(test_db))[0].value)["state"] == "pending"
    log.telegram_message_id = 1001
    await meter.observe_log(test_db, log)
    await test_db.commit()
    value = json.loads((await opportunity_rows(test_db))[0].value)
    assert value["state"] == "completed" and value["receipt_log_id"] == log.id and value["message_id"] == 1001
    await meter.observe_log(test_db, log)
    state.last_success_at, state.next_due_at = now, now+timedelta(hours=1)
    await test_db.commit()
    assert (await meter.collect_due(test_db, now+timedelta(seconds=30)))["new"] == 0
    assert (await meter.collect_due(test_db, now+timedelta(hours=2)))["new"] == 1
    assert len(await opportunity_rows(test_db)) == 2


@pytest.mark.asyncio
async def test_rejoin_incarnation_cannot_be_completed_by_an_old_receipt(test_db):
    now, member, _ = await due_fixture(test_db)
    await meter.collect_due(test_db, now)
    log = AdDeliveryLog(account_id=2, group_id=member.group_id, telegram_group_id=member.telegram_group_id,
        ad_campaign_id=88, status="success", sent_at=now, telegram_message_id=991,
        qualification_context_json=receipt_context(member, membership_joined_at=(member.joined_at-timedelta(days=1)).isoformat()))
    test_db.add(log)
    await test_db.flush()
    await meter.observe_log(test_db, log)
    assert json.loads((await opportunity_rows(test_db))[0].value)["state"] == "pending"


@pytest.mark.asyncio
async def test_complete_window_requires_dispatch_coverage_and_keeps_waits_in_denominator(test_db):
    now, _, _ = await due_fixture(test_db)
    await meter.collect_due(test_db, now)
    marker = await test_db.get(SystemSetting, meter.COVERAGE_PREFIX+"2")
    coverage = json.loads(marker.value)
    coverage["started_at"] = (now-timedelta(days=2)).isoformat()
    marker.value = json.dumps(coverage)
    result = await meter.summary(test_db, 2, now)
    assert result["complete"] and result["due"] == 1 and result["completion_rate"] == 0
    assert result["pending_by_reason"] == {"software_dispatch_wait": 1}
    await meter.collect_due(test_db, now+timedelta(minutes=5))
    result = await meter.summary(test_db, 2, now+timedelta(minutes=5))
    assert not result["complete"] and result["completion_rate"] is None
    assert "dispatcher_scan_gap" in result["incomplete_reasons"]
    result = await meter.summary(test_db, 2, now+timedelta(minutes=8))
    assert "dispatcher_scan_stale" in result["incomplete_reasons"]


@pytest.mark.asyncio
async def test_disabled_measurement_never_opens_or_commits_a_transaction(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(settings, "GROWTH_EFFICIENCY_ENABLED", False)
    db = SimpleNamespace(begin_nested=AsyncMock(side_effect=AssertionError()), commit=AsyncMock(side_effect=AssertionError()))
    assert await meter.capture_due(db, datetime.utcnow()) == {"accounts": 0, "new": 0}
    await meter.observe_log_safely(db, SimpleNamespace(account_id=2))


@pytest.mark.asyncio
@pytest.mark.parametrize("capture_failure", [False, True])
async def test_capture_owns_a_short_session_and_cannot_commit_callers_pending_edits(tmp_path, monkeypatch, capture_failure):
    from contextlib import asynccontextmanager
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    database = create_async_engine("sqlite+aiosqlite:///"+str(tmp_path/"sessions.sqlite"))
    async with database.begin() as connection:
        await connection.run_sync(SystemSetting.__table__.create)
    factory = async_sessionmaker(database, expire_on_commit=False)
    @asynccontextmanager
    async def dedicated_session():
        async with factory() as session:
            yield session
            await session.commit()
    async def collect(session, now, **kwargs):
        session.add(SystemSetting(key="meter-owned", value="{}"))
        if capture_failure:
            raise RuntimeError("measurement failed")
        return {"accounts": 1, "new": 1}
    monkeypatch.setattr("app.core.database.get_db_session", dedicated_session)
    monkeypatch.setattr(meter, "collect_due", collect)
    async with factory() as caller:
        unrelated = SystemSetting(key="caller-pending", value="{}")
        caller.add(unrelated)
        result = await meter.capture_due(caller, datetime.utcnow())
        assert result == ({"accounts": 1, "new": 0, "incomplete": True} if capture_failure else {"accounts": 1, "new": 1})
        assert unrelated in caller.new
        async with factory() as independent:
            assert (await independent.get(SystemSetting, "meter-owned") is None) is capture_failure
            assert await independent.get(SystemSetting, "caller-pending") is None
        await caller.rollback()
    await database.dispose()


@pytest.mark.asyncio
async def test_old_pending_obligation_cannot_age_out_of_the_completion_denominator(test_db):
    now, member, _ = await due_fixture(test_db)
    await meter.collect_due(test_db, now)
    row = (await opportunity_rows(test_db))[0]
    value = json.loads(row.value)
    value["due_at"] = (now-timedelta(days=2)).isoformat()
    row.value = json.dumps(value, separators=(",", ":"))
    assert (await meter.summary(test_db, 2, now))["due"] == 1
    log = AdDeliveryLog(account_id=2, group_id=member.group_id, telegram_group_id=member.telegram_group_id,
        ad_campaign_id=88, status="success", sent_at=now, telegram_message_id=1002,
        qualification_context_json=receipt_context(member))
    test_db.add(log)
    await test_db.flush()
    await meter.observe_log(test_db, log)
    result = await meter.summary(test_db, 2, now)
    assert result["due"] == result["completed"] == 1 and not result["pending"]


@pytest.mark.asyncio
async def test_requalification_does_not_add_another_business_obligation_and_receipt_keeps_actual_scope(test_db):
    now, member, _ = await due_fixture(test_db)
    await meter.collect_due(test_db, now)
    row = (await opportunity_rows(test_db))[0]
    due_audit = json.loads(row.value)["due_authorization"]["grant_audit_id"]
    log = AdDeliveryLog(account_id=2, group_id=member.group_id, telegram_group_id=member.telegram_group_id,
        ad_campaign_id=88, status="success", sent_at=now, telegram_message_id=1003,
        qualification_context_json=receipt_context(member, audit_id=due_audit+100))
    test_db.add(log)
    await test_db.flush()
    await meter.observe_log(test_db, log)
    result = json.loads(row.value)
    assert result["state"] == "completed" and result["authorization_changed_since_due"] is True
    assert result["due_authorization"]["grant_audit_id"] == due_audit
    assert result["receipt_authorization"]["audit_id"] == due_audit+100
    assert len(await opportunity_rows(test_db)) == 1


@pytest.mark.asyncio
async def test_capture_validates_many_members_with_a_bounded_number_of_selects(test_db):
    from sqlalchemy import event
    from app.core.group.models import Group, GroupAccountMembership
    now, _, _ = await due_fixture(test_db)
    for index in range(12):
        group = Group(id=1000+index, group_id=4000000+index, title="batch fixture")
        test_db.add(group)
        test_db.add(GroupAccountMembership(id=1000+index, account_id=2, group_id=group.id,
            telegram_group_id=group.group_id, status="joined", joined_at=now-timedelta(days=1)))
    await test_db.commit()
    statements = []
    def record(connection, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)
    event.listen(test_db.bind.sync_engine, "before_cursor_execute", record)
    try:
        await meter.collect_due(test_db, now)
    finally:
        event.remove(test_db.bind.sync_engine, "before_cursor_execute", record)
    assert len(statements) <= 20


@pytest.mark.asyncio
async def test_retention_cleans_old_completed_measurements_and_never_deletes_old_pending(test_db):
    now, _, _ = await due_fixture(test_db)
    old = now-timedelta(days=10)
    pending = {"id": meter.PREFIX+"2.old-pending", "membership_id": 999, "campaign_id": 88,
        "incarnation": old.isoformat(), "due_at": old.isoformat(), "state": "pending"}
    completed = {**pending, "id": meter.PREFIX+"2.old-completed", "state": "completed",
        "completed_at": old.isoformat(), "receipt_log_id": 400, "message_id": 700}
    test_db.add_all([SystemSetting(key=value["id"], value=json.dumps(value, separators=(",", ":")),
        created_at=old, updated_at=old) for value in (pending, completed)])
    await test_db.commit()
    await meter.collect_due(test_db, now)
    assert await test_db.get(SystemSetting, completed["id"]) is None
    assert await test_db.get(SystemSetting, pending["id"]) is not None
    result = await meter.summary(test_db, 2, now)
    assert result["pending_before_window"] == 1 and result["due"] == 2
