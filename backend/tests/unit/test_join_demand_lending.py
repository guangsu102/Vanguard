"""Join demand follows final admission while real work keeps its reservation."""

import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.account import join_demand_lending as lending
from app.core.account import rpc_governor as rpc
from app.core.account.critical_fairness import add_workload_budgets
from app.core.automation_settings import AUTO_JOIN_SCHEDULER_SETTING_KEY
from app.core.settings_models import SystemSetting
from app.modules.acquisition import qualification_service as service
from app.modules.acquisition.models import AutoJoinAttempt
from tests.integration.test_growth_account_scheduler import queue_store  # noqa: F401
from tests.unit.test_ad_read_reserve import mock_usage
from tests.unit.test_completion_capacity import add_join_candidate, allocate, configured

pytestmark = pytest.mark.asyncio


async def update_setting(db, key, **values):
    row = await db.get(SystemSetting, key)
    config = json.loads(row.value)
    config.update(values)
    row.value = json.dumps(config)
    await db.commit()


async def test_future_join_interval_lends_even_ready_inventory_and_restores_when_due(
    test_db, monkeypatch
):
    _, account, _, _, _, config = await configured(test_db)
    await add_join_candidate(test_db, account.id)
    now = datetime.utcnow()
    config.next_join_after = now + timedelta(seconds=5)
    await test_db.commit()
    mock_usage(monkeypatch, {})

    before = await rpc.snapshot(test_db, account.id, now)
    after = await rpc.snapshot(test_db, account.id, now + timedelta(seconds=6))
    assert before["limits"]["join_idle_hold_day"] == 0
    assert before["join_idle_lending"]["reason"] == "join_interval"
    assert after["limits"]["join_idle_hold_day"] == 12
    assert after["join_idle_lending"]["reason"] == "executable_join_candidate"
    assert before["usage"] == after["usage"]


async def test_review_debt_releases_floor_without_waiting_for_backlog_marker(test_db, monkeypatch):
    _, account, _, member, row, config = await configured(test_db)
    await add_join_candidate(test_db, account.id)
    row.decision, row.reason, member.review_status = (
        "technical_wait",
        "telegram_read_budget",
        "review_2h",
    )
    row.next_retry_at = datetime.utcnow() + timedelta(hours=6)
    assert config.join_review_backlog_paused is False
    await test_db.commit()
    mock_usage(
        monkeypatch,
        {
            "day": (640, 60000),
            "join_reserved_day": (640, 60000),
            "critical_review_day": (640, 60000),
        },
    )

    state = await rpc.snapshot(test_db, account.id, datetime.utcnow())
    assert state["limits"]["join_idle_hold_day"] == 0
    plan = state["join_idle_lending"]["admission"]
    assert plan["remaining"] == 0 and plan["review_reads_due"] == 24
    assert state["join_idle_lending"]["reason"] == "join_wait_inventory_review"


async def test_join_stage_uses_final_ad_forecast_and_never_recurses(test_db, monkeypatch):
    _, account, _, _, _, _ = await configured(test_db)
    mock_usage(monkeypatch, {})

    async def ad_forecast(db, account_id, now, limits, budget):
        limits["ad_demand_hold_day"] = 321
        budget["last_ad_forecast"] = True

    monkeypatch.setattr("app.core.account.demand_lending.add_ad_demand_headroom", ad_forecast)
    admission = AsyncMock(return_value={"remaining": 0, "reason": "join_wait_inventory_review"})
    monkeypatch.setattr("app.modules.acquisition.growth_admission.admission_plan", admission)
    snapshot = rpc.snapshot
    recursion = AsyncMock(side_effect=AssertionError("recursive_snapshot"))
    monkeypatch.setattr(rpc, "snapshot", recursion)

    state = await snapshot(test_db, account.id, datetime.utcnow())
    final_budget = admission.await_args.kwargs["rpc"]
    assert final_budget["last_ad_forecast"] is True
    assert final_budget["limits"]["ad_demand_hold_day"] == 321
    assert state["limits"]["join_idle_hold_day"] == 0
    assert not state.get("forecast_errors")
    recursion.assert_not_awaited()


@pytest.mark.parametrize("disabled", ["scheduler", "ad_only", "daily_limit", "account_risk"])
async def test_nonexecutable_join_does_not_keep_ready_candidate_floor(
    test_db, monkeypatch, disabled
):
    _, account, _, _, _, config = await configured(test_db)
    await add_join_candidate(test_db, account.id)
    if disabled == "scheduler":
        await update_setting(test_db, AUTO_JOIN_SCHEDULER_SETTING_KEY, enabled=False)
    elif disabled == "ad_only":
        config.operation_mode = "ad_only"
    elif disabled == "daily_limit":
        config.max_groups_per_day = 0
    else:
        account.risk_level = "quarantined"
    await test_db.commit()
    mock_usage(monkeypatch, {})
    state = await rpc.snapshot(test_db, account.id, datetime.utcnow())
    assert state["limits"]["join_idle_hold_day"] == 0
    assert not state.get("forecast_errors")


