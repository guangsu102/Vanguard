from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from app.core.account import rpc_governor as rpc
from app.core.account.reserved_reads import allocations, available, lending_caps, transferred_caps
from app.core.account.survival_lending import survival_holds
from tests.unit.test_ad_read_reserve import mock_usage
from tests.unit.test_adaptive_group_frequency import NOW
from tests.unit.test_event_cycle_survival import scenario


@pytest.mark.parametrize("total", [30, 84, 168, 240, 1680, 2400])
def test_lending_never_changes_total_ad_or_sync_reservations(total):
    caps = allocations(total)
    used = [0, 0, caps[2] + caps[4], caps[3], 0]
    hold = (total * 5 + 99) // 100
    after = lending_caps(caps, used, "critical", hold)
    loan = caps[1] - after[1]
    assert sum(after) == total and after[0] == caps[0] and after[3] == caps[3]
    assert available(total, used, after, "critical") == loan
    used[2] += loan
    settled = transferred_caps(caps, loan)
    assert available(total, used, settled, "ad") == caps[0]
    assert available(total, used, settled, "survival") == hold
    assert available(total, used, settled, "critical") == 0
    for lane in ["ad", "survival", "sync", "routine", "background"]:
        assert lending_caps(caps, used, lane, hold) == caps


@pytest.mark.asyncio
async def test_idle_trial_capacity_can_unblock_review_without_resetting_totals(
    test_db, monkeypatch
):
    account, _, _, state, _, _ = await scenario(test_db)
    mock_usage(monkeypatch, {"hour": (120, 1800), "day": (1200, 60000)})
    snapshot = await rpc.check_read_ready(
        test_db, account.id, now=NOW, purpose="group_qualification"
    )
    assert snapshot["lanes"]["critical"]["remaining"] == 26
    assert snapshot["lanes"]["ad"]["remaining"] == 84
    assert snapshot["usage"]["hour"]["used"] == 120
    assert snapshot["usage"]["hour"]["ttl_seconds"] == 1800
    assert snapshot["survival_lending"]["holds"] == {"hour": 10, "day": 84}


@pytest.mark.asyncio
async def test_due_checks_and_buffer_are_reserved_before_any_loan(test_db, monkeypatch):
    account, _, _, state, _, _ = await scenario(test_db)
    state.daily_review_due_at = NOW
    await test_db.commit()
    mock_usage(monkeypatch, {"hour": (120, 1800), "day": (1200, 60000)})
    snapshot = await rpc.snapshot(test_db, account.id, NOW)
    assert snapshot["survival_lending"]["holds"] == {"hour": 20, "day": 94}
    assert snapshot["lanes"]["critical"]["remaining"] == 16


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "blocker", ["uncertain", "unknown_error", "reader", "legacy", "disabled", "error"]
)
async def test_uncertain_or_active_survival_work_disables_new_lending(
    test_db, monkeypatch, blocker
):
    account, config, _, state, _, log = await scenario(test_db)
    if blocker == "uncertain":
        log.status = "reconciliation_required"
    elif blocker == "unknown_error":
        log.status, log.error = "failed", "send_outcome_unknown:TimeoutError"
    elif blocker == "reader":
        state.daily_review_expires_at = NOW + timedelta(minutes=3)
    elif blocker == "legacy":
        log.survival_status, log.survival_stage = "pending", "two_minute"
    elif blocker == "disabled":
        config.adaptive_ads_enabled = False
    else:
        state.daily_review_error = "message_missing_cause_unconfirmed"
    await test_db.commit()
    mock_usage(monkeypatch, {"hour": (120, 1800), "day": (1200, 60000)})
    with pytest.raises(rpc.RpcDeferred):
        await rpc.check_read_ready(test_db, account.id, now=NOW, purpose="group_qualification")


@pytest.mark.asyncio
async def test_existing_loan_does_not_get_reinstated_and_steal_ad_headroom(test_db, monkeypatch):
    account, _, _, _, _, _ = await scenario(test_db)
    mock_usage(
        monkeypatch,
        {
            "hour": (144, 1800),
            "day": (1440, 60000),
            "join_reserved_hour": (84, 1800),
            "join_reserved_day": (840, 60000),
            "sync_reserved_hour": (48, 1800),
            "sync_reserved_day": (480, 60000),
            "flex_reserved_hour": (12, 1800),
            "flex_reserved_day": (120, 60000),
            "survival_lent_hour": (24, 1800),
            "survival_lent_day": (240, 60000),
        },
    )
    snapshot = await rpc.snapshot(test_db, account.id, NOW)
    assert snapshot["lanes"]["ad"]["remaining"] == 84
    assert snapshot["lanes"]["survival"]["remaining"] == 7
    assert snapshot["lanes"]["critical"]["remaining"] == 2
    assert snapshot["usage"]["survival_hour"]["guaranteed_limit"] == 12
    assert snapshot["usage"]["critical_hour"]["guaranteed_limit"] == 84


@pytest.mark.asyncio
async def test_legacy_missing_cycle_deadline_is_resolved_before_lending(test_db, monkeypatch):
    account, _, _, state, _, _ = await scenario(test_db)
    state.daily_review_due_at = None
    await test_db.commit()
    monkeypatch.setattr(
        "app.modules.acquisition.read_costs.operation_costs",
        AsyncMock(return_value={"daily_review_read_cost": 10}),
    )
    limits = rpc.limits_for({}, NOW)
    budget = {"usage": {w: {"ttl_seconds": -2} for w in ("hour", "day")}}
    holds = await survival_holds(test_db, account.id, NOW, limits, budget)
    assert holds == {"hour": 12, "day": 130}
