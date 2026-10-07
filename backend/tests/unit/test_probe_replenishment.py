import json
from datetime import datetime, timedelta

import pytest

from app.core.account.models import AccountOperationConfig, AccountOutboundAttempt
from app.core.account.outbound_budget import AccountOutboundBudgetService, OutboundBudgetBlocked
from app.core.account.rpc_governor import limits_for
from app.modules.acquisition.ad_output_plan import ad_output_plan, pending_trial_work
from app.modules.acquisition.models import AdDeliveryLog, GroupAdFrequency
from tests.unit.test_adaptive_group_frequency import NOW, TARGET, setup

pytestmark = pytest.mark.asyncio


async def test_only_confirmed_trial_success_spends_thirty_quota(test_db):
    a, _, _, _, _ = await setup(test_db)
    for i, status in enumerate(
        ["succeeded", "succeeded", "reserved", "attempted", "unknown", "failed", "cancelled"]
    ):
        test_db.add(
            AccountOutboundAttempt(
                account_id=a.id,
                attempt_key=f"trial-{i}",
                category="ad",
                target_key=str(TARGET),
                state=status,
                attempted_at=NOW - timedelta(hours=1) if status != "reserved" else None,
                lease_expires_at=NOW + timedelta(minutes=1) if status == "reserved" else None,
                context_json=json.dumps({"frequency": {"lane": "probe"}}),
            )
        )
    test_db.add(
        AccountOutboundAttempt(
            account_id=a.id,
            attempt_key="mature",
            category="ad",
            target_key="mature",
            state="succeeded",
            attempted_at=NOW - timedelta(hours=1),
            context_json=json.dumps({"frequency": {"lane": "mature"}}),
        )
    )
    await test_db.commit()
    service = AccountOutboundBudgetService(test_db)
    result = await service.snapshot(a.id, NOW)
    probe = result["ad_lanes"]["probe"]
    assert probe["used_today"] == probe["used_rolling_24h"] == 2  # Same old group may count twice.
    assert probe["remaining"] == 28 and probe["inflight"] == 3
    assert result["unknown_count"] == 2
    with pytest.raises(OutboundBudgetBlocked, match="outbound_target_reconciliation_required"):
        await service._assert_target_resolved(a.id, "ad", str(TARGET), exclude_key="new")


async def test_unknown_reconciliation_counts_once_without_resending(test_db):
    a, _, _, _, _ = await setup(test_db)
    row = AccountOutboundAttempt(
        account_id=a.id,
        attempt_key="unknown",
        category="ad",
        target_key=str(TARGET),
        state="unknown",
        attempted_at=NOW - timedelta(hours=1),
        context_json="{}",
    )
    test_db.add(row)
    await test_db.commit()
    service = AccountOutboundBudgetService(test_db)
    assert (await service.snapshot(a.id, NOW))["ad_lanes"]["probe"]["used_today"] == 0
    with pytest.raises(OutboundBudgetBlocked, match="outbound_reconciliation_required"):
        await service.finish("unknown", account_id=a.id, state="succeeded", message_id=19, now=NOW)
    for _ in range(2):
        await service.reconcile(
            "unknown", account_id=a.id, state="succeeded", message_id=19, confirmed=True, now=NOW
        )
    assert (await service.snapshot(a.id, NOW))["ad_lanes"]["probe"]["used_today"] == 1


async def test_pending_trial_does_not_hide_remaining_quota_when_shared_budget_spent(test_db):
    a, _, _, _, _ = await setup(test_db)
    for i in range(144):
        test_db.add(
            AccountOutboundAttempt(
                account_id=a.id,
                attempt_key=f"mature-{i}",
                category="ad",
                target_key=str(i),
                state="succeeded",
                attempted_at=NOW - timedelta(hours=1),
                context_json=json.dumps({"frequency": {"lane": "mature"}}),
            )
        )
    await test_db.commit()
    result = await AccountOutboundBudgetService(test_db).snapshot(a.id, NOW)
    assert result["ad_lanes"]["probe"]["remaining"] == 30
    assert result["ad_lanes"]["probe"]["executable_remaining"] == 0


@pytest.mark.parametrize("status", ["success", "unknown"])
async def test_mature_send_and_survival_work_do_not_pause_new_group_plan(test_db, status):
    a, c, campaign, _, state = await setup(test_db, quota=4, mature=True)
    test_db.add(
        AdDeliveryLog(
            account_id=a.id,
            ad_campaign_id=campaign.id,
            telegram_group_id=TARGET,
            status=status,
            telegram_message_id=123 if status == "success" else None,
            sent_at=NOW - timedelta(minutes=10) if status == "success" else None,
            survival_status="pending",
            survival_stage="two_minute",
            survival_check_due_at=NOW - timedelta(minutes=8),
            qualification_context_json=json.dumps({"group_type": "supergroup"}),
        )
    )
    await test_db.commit()
    result = await ad_output_plan(
        test_db,
        a,
        c,
        NOW,
        inventory={
            "total": 30,
            "usable": 30,
            "probe_usable": 0,
            "mature_usable": 30,
            "slots_24h": 120,
            "active_backlog": 0,
        },
        limits={"ad": 144, "ad_probe": 30},
        rpc={"state": "ready", "limits": limits_for({}, NOW)},
        workload={
            "ad_targets": 30,
            "probe_ad_targets": 0,
            "material_count": 1,
            "blocker_counts": {},
        },
    )
    assert result["group_deficit"] == 30
    assert result["probe_survival_due"] == result["probe_unresolved_sends"] == 0
    assert result["join_blocker"] is None
    assert result["survival_due"] + result["unresolved_sends"] == 1
    state.mature = False
    await test_db.commit()
    split = await pending_trial_work(test_db, a.id, NOW, {})
    assert split["probe_survival_due"] + split["probe_unresolved"] == 1


async def test_current_maturity_splits_usable_inventory_and_review_backlog(test_db):
    from app.modules.acquisition.capacity import inventory_snapshot
    from app.modules.acquisition.join_budget import JoinRequestBudgetService
    from tests.unit.test_qualification_service import setup as qualification_setup

    a, g, m, q = await qualification_setup(test_db)
    test_db.add(AccountOperationConfig(account_id=a.id, dynamic_capacity_enabled=True))
    state = GroupAdFrequency(
        telegram_group_id=-1000000000000 - g.group_id, quota=2, mature=True, status="active"
    )
    test_db.add(state)
    await test_db.commit()
    inventory = await inventory_snapshot(test_db, a.id)
    assert inventory["mature_usable"] == 1 and inventory["probe_usable"] == 0
    q.decision = "technical_wait"
    q.state = "completed"
    q.next_retry_at = datetime.utcnow() + timedelta(hours=1)
    q.expires_at = datetime.utcnow() - timedelta(hours=1)
    m.review_status = "review_2h"
    m.ad_status = "warming"
    await test_db.commit()
    inventory = await inventory_snapshot(test_db, a.id)
    assert inventory["active_backlog"] == 1 and inventory["probe_active_backlog"] == 0
    assert await JoinRequestBudgetService(test_db)._review_backlog(a.id, datetime.utcnow()) == (
        0,
        0,
    )
