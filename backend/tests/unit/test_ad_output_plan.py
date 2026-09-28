import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.account.rpc_governor import limits_for
from app.modules.acquisition.ad_output_plan import (
    ad_output_plan,
    delivery_priority,
    expansion_plan,
    output_metrics,
    resource_capacity,
)
from app.modules.acquisition.capacity import inventory_snapshot
from app.modules.acquisition.models import AdCampaign, AdDeliveryLog, GroupAdFrequency

NOW = datetime(2026, 9, 27, 14)


@pytest.mark.parametrize("factor,expected", [(0.5, 4), (0.6, 5), (1, 9)])
def test_plan_leaves_room_for_listener_and_maintenance(factor, expected):
    limits = limits_for({"read_factor": factor}, NOW)
    assert resource_capacity(144, limits) == expected
    assert expected * 30 <= (limits["non_ad_day"] - limits["sync_day"]) * 0.8
    assert resource_capacity(2, limits) == 2


def plan(**overrides):
    args = {
        "capacity": 9,
        "inventory": {"usable": 10, "slots_24h": 10, "active_backlog": 0},
        "ad_due": 0,
        "survival_due": 0,
        "unresolved": 0,
        "unavailable_reason": None,
        "material_count": 1,
    }
    args.update(overrides)
    return expansion_plan(**args)


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"ad_due": 1}, "join_wait_ad_delivery"),
        ({"survival_due": 1, "ad_due": 1}, "join_wait_ad_survival"),
        ({"unresolved": 1, "survival_due": 1}, "join_wait_ad_reconciliation"),
        ({"material_count": 0}, "join_ad_material_missing"),
        ({"unavailable_reason": "account_risk_quarantined"}, "account_risk_quarantined"),
        ({"capacity": 0}, "join_ad_capacity_unavailable"),
    ],
)
def test_new_group_work_yields_to_existing_output(overrides, reason):
    assert plan(**overrides)["join_blocker"] == reason


def test_backlog_and_mature_frequency_reduce_new_group_demand():
    assert plan()["target_groups"] == 18
    assert plan()["group_deficit"] == 8
    covered = plan(inventory={"usable": 4, "slots_24h": 20, "active_backlog": 0})
    assert covered["target_groups"] == 4
    assert covered["join_blocker"] == "join_inventory_target_met"
    covered = plan(inventory={"usable": 10, "slots_24h": 10, "active_backlog": 8})
    assert covered["group_deficit"] == 0
    assert covered["join_blocker"] == "join_wait_inventory_review"
    assert covered["qualified_group_deficit"] == 8


def test_resource_plan_uses_independent_survival_budget_and_measured_costs():
    limits = limits_for({}, NOW)
    baseline = resource_capacity(144, limits)
    assert resource_capacity(144, {**limits, 'sync_day': 999999}) == baseline
    assert resource_capacity(144, {**limits, 'survival_day': 0}) == 0
    assert resource_capacity(144, limits, delivery_cost=100, survival_cost=90) < baseline


def test_quality_priority_ages_waiting_targets_without_starvation():
    def rank(**kw):
        args = {
            "due": NOW,
            "last_sent": NOW - timedelta(days=2),
            "survived": 1,
            "deleted": 0,
            "now": NOW,
            "identity": 1,
        }
        args.update(kw)
        return delivery_priority(**args)

    assert rank() < rank(last_sent=None, survived=0)
    assert rank(last_sent=None, survived=0, due=NOW - timedelta(days=2)) < rank()
    assert rank(deleted=1) > rank()


@pytest.mark.asyncio
async def test_usable_inventory_excludes_frequency_exit_and_unknown_namespace(test_db):
    from tests.unit.test_qualification_service import setup

    account, group, member, review = await setup(test_db)
    now = datetime.utcnow()
    initial = await inventory_snapshot(test_db, account.id, now)
    assert initial["qualified"] == initial["usable"] == initial["slots_24h"] == 1
    state = GroupAdFrequency(
        telegram_group_id=-1000000000000 - group.group_id,
        quota=1,
        status="exit_pending",
        mature=False,
    )
    test_db.add(state)
    await test_db.commit()
    closed = await inventory_snapshot(test_db, account.id, now)
    assert closed["qualified"] == 1 and closed["usable"] == closed["slots_24h"] == 0
    await test_db.delete(state)
    evidence = json.loads(review.evidence_json)
    evidence.pop("group_type")
    review.evidence_json = json.dumps(evidence)
    await test_db.commit()
    unknown = await inventory_snapshot(test_db, account.id, now)
    assert unknown["usable"] == 0


@pytest.mark.asyncio
async def test_metrics_keep_recent_and_completed_age_cohorts_separate(test_db):
    from tests.unit.test_qualification_service import setup

    account, group, _, _ = await setup(test_db)
    campaign = AdCampaign(name="metrics", enabled=True)
    test_db.add(campaign)
    await test_db.flush()
    for mid, age, state, confirmed in [
        (1, 2, "pending", False),
        (2, 26, "survived", True),
        (3, 28, "check_failed", False),
        (4, 25, "deleted", False),
    ]:
        test_db.add(
            AdDeliveryLog(
                account_id=account.id,
                group_id=group.id,
                telegram_group_id=group.group_id,
                ad_campaign_id=campaign.id,
                status="success",
                telegram_message_id=mid,
                sent_at=NOW - timedelta(hours=age),
                survival_status=state,
                survived_one_hour_at=NOW - timedelta(hours=1),
                survived_twenty_four_hour_at=NOW - timedelta(hours=1) if confirmed else None,
            )
        )
    await test_db.commit()
    metrics = await output_metrics(test_db, account.id, NOW)
    assert {k:v for k,v in metrics.items() if not k.startswith("survival_")} == {
        "sent_24h": 1,
        "confirmed_1h": 1,
        "deleted": 0,
        "unresolved": 1,
        "matured_sends_72h": 3,
        "confirmed_24h": 1,
        "matured_unresolved": 1,
    }


