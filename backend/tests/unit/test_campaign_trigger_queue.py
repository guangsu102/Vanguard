import importlib
import json

import pytest

from app.core.campaign.models import Campaign, CampaignScope, CampaignType
from app.modules.guardian.models import GroupCampaignTriggerEvent

campaigns_api = importlib.import_module("app.api.campaigns")


class DummyResult:
    id = "campaign-task-123"


@pytest.mark.asyncio
async def test_managed_group_manual_trigger_is_queued(test_db, monkeypatch):
    campaign = Campaign(
        name="manual-broadcast-test",
        campaign_type=CampaignType.DISCOUNT,
        campaign_scope=CampaignScope.MANAGED_GROUP,
        trigger_timing="manual",
        trigger_event=GroupCampaignTriggerEvent.MANUAL_BROADCAST.value,
        enabled=True,
    )
    test_db.add(campaign)
    await test_db.commit()
    await test_db.refresh(campaign)

    calls = {}

    def fake_apply_async(*args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs
        return DummyResult()

    monkeypatch.setattr(campaigns_api.execute_campaign_rewards, "apply_async", fake_apply_async)

    response = await campaigns_api.trigger_campaign(campaign.id, db=test_db)

    assert calls["kwargs"] == {"args": [campaign.id], "queue": "default"}
    assert response["data"]["queued"] is True
    assert response["data"]["task_id"] == "campaign-task-123"


@pytest.mark.asyncio
async def test_global_manual_trigger_without_user_id_is_queued(test_db, monkeypatch):
    campaign = Campaign(
        name="global-manual-test",
        campaign_type=CampaignType.DISCOUNT,
        campaign_scope=CampaignScope.GLOBAL,
        trigger_timing="manual",
        distribution_mode="manual",
        enabled=True,
    )
    test_db.add(campaign)
    await test_db.commit()
    await test_db.refresh(campaign)

    calls = {}

    def fake_apply_async(*args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs
        return DummyResult()

    monkeypatch.setattr(campaigns_api.execute_campaign_rewards, "apply_async", fake_apply_async)

    response = await campaigns_api.trigger_campaign(campaign.id, db=test_db)

    assert calls["kwargs"] == {"args": [campaign.id], "queue": "default"}
    assert response["data"]["queued"] is True
    assert response["data"]["user_id"] is None


@pytest.mark.asyncio
async def test_managed_group_structured_policy_fields_are_stored(test_db, monkeypatch):
    async def fake_ensure_bindings(db, group_ids):
        return [
            type(
                "Binding",
                (),
                {
                    "telegram_group_id": group_ids[0],
                    "bot_account_id": 99,
                },
            )()
        ]

    async def fake_ensure_bot(db, bot_account_id):
        return None

    monkeypatch.setattr(campaigns_api, "ensure_managed_group_bindings", fake_ensure_bindings)
    monkeypatch.setattr(campaigns_api, "ensure_guardian_bot_account", fake_ensure_bot)

    payload = campaigns_api.CampaignCreate(
        name="structured-policy-test",
        campaign_type="discount",
        campaign_scope="managed_group",
        trigger_event=GroupCampaignTriggerEvent.NEW_MEMBER_DELAY.value,
        trigger_timing="delayed",
        target_group_ids=[10001],
        bot_account_id=99,
        distribution_mode="delayed",
        broadcast_message="hello group",
        delay_minutes=15,
        once_per_user=True,
        min_join_minutes=5,
        enabled=True,
    )

    response = await campaigns_api.create_campaign(payload, db=test_db)

    campaign = await test_db.get(Campaign, response.id)
    broadcast_policy = json.loads(campaign.broadcast_policy_json)
    eligibility_policy = json.loads(campaign.eligibility_policy_json)

    assert response.broadcast_message == "hello group"
    assert response.delay_minutes == 15
    assert response.once_per_user is True
    assert response.min_join_minutes == 5
    assert broadcast_policy == {"message": "hello group", "delay_minutes": 15}
    assert eligibility_policy == {"once_per_user": True, "min_join_minutes": 5}


@pytest.mark.asyncio
async def test_managed_group_distribution_mode_is_derived_from_trigger_event(test_db, monkeypatch):
    async def fake_ensure_bindings(db, group_ids):
        return [
            type(
                "Binding",
                (),
                {
                    "telegram_group_id": group_ids[0],
                    "bot_account_id": 99,
                },
            )()
        ]

    async def fake_ensure_bot(db, bot_account_id):
        return None

    monkeypatch.setattr(campaigns_api, "ensure_managed_group_bindings", fake_ensure_bindings)
    monkeypatch.setattr(campaigns_api, "ensure_guardian_bot_account", fake_ensure_bot)

    payload = campaigns_api.CampaignCreate(
        name="distribution-mode-derived-test",
        campaign_type="discount",
        campaign_scope="managed_group",
        trigger_event=GroupCampaignTriggerEvent.NEW_MEMBER_DELAY.value,
        trigger_timing="delayed",
        target_group_ids=[10001],
        bot_account_id=99,
        distribution_mode="welcome",
        delay_minutes=15,
        enabled=True,
    )

    response = await campaigns_api.create_campaign(payload, db=test_db)
    campaign = await test_db.get(Campaign, response.id)
    broadcast_policy = json.loads(campaign.broadcast_policy_json)

    assert response.distribution_mode == "delayed"
    assert campaign.distribution_mode == "delayed"
    assert broadcast_policy == {"delay_minutes": 15}


@pytest.mark.asyncio
async def test_global_structured_policy_fields_are_stored(test_db):
    payload = campaigns_api.CampaignCreate(
        name="global-structured-policy-test",
        campaign_type="discount",
        campaign_scope="global",
        trigger_timing="scheduled",
        distribution_mode="welcome",
        broadcast_message="hello users",
        schedule_times=["09:00", "09:00", "18:30"],
        once_per_user=True,
        target_user_states=["new", "pending", "new"],
        target_limit=50,
        min_account_age_minutes=30,
        enabled=True,
    )

    response = await campaigns_api.create_campaign(payload, db=test_db)

    campaign = await test_db.get(Campaign, response.id)
    broadcast_policy = json.loads(campaign.broadcast_policy_json)
    eligibility_policy = json.loads(campaign.eligibility_policy_json)

    assert response.broadcast_message == "hello users"
    assert response.distribution_mode == "scheduled"
    assert campaign.distribution_mode == "scheduled"
    assert response.schedule_times == ["09:00", "18:30"]
    assert response.target_user_states == ["new", "pending"]
    assert response.target_limit == 50
    assert response.min_account_age_minutes == 30
    assert broadcast_policy == {"message": "hello users", "schedule_times": ["09:00", "18:30"]}
    assert eligibility_policy == {
        "once_per_user": True,
        "target_user_states": ["new", "pending"],
        "target_limit": 50,
        "min_account_age_minutes": 30,
    }


@pytest.mark.asyncio
async def test_structured_policy_fields_clear_existing_values(test_db):
    campaign = Campaign(
        name="clear-policy-test",
        campaign_type=CampaignType.DISCOUNT,
        campaign_scope=CampaignScope.GLOBAL,
        trigger_timing="manual",
        distribution_mode="manual",
        broadcast_policy_json=json.dumps({"message": "old message", "delay_minutes": 10}),
        eligibility_policy_json=json.dumps(
            {
                "once_per_user": True,
                "target_user_states": ["active"],
                "target_limit": 20,
                "min_account_age_minutes": 60,
            }
        ),
        enabled=True,
    )
    test_db.add(campaign)
    await test_db.commit()
    await test_db.refresh(campaign)

    payload = campaigns_api.CampaignUpdate(
        broadcast_message="",
        target_user_states=[],
        target_limit=0,
        min_account_age_minutes=0,
    )

    response = await campaigns_api.update_campaign(campaign.id, payload, db=test_db)

    await test_db.refresh(campaign)

    assert response.broadcast_message is None
    assert response.target_user_states is None
    assert response.target_limit is None
    assert response.min_account_age_minutes is None
    assert campaign.broadcast_policy_json is None
    assert campaign.eligibility_policy_json == json.dumps({"once_per_user": True}, ensure_ascii=False)
