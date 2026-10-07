from datetime import datetime, timedelta

import pytest

from app.core.account import rpc_governor as rpc
from app.modules.acquisition.survival_schedule import backlog
from tests.unit.test_ad_read_reserve import mock_usage


@pytest.mark.parametrize("factor", [1, 0.6, 0.5, 0.125])
def test_reserves_and_other_headroom_sum_to_unchanged_total(factor):
    limits = rpc.limits_for({"read_factor": factor}, datetime.utcnow())
    for window in ("hour", "day"):
        assert (
            limits["ad_" + window] + limits["survival_" + window] + limits["non_survival_" + window]
            == limits[window]
        )
        assert limits["survival_" + window] * 100 >= limits[window] * 15
        assert limits["ad_" + window] * 100 >= limits[window] * 35


@pytest.mark.asyncio
async def test_old_maintenance_exhaustion_does_not_consume_new_survival_reserve(
    monkeypatch, test_db
):
    mock_usage(
        monkeypatch, {"hour": (120, 1800), "day": (1200, 60000), "critical_hour": (60, 1800)}
    )
    current = await rpc.check_read_ready(
        test_db, 3, purpose="ad_survival_check", requires_bootstrap=True
    )
    assert current["lanes"]["survival"]["remaining"] == 31
    assert current["lanes"]["ad"]["remaining"] == 84
    for purpose in [
        "group_qualification",
        "join_candidate_preview",
        "growth_event",
    ]:
        with pytest.raises(rpc.RpcDeferred):
            await rpc.check_read_ready(test_db, 3, purpose=purpose)


@pytest.mark.asyncio
async def test_spent_survival_does_not_spend_ad_or_other_share(monkeypatch, test_db):
    mock_usage(
        monkeypatch,
        {
            "hour": (36, 1800),
            "day": (360, 60000),
            "survival_hour": (36, 1800),
            "survival_day": (360, 60000),
        },
    )
    borrowed = await rpc.check_read_ready(test_db, 3, purpose="ad_survival_check")
    assert borrowed["lanes"]["survival"]["remaining"] == 7
    state = await rpc.check_read_ready(test_db, 3, purpose="ad_delivery")
    assert state["usage"]["non_survival_hour"]["used"] == 0
    assert state["lanes"]["critical"]["remaining"] == 72


@pytest.mark.asyncio
async def test_retry_deadline_never_hides_overdue_checkpoint(test_db):
    from app.modules.acquisition.automation import AcquisitionAutomationService
    from app.modules.acquisition.models import AdCampaign, AdDeliveryLog
    from tests.unit.test_qualification_service import setup

    account, group, *_ = await setup(test_db)
    now = datetime.utcnow()
    campaign = AdCampaign(name="overdue", enabled=True)
    test_db.add(campaign)
    await test_db.flush()
    log = AdDeliveryLog(
        account_id=account.id,
        group_id=group.id,
        ad_campaign_id=campaign.id,
        telegram_group_id=group.group_id,
        status="success",
        telegram_message_id=1,
        sent_at=now - timedelta(minutes=10),
        survival_status="pending",
        survival_stage="two_minute",
        survival_check_due_at=now + timedelta(hours=1),
        survival_error="telegram_read_budget",
    )
    test_db.add(log)
    await test_db.commit()
    metrics = await backlog(test_db, account.id, now)
    assert metrics["survival_overdue"] == metrics["survival_deferred_overdue"] == 1
    assert metrics["survival_oldest_overdue_seconds"] == 480
    service = AcquisitionAutomationService(test_db)
    assert await service._claim_survival_check(log.id, now) is not None


@pytest.mark.asyncio
async def test_platform_cooldown_retry_is_not_pulled_forward(test_db):
    from app.modules.acquisition.automation import AcquisitionAutomationService
    from app.modules.acquisition.models import AdCampaign, AdDeliveryLog
    from tests.unit.test_qualification_service import setup

    account, group, *_ = await setup(test_db)
    now = datetime.utcnow()
    campaign = AdCampaign(name="cooldown", enabled=True)
    test_db.add(campaign)
    await test_db.flush()
    log = AdDeliveryLog(
        account_id=account.id,
        group_id=group.id,
        ad_campaign_id=campaign.id,
        telegram_group_id=group.group_id,
        status="success",
        telegram_message_id=2,
        sent_at=now - timedelta(minutes=10),
        survival_status="pending",
        survival_stage="two_minute",
        survival_check_due_at=now + timedelta(hours=1),
        survival_error="telegram_rpc_cooldown",
    )
    test_db.add(log)
    await test_db.commit()
    assert await AcquisitionAutomationService(test_db)._claim_survival_check(log.id, now) is None
