"""Regression coverage for receipt rechecks, forecast isolation and empty dispatch."""

import json
from copy import deepcopy
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.account import rpc_governor as rpc
from app.core.account.budget_forecast import apply_forecast
from app.core.account.demand_lending import add_refresh_demand_headroom
from app.core.account.models import AccountOutboundAttempt
from app.core.account.send_receipts import RECHECK_PREFIX, recover_missing_receipts
from app.core.scheduler import growth_dispatch as scheduler
from app.core.settings_models import SystemSetting
from app.modules.acquisition.models import (
    AdDeliveryLog,
    AutoJoinAttempt,
    GroupAdFrequency,
    GroupAdFrequencyEvent,
)
from tests.integration.test_growth_account_scheduler import add_accounts
from tests.unit.test_ad_read_reserve import mock_usage
from tests.unit.test_qualification_service import setup

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("extra_ad_reads,expected_room", [(0, 16), (588, 6), (1531, 0)])
async def test_unfunded_send_plan_leaves_full_renewal_only_with_real_headroom(
    monkeypatch, extra_ad_reads, expected_room,
):
    monkeypatch.setattr(rpc.settings, "TELEGRAM_READ_BUDGET_PERCENT", 150)
    now = datetime.utcnow()
    limits = rpc.limits_for({}, now)
    usage = {
        "day": 2069 + extra_ad_reads, "ad_day": 747 + extra_ad_reads,
        "survival_day": 174, "join_reserved_day": 660,
        "sync_reserved_day": 399, "flex_reserved_day": 89,
    }
    mock_usage(monkeypatch, {name: (used, 60000) for name, used in usage.items()})
    budget = await rpc.read_budget_state(2, limits, now)
    before = deepcopy(budget["usage"])
    demand = {
        "delivery_cost": 5, "renewal_cost": 11,
        "hour": {"sends": 5, "renewals": 1},
        "day": {"sends": 103, "renewals": 23},
    }
    add_refresh_demand_headroom(limits, budget, demand)
    assert budget["usage"] == before and limits["day"] == 3600
    assert budget["lanes"]["ad_refresh"]["remaining"] == expected_room
    if not extra_ad_reads:
        assert limits["ad_refresh_hold_day"] == 588
        assert budget["ad_dependency_recovery"]["unfunded_delivery_plan"]["day"] == {
            "available_reads": 604, "renewal_reads": 16,
            "forecast_delivery_hold": 618, "send_floor": 121,
        }


async def test_unfunded_plan_changes_no_hold_without_a_due_repair(monkeypatch):
    now = datetime.utcnow()
    limits = rpc.limits_for({}, now)
    mock_usage(monkeypatch, {})
    budget = await rpc.read_budget_state(2, limits, now)
    before_limits, before_budget = deepcopy(limits), deepcopy(budget)
    demand = {"delivery_cost": 5, "renewal_cost": 11,
              "hour": {"sends": 103, "renewals": 0},
              "day": {"sends": 103, "renewals": 0}}
    add_refresh_demand_headroom(limits, budget, demand)
    assert limits == before_limits and budget == before_budget


@pytest.mark.parametrize("late_proof", [False, True])
async def test_receipt_rechecks_after_its_independent_timer_expires(test_db, late_proof):
    account, group, _, _ = await setup(test_db)
    now = datetime.utcnow()
    log = AdDeliveryLog(
        account_id=account.id, group_id=group.id, telegram_group_id=group.group_id,
        ad_campaign_id=1, status="pending", created_at=now - timedelta(days=1),
        reservation_token="late-proof", error="send_outcome_unknown:ValueError",
        survival_status="not_required", survival_stage="complete",
    )
    attempt = AccountOutboundAttempt(
        account_id=account.id, attempt_key="ad:late-proof", category="ad",
        target_key="group:test", state="unknown",
    )
    test_db.add_all([log, attempt])
    await test_db.commit()
    first = await recover_missing_receipts(test_db, now, account.id, 10)
    assert first["manual_evidence_required"] == 1
    assert log.survival_check_due_at is None
    assert (await recover_missing_receipts(test_db, now + timedelta(hours=1), account.id, 10)) == {
        "receipt_ids_recovered": 0, "receipt_evidence_required": 0,
        "manual_evidence_required": 0, "abandoned_pending_closed": 0,
    }
    if late_proof:
        # A late local ledger proof is still safe to adopt without another
        # Telegram read; the terminal manual state is reopened only by exact
        # evidence.
        attempt.message_id = 12345
        await test_db.commit()
    due = now + timedelta(hours=6)
    # Account scoping and the exact deadline must remain effective.
    assert (await recover_missing_receipts(test_db, due, account.id + 1, 10))["receipt_ids_recovered"] == 0
    result = await recover_missing_receipts(test_db, due, account.id, 10)
    schedule = await test_db.get(SystemSetting, RECHECK_PREFIX + str(log.id))
    assert attempt.state in {"unknown", "succeeded"}
    assert json.loads(log.qualification_context_json)["send_reconciliation"]["automatic_resend"] is False
    if late_proof:
        assert result["receipt_ids_recovered"] == 1
        assert log.status == "success"
        assert attempt.state == "succeeded"
        assert log.telegram_message_id == 12345 and log.survival_check_due_at == due
        assert schedule is None
    else:
        assert result["manual_evidence_required"] == 0
        assert schedule is None
        assert log.telegram_message_id is None and log.survival_check_due_at is None