async def test_approved_database_reservation_survives_disabled_switches(test_db, monkeypatch):
    _, account, group, _, _, config = await configured(test_db)
    now = datetime.utcnow()
    config.auto_join_enabled = False
    test_db.add(
        AutoJoinAttempt(
            account_id=account.id,
            group_id=group.id,
            request_state="reserved",
            reservation_expires_at=now + timedelta(minutes=5),
        )
    )
    await test_db.commit()
    mock_usage(monkeypatch, {})
    state = await rpc.snapshot(test_db, account.id, now)
    assert state["limits"]["join_idle_hold_day"] == 12
    assert state["join_idle_lending"]["reason"] == "active_join_or_review_reservation"


@pytest.mark.parametrize("due", [True, False])
async def test_approval_only_keeps_floor_when_its_reconciliation_is_due(test_db, monkeypatch, due):
    _, account, group, _, _, config = await configured(test_db)
    now = datetime.utcnow()
    config.next_join_after = now + timedelta(hours=1)
    test_db.add(
        AutoJoinAttempt(
            account_id=account.id,
            group_id=group.id,
            status="pending",
            request_state="sent",
            target_key="username:approval",
            reconciliation_next_at=now + timedelta(seconds=-1 if due else 3600),
        )
    )
    await test_db.commit()
    mock_usage(monkeypatch, {})
    state = await rpc.snapshot(test_db, account.id, now)
    assert state["limits"]["join_idle_hold_day"] == (12 if due else 0)
    if due:
        assert state["join_idle_lending"]["due"] == ["join_request_reconciliation"]


@pytest.mark.parametrize(
    "case", ["due", "future", "disabled", "protected", "claimed", "spent", "old_epoch"]
)
async def test_verification_floor_requires_enabled_current_unclaimed_due_work(
    test_db, monkeypatch, case
):
    _, account, group, member, row, config = await configured(test_db)
    now = datetime.utcnow()
    config.next_join_after = now + timedelta(hours=1)
    member.joined_at = now - timedelta(hours=1)
    row.membership_joined_at, row.decision = member.joined_at, "observe"
    if case == "old_epoch":
        row.membership_joined_at -= timedelta(days=1)
    await update_setting(
        test_db,
        service.SETTING_KEY,
        execute_verification=True,
        manual_protected_group_ids=[group.id] if case == "protected" else [],
    )
    if case == "disabled":
        await update_setting(
            test_db, AUTO_JOIN_SCHEDULER_SETTING_KEY, join_verification={"enabled": False}
        )
    ledger = {"membership_version": member.joined_at.isoformat()}
    if case in {"future", "claimed"}:
        ledger["next_check_at" if case == "future" else "claim_until"] = (
            now + timedelta(hours=1)
        ).isoformat()
    if case == "spent":
        ledger["actions"] = [{"status": "sent"}] * 3
    test_db.add(
        SystemSetting(key=f"qualification.verification.{member.id}", value=json.dumps(ledger))
    )
    await test_db.commit()
    mock_usage(monkeypatch, {})
    state = await rpc.snapshot(test_db, account.id, now)
    assert not state.get("forecast_errors")
    assert state["limits"]["join_idle_hold_day"] == (12 if case == "due" else 0)
    if case == "due":
        assert state["join_idle_lending"]["due"] == ["qualification_verification"]


@pytest.mark.parametrize("case", ["due", "future", "scope", "protected", "owned"])
async def test_exit_floor_respects_due_time_membership_scope_and_protection(
    test_db, monkeypatch, case
):
    _, account, group, member, _, config = await configured(test_db)
    now = datetime.utcnow()
    config.next_join_after = now + timedelta(hours=1)
    member.review_status = "exit_pending"
    member.review_next_at = now + timedelta(seconds=3600 if case == "future" else -1)
    if case == "scope":
        await update_setting(test_db, service.SETTING_KEY, exit_membership_ids=[member.id + 1])
    if case == "protected":
        await update_setting(test_db, service.SETTING_KEY, protected_membership_ids=[member.id])
    if case == "owned":
        from app.modules.owned_group.models import OwnedGroupAsset

        test_db.add(
            OwnedGroupAsset(
                internal_name="owned",
                title="owned",
                owner_account_id=account.id,
                core_group_id=group.id,
                telegram_chat_id=group.group_id,
            )
        )
    await test_db.commit()
    mock_usage(monkeypatch, {})
    state = await rpc.snapshot(test_db, account.id, now)
    assert not state.get("forecast_errors")
    assert state["limits"]["join_idle_hold_day"] == (12 if case == "due" else 0)