@pytest.mark.asyncio
async def test_plan_does_not_call_telegram_and_blocks_due_survival(test_db):
    from tests.unit.test_qualification_service import setup

    account, group, _, _ = await setup(test_db)
    campaign = AdCampaign(name="priority", enabled=True)
    test_db.add(campaign)
    await test_db.flush()
    test_db.add(
        AdDeliveryLog(
            account_id=account.id,
            group_id=group.id,
            telegram_group_id=group.group_id,
            ad_campaign_id=campaign.id,
            status="success",
            telegram_message_id=1,
            sent_at=NOW - timedelta(minutes=10),
            survival_status="pending",
            survival_check_due_at=NOW - timedelta(minutes=8),
        )
    )
    await test_db.commit()
    result = await ad_output_plan(
        test_db,
        account,
        SimpleNamespace(enabled=True, auto_ads_enabled=True),
        NOW,
        rpc={"state": "ready", "limits": limits_for({}, NOW)},
        limits={"ad": 144},
        inventory={"usable": 1, "slots_24h": 1, "active_backlog": 0},
        workload={"ad_targets": 1, "material_count": 1, "blocker_counts": {}},
    )
    assert result["join_blocker"] == "join_wait_ad_survival"
    assert result["target_groups"] == 18


@pytest.mark.asyncio
async def test_join_actual_send_boundary_rechecks_ad_priority(test_db, monkeypatch):
    from app.core.automation_settings import save_auto_join_scheduler_settings
    from app.modules.acquisition import ad_output_plan as module
    from app.modules.acquisition.join_budget import JoinBudgetBlocked, JoinRequestBudgetService
    from tests.unit.test_dynamic_outbound_capacity import account_config

    account, config = await account_config(test_db)
    await save_auto_join_scheduler_settings(test_db, {"enabled": True})
    stub = AsyncMock(return_value={"join_blocker": None, "group_deficit": 18})
    monkeypatch.setattr(module, "ad_output_plan", stub)
    service = JoinRequestBudgetService(test_db)
    row = await service.reserve(
        account.id, target_key="username:authorized_group", source="test", now=NOW
    )
    stub.return_value = {"join_blocker": "join_wait_ad_survival", "group_deficit": 18}
    with pytest.raises(JoinBudgetBlocked, match="join_wait_ad_survival"):
        await service.mark_sent(row, now=NOW)
    assert row.request_state == "reserved" and row.telegram_action_attempted is False


@pytest.mark.asyncio
async def test_inventory_excludes_manual_hold_and_obsolete_policy(test_db):
    from app.modules.acquisition.models import GroupAdProfile
    from tests.unit.test_qualification_service import setup

    account, group, member, review = await setup(test_db)
    now = datetime.utcnow()
    profile = GroupAdProfile(
        group_id=group.id,
        telegram_group_id=group.group_id,
        ad_policy_source="manual",
        ad_policy_mode="forbidden",
    )
    test_db.add(profile)
    await test_db.commit()
    assert (await inventory_snapshot(test_db, account.id, now))["usable"] == 0
    await test_db.delete(profile)
    review.policy_version = "obsolete"
    await test_db.commit()
    assert (await inventory_snapshot(test_db, account.id, now))["usable"] == 0


@pytest.mark.asyncio
async def test_priority_query_keeps_current_membership_epoch(test_db):
    from app.modules.acquisition.ad_output_plan import prioritize_deliveries
    from tests.unit.test_qualification_service import setup

    account, group, member, _ = await setup(test_db)
    now = datetime.utcnow()
    campaign = AdCampaign(name="sort", enabled=True)
    test_db.add(campaign)
    await test_db.flush()
    test_db.add(
        AdDeliveryLog(
            account_id=account.id,
            group_id=group.id,
            telegram_group_id=group.group_id,
            ad_campaign_id=campaign.id,
            status="success",
            telegram_message_id=1,
            sent_at=member.joined_at - timedelta(days=1),
            survival_status="deleted",
        )
    )
    await test_db.commit()
    assert await prioritize_deliveries(test_db, [member], campaign.id, now) == [member]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state,reason",
    [("quarantined", "account_risk_quarantined"), ("normal", "join_ad_delivery_paused")],
)
async def test_global_ad_pause_prevents_expansion(test_db, state, reason):
    from tests.unit.test_qualification_service import setup

    account, _, _, _ = await setup(test_db)
    account.risk_level = state
    await test_db.flush()
    result = await ad_output_plan(
        test_db,
        account,
        SimpleNamespace(enabled=True, auto_ads_enabled=True),
        NOW,
        rpc={"state": "ready", "limits": limits_for({}, NOW)},
        limits={"ad": 144},
        inventory={"usable": 1, "slots_24h": 1, "active_backlog": 0},
        workload={
            "ad_targets": 0,
            "material_count": 1,
            "blocker_counts": {"ad_time_window_blocked": 1},
        },
    )
    assert result["join_blocker"] == reason