async def test_due_receipt_recheck_never_steals_a_claimed_reader(test_db):
    account, group, _, _ = await setup(test_db)
    now = datetime.utcnow()
    log = AdDeliveryLog(
        account_id=account.id, group_id=group.id, telegram_group_id=group.group_id,
        ad_campaign_id=1, status="pending", created_at=now - timedelta(days=1),
        survival_claim_token="live-reader", survival_claim_expires_at=now + timedelta(minutes=3),
    )
    test_db.add(log)
    await test_db.flush()
    test_db.add(SystemSetting(key=RECHECK_PREFIX + str(log.id), value=now.isoformat()))
    await test_db.commit()
    assert (await recover_missing_receipts(test_db, now, account.id, 10))["receipt_evidence_required"] == 0
    assert log.survival_claim_token == "live-reader"


FORECASTS = [
    ("survival_lending", "add_idle_survival_headroom"),
    ("sync_lending", "add_idle_sync_headroom"),
    ("work_demand", "add_work_demand"),
    ("demand_lending", "add_ad_demand_headroom"),
]


@pytest.mark.parametrize("failed_module,failed_function", FORECASTS)
@pytest.mark.parametrize("exhausted", [False, True])
async def test_optional_forecast_failure_preserves_budget_and_business_transaction(
    test_db, monkeypatch, failed_module, failed_function, exhausted,
):
    import importlib

    now = datetime.utcnow()
    for module, function in FORECASTS:
        monkeypatch.setattr(importlib.import_module("app.core.account." + module), function, AsyncMock())
    day_limit = rpc.limits_for({}, now)["day"]
    mock_usage(monkeypatch, {"day": (day_limit if exhausted else 0, 60000)})
    before = await rpc.snapshot(test_db, 2, now)
    marker = SystemSetting(key="business.pending", value="must-survive")
    test_db.add(marker)

    async def broken(db, account_id, checked_at, limits, budget):
        limits["ad_demand_hold_day"] = 0
        budget["lanes"]["ad"]["remaining"] = 99999
        await db.execute(text("SELECT * FROM missing_optional_forecast_table"))

    monkeypatch.setattr(importlib.import_module("app.core.account." + failed_module), failed_function, broken)
    after = await rpc.snapshot(test_db, 2, now)
    assert after["state"] == before["state"]
    assert after["usage"] == before["usage"] and after["limits"] == before["limits"]
    assert after["lanes"] == before["lanes"]
    assert len(after["forecast_errors"]) == 1
    assert test_db.is_active and await test_db.scalar(text("SELECT 1")) == 1
    await test_db.commit()
    assert (await test_db.get(SystemSetting, marker.key)).value == "must-survive"
    if exhausted:
        assert after["state"] == "budget_wait" and after["lanes"]["ad"]["remaining"] == 0
        with pytest.raises(rpc.RpcDeferred):
            await rpc.check_read_ready(test_db, 2, now, purpose="ad_delivery")


async def test_failed_business_flush_is_not_treated_as_an_optional_forecast_error(test_db):
    test_db.add(GroupAdFrequency(telegram_group_id=42, quota=0))
    with pytest.raises(IntegrityError):
        await apply_forecast(test_db, 2, datetime.utcnow(), "test", AsyncMock(), {}, {})
    assert not test_db.is_active
    await test_db.rollback()


async def test_thirty_accounts_dispatch_only_actionable_join_reconciliation(test_db):
    await add_accounts(test_db)
    now = datetime.utcnow()
    for account_id in range(1, 31):
        test_db.add(AutoJoinAttempt(
            account_id=account_id, target_key=f"username:test{account_id}",
            request_state="sent", status="success", reconciliation_status="pending",
        ))
    for account_id, changes in [
        (2, {"request_state": "sent", "status": "pending"}),
        (3, {"request_state": "outcome_unknown", "status": "failed"}),
        (4, {"request_state": "sent", "status": "pending", "reconciliation_next_at": now + timedelta(minutes=15)}),
        (5, {"request_state": "outcome_unknown", "status": "pending", "target_key": None}),
        (6, {"request_state": "outcome_unknown", "status": "pending", "reconciliation_status": "confirmed"}),
    ]:
        test_db.add(AutoJoinAttempt(
            account_id=account_id, **{"target_key": f"username:due{account_id}",
                                    "reconciliation_status": "pending", **changes},
        ))
    await test_db.commit()
    stages = await scheduler.account_sets(test_db, now)
    assert set(stages["reconcile"]) == {2, 3}
    assert not stages["exit"] and not stages["verify"]