async def test_incomplete_inventory_and_forecast_failure_keep_default_floor(test_db, monkeypatch):
    _, account, _, _, _, _ = await configured(test_db)
    mock_usage(monkeypatch, {})
    incomplete = {
        "active_request_reservation": False,
        "executable": False,
        "sample_complete": False,
    }
    monkeypatch.setattr(
        "app.core.account.work_demand._join_demand_snapshot", AsyncMock(return_value=incomplete)
    )
    before = await rpc.snapshot(test_db, account.id, datetime.utcnow())
    assert before["limits"]["join_idle_hold_day"] == -1

    async def broken(db, account_id, now, limits, budget):
        limits["join_idle_hold_day"] = 0
        budget["usage"]["day"]["used"] = 0
        raise ValueError("optional_join_inventory_failed")

    test_db.add(SystemSetting(key="join.test.business", value="preserved"))
    monkeypatch.setattr(lending, "add_join_demand_headroom", broken)
    after = await rpc.snapshot(test_db, account.id, datetime.utcnow())
    assert "join_idle_hold_day" not in after["limits"]
    assert after["usage"] == before["usage"]
    assert after["forecast_errors"] == {"join_demand_lending": "ValueError"}
    assert test_db.is_active
    assert (
        await test_db.scalar(
            select(SystemSetting.value).where(SystemSetting.key == "join.test.business")
        )
        == "preserved"
    )
    await test_db.commit()


async def test_actual_0644_window_can_complete_review_keep_ads_and_preserve_usage_and_ttl(
    queue_store, test_db, monkeypatch  # noqa: F811
):
    _, account, _, _, _, config = await configured(test_db)
    now = datetime.utcnow()
    config.next_join_after = now + timedelta(minutes=28)
    await test_db.commit()
    monkeypatch.setattr("app.core.redis.get_redis", AsyncMock(return_value=queue_store))
    monkeypatch.setattr(rpc.settings, "TELEGRAM_READ_BUDGET_PERCENT", 150)
    limits = {
        **rpc.limits_for({}, now),
        "survival_hold_hour": 55,
        "survival_hold_day": 164,
        "ad_refresh_hold_hour": 10,
        "ad_refresh_hold_day": 404,
        "ad_demand_hold_hour": 69,
        "ad_demand_hold_day": 867,
    }
    counters = {
        "hour": 237,
        "day": 2447,
        "ad_hour": 119,
        "ad_day": 924,
        "survival_hour": 1,
        "survival_day": 201,
        "join_reserved_hour": 72,
        "join_reserved_day": 763,
        "sync_reserved_hour": 40,
        "sync_reserved_day": 463,
        "flex_reserved_hour": 5,
        "flex_reserved_day": 96,
        "critical_join_hour": 0,
        "critical_join_day": 47,
        "critical_review_hour": 72,
        "critical_review_day": 716,
        "listener_hour": 1,
        "listener_day": 12,
    }
    for name, value in counters.items():
        await queue_store.set(
            f"vanguard:rpc:{account.id}:{name}",
            value,
            ex=1668 if name.endswith("hour") or name == "hour" else 57479,
        )
    budget = await rpc.read_budget_state(account.id, limits, now)
    add_workload_budgets(budget, limits)
    assert budget["critical_workloads"]["review"]["remaining"] == 9
    assert await allocate(queue_store, limits, 24, token="blocked", mode="reserve") > 0
    await lending.add_join_demand_headroom(test_db, account.id, now, limits, budget)
    add_workload_budgets(budget, limits)
    assert budget["critical_workloads"]["review"]["remaining"] == 31
    assert await allocate(queue_store, limits, 24, token="review", mode="reserve") == 0
    assert await queue_store.get("vanguard:rpc:2:day") == "2447"
    for _ in range(24):
        assert await allocate(queue_store, limits, 1, token="review") == 0
    assert await allocate(queue_store, limits, 5, lane="ad") == 0
    assert await queue_store.get("vanguard:rpc:2:day") == "2476"
    assert await queue_store.get("vanguard:rpc:2:hour") == "266"
    assert 57469 <= await queue_store.ttl("vanguard:rpc:2:day") <= 57479
    assert 1658 <= await queue_store.ttl("vanguard:rpc:2:hour") <= 1668
