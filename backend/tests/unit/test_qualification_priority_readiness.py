"""Unsendable queued ads must not starve evidence refresh for an account."""

import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.core.account.models import AccountOperationConfig
from app.modules.acquisition import qualification_service as qualification
from app.modules.acquisition.automation import AcquisitionAutomationService
from app.modules.acquisition.models import AdCampaign, AdDeliveryLog, AdDeliveryScheduleState
from tests.unit.test_qualification_service import setup


async def due_ad(db):
    account, group, member, review = await setup(db)
    now = datetime.utcnow().replace(minute=1)
    review.checked_at = now - timedelta(minutes=1)
    review.expires_at = now + timedelta(hours=1)
    campaign = AdCampaign(name="priority", enabled=True)
    db.add(campaign)
    await db.flush()
    db.add(
        AccountOperationConfig(
            account_id=account.id,
            enabled=True,
            auto_ads_enabled=True,
            auto_join_enabled=False,
            dynamic_capacity_enabled=True,
            adaptive_ads_enabled=True,
        )
    )
    db.add(
        AdDeliveryScheduleState(
            campaign_id=campaign.id,
            account_id=account.id,
            group_id=group.id,
            telegram_group_id=group.group_id,
            next_due_at=now - timedelta(hours=12),
            status="retry",
        )
    )
    await db.commit()
    return account, group, review, now


@pytest.mark.parametrize(
    "reason",
    [
        "qualification_group_identity_unknown",
        "frequency_group_daily_cap",
        "frequency_survival_unresolved",
        "outbound_ad_probe_budget",
        None,
    ],
)
async def test_only_ready_group_preserves_ad_priority(test_db, monkeypatch, reason):
    account, group, _, now = await due_ad(test_db)
    gate = AsyncMock(return_value=reason)
    monkeypatch.setattr(AcquisitionAutomationService, "_qualification_delivery_quota_reason", gate)
    assert await qualification.priority_account_ids(test_db, {}, now) == (
        {account.id} if reason is None else set()
    )
    assert gate.await_args.args[1] == group.group_id


async def test_real_identity_gate_releases_review_priority(test_db):
    account, _, review, now = await due_ad(test_db)
    evidence = json.loads(review.evidence_json)
    # Positive bare ids require a namespace; never infer one to make an ad send.
    evidence.pop("group_type", None)
    review.evidence_json = json.dumps(evidence)
    await test_db.commit()
    assert await qualification.priority_account_ids(test_db, {}, now) == set()
    assert review.state == "completed" and review.decision == "allowed"


async def test_due_survival_keeps_priority_even_when_ad_cannot_send(test_db, monkeypatch):
    account, group, _, now = await due_ad(test_db)
    test_db.add(
        AdDeliveryLog(
            account_id=account.id,
            telegram_group_id=group.group_id,
            ad_campaign_id=1,
            status="success",
            telegram_message_id=55,
            survival_status="pending",
            survival_check_due_at=now - timedelta(seconds=1),
        )
    )
    await test_db.commit()
    gate = AsyncMock(return_value="frequency_group_daily_cap")
    monkeypatch.setattr(AcquisitionAutomationService, "_qualification_delivery_quota_reason", gate)
    assert await qualification.priority_account_ids(test_db, {}, now) == {account.id}
    gate.assert_not_awaited()