async def test_maintenance_dispatch_preserves_frequency_and_stale_membership_exits(test_db):
    account, group, member, audit = await setup(test_db, decision="observe")
    now = datetime.utcnow()
    member.joined_at = audit.membership_joined_at = now - timedelta(hours=1)
    config = await test_db.get(SystemSetting, "automation.group_qualification")
    config.value = json.dumps({
        "enabled": True, "execute_exits": True, "execute_verification": True,
        "exit_all_accounts": True,
    })
    log = AdDeliveryLog(account_id=account.id, group_id=group.id, telegram_group_id=group.group_id,
                        ad_campaign_id=1, status="success")
    test_db.add(log)
    await test_db.flush()
    test_db.add_all([
        GroupAdFrequency(telegram_group_id=group.group_id, status="exit_pending"),
        GroupAdFrequencyEvent(telegram_group_id=group.group_id, log_id=log.id, kind="sent",
                              epoch=1, old_quota=1, new_quota=1),
    ])
    await test_db.commit()
    stages = await scheduler.account_sets(test_db, now)
    assert stages["exit"] == [account.id] and stages["verify"] == [account.id]
    member.leave_retry_at = now + timedelta(hours=1)
    member.joined_at = now - timedelta(days=3)
    await test_db.commit()
    stages = await scheduler.account_sets(test_db, now)
    assert not stages["exit"] and not stages["verify"]
    member.status, member.review_status = "left", "membership_reconciliation"
    member.ad_status = "blocked"
    member.review_next_at = now
    await test_db.commit()
    assert (await scheduler.account_sets(test_db, now))["exit"] == [account.id]


async def test_outbound_budget_blocks_are_pre_request_deferrals():
    from app.core.account.outbound_budget import OutboundBudgetBlocked

    blocked = OutboundBudgetBlocked("outbound_ad_interval", 344)
    assert rpc.deferred_error(blocked) == ("outbound_ad_interval", 344)
    # The classified string form must stay recognizable for metrics.
    assert rpc.deferred_error("outbound_ad_interval: retry_after_seconds=344") == (
        "outbound_ad_interval", 344,
    )
    # A send whose outcome is unknown must never be reclassified as a wait.
    assert rpc.deferred_error("send_outcome_unknown:timeout") is None


async def test_stale_pending_without_ledger_attempt_is_closed(test_db):
    account, group, _, _ = await setup(test_db)
    now = datetime.utcnow()
    orphan = AdDeliveryLog(
        account_id=account.id, group_id=group.id, telegram_group_id=group.group_id,
        ad_campaign_id=1, status="pending", created_at=now - timedelta(hours=2),
        reservation_token="orphan-token", error=None, telegram_message_id=None,
    )
    live = AdDeliveryLog(
        account_id=account.id, group_id=group.id, telegram_group_id=group.group_id,
        ad_campaign_id=1, status="pending", created_at=now - timedelta(minutes=2),
        reservation_token="fresh-token", error=None, telegram_message_id=None,
    )
    fenced = AdDeliveryLog(
        account_id=account.id, group_id=group.id, telegram_group_id=group.group_id,
        ad_campaign_id=1, status="pending", created_at=now - timedelta(hours=2),
        reservation_token="fenced-token", error=None, telegram_message_id=None,
    )
    test_db.add_all([orphan, live, fenced])
    await test_db.flush()
    test_db.add(AccountOutboundAttempt(
        account_id=account.id, attempt_key="ad:fenced-token", category="ad",
        target_key="group:x", state="reserved",
    ))
    await test_db.commit()

    result = await recover_missing_receipts(test_db, now, account.id, 10)

    assert result["abandoned_pending_closed"] == 1
    await test_db.refresh(orphan)
    await test_db.refresh(live)
    await test_db.refresh(fenced)
    assert orphan.status == "failed"
    assert orphan.error.startswith("send_abandoned_before_attempt")
    assert json.loads(orphan.qualification_context_json)["send_reconciliation"][
        "verification_terminal"
    ] is True
    # A fresh reservation and one with a live ledger fence stay untouched.
    assert live.status == "pending" and fenced.status == "pending"
