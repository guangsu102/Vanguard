"""Production legacy positive peer IDs require the verified qualification namespace."""

import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.core.account.models import AccountOperationConfig
from app.modules.acquisition.automation import AcquisitionAutomationService
from app.modules.acquisition.capacity import executable_inventory
from app.modules.acquisition.capacity_reads import CapacityReads
from app.modules.acquisition.models import (
    AccountAdBinding, AdCampaign, AdCreative, AdDeliveryLog, AdDeliveryScheduleState,
)
from tests.unit.test_ad_read_reserve import mock_usage
from tests.unit.test_qualification_service import setup

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def available_budget(monkeypatch):
    cache = mock_usage(monkeypatch, {})
    cache.get = AsyncMock(return_value=None)
    cache.lrange = AsyncMock(return_value=[])


@pytest.mark.parametrize("known_identity", [True, False])
@pytest.mark.parametrize("material_count", [1, 2])
@pytest.mark.parametrize("use_projection", [True, False])
@pytest.mark.parametrize("schedule_mode", ["absent", "earlier", "later", "paused", "sending"])
async def test_capacity_keeps_verified_namespace_for_frequency_deadline(
    test_db, monkeypatch, known_identity, material_count, use_projection, schedule_mode
):
    account, group, member, review = await setup(test_db)
    # This regression concerns peer identity/frequency, not time-window gating.
    now = datetime.utcnow().replace(hour=12, minute=0, second=0, microsecond=0)
    review.checked_at = now
    review.expires_at = now + timedelta(days=1)
    config = AccountOperationConfig(
        account_id=account.id,
        enabled=True,
        auto_ads_enabled=True,
        dynamic_capacity_enabled=True,
        adaptive_ads_enabled=True,
        operation_mode="growth",
    )
    creative = AdCreative(
        name="profile", content="Details in profile", creative_type="text", enabled=True
    )
    campaign = AdCampaign(
        name="growth", enabled=True, delivery_policy="growth", send_mode="interval"
    )
    test_db.add_all([config, creative, campaign])
    await test_db.flush()
    test_db.add(
        AccountAdBinding(
            account_id=account.id, ad_campaign_id=campaign.id, creative_id=creative.id, enabled=True
        )
    )
    campaigns = [campaign]
    if material_count == 2:
        second_campaign = AdCampaign(name="second-growth", enabled=True,
                                     delivery_policy="growth", send_mode="interval")
        test_db.add(second_campaign)
        await test_db.flush()
        campaigns.append(second_campaign)
        test_db.add(AccountAdBinding(account_id=account.id, ad_campaign_id=second_campaign.id,
                                   creative_id=creative.id, enabled=True))
    sent = now - timedelta(hours=2)
    for index, item in enumerate(campaigns):
        if schedule_mode != "absent":
            test_db.add(AdDeliveryScheduleState(
                campaign_id=item.id, account_id=account.id, group_id=group.id,
                telegram_group_id=group.group_id,
                status=schedule_mode if schedule_mode in {"paused", "sending"} else "idle",
                next_due_at=now + timedelta(hours=(4 if schedule_mode == "earlier" else 30) + index),
                lease_expires_at=now + timedelta(hours=25) if schedule_mode == "sending" else None,
            ))
    test_db.add(
        AdDeliveryLog(
            account_id=account.id,
            group_id=group.id,
            telegram_group_id=group.group_id,
            ad_campaign_id=campaign.id,
            status="success",
            telegram_message_id=42,
            sent_at=sent,
            created_at=sent,
            survival_status="pending",
            survival_stage="twenty_four_hour",
            survival_check_due_at=sent + timedelta(days=1),
            qualification_context_json=json.dumps({"group_type": "supergroup"}),
        )
    )
    if not known_identity:
        evidence = json.loads(review.evidence_json)
        evidence.pop("group_type", None)
        review.evidence_json = json.dumps(evidence)
    await test_db.commit()
    monkeypatch.setattr(
        AcquisitionAutomationService,
        "_get_ad_delivery_cooldown_until",
        AsyncMock(return_value=None),
    )
    reader = CapacityReads(test_db) if use_projection else test_db
    result = await executable_inventory(reader, account.id, config, now)
    assert result["ad_targets"] == 0
    if known_identity:
        expected = sent + timedelta(days=1)
        if schedule_mode in {"later", "sending"}:
            expected = now + timedelta(hours=30)
        if schedule_mode == "paused":
            expected = None
        assert result["ad_next_allowed_at"] == expected
        assert result["blocker_counts"]["frequency_group_daily_cap"] == material_count
    else:
        assert result["ad_next_allowed_at"] is None
        assert result["blocker_counts"]["qualification_group_identity_unknown"] == material_count
