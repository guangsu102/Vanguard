from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.core.account.models import (
    AccountOperationConfig,
    AccountOperationMode,
    AccountStatus,
    AccountType,
    TelegramAccount,
)
from app.core.automation_settings import save_ad_only_recommendation_settings
from app.core.group.models import Group, GroupAccountMembership, GroupLevel
from app.core.security import get_current_user
from app.main import app
from app.modules.acquisition.ad_only_recommendation import (
    AdOnlyRecommendationService,
    AdOnlyWorkflowError,
)
from app.modules.acquisition.models import (
    AdCampaign,
    AdCreative,
    AdDeliveryLog,
    AdDeliveryPolicy,
    AdSurvivalStatus,
    DeliveryStatus,
    GroupAdHandover,
    GroupAdOnlyAssessment,
    GroupAdOnlyEvent,
    GroupAdPolicyEvent,
    GroupAdPolicyMode,
    GroupAdProfile,
)


async def _seed_candidate(
    test_db,
    *,
    suffix: str,
    sample_count: int = 10,
) -> dict:
    now = datetime.utcnow()
    account = TelegramAccount(
        identifier=f"growth-{suffix}",
        display_name=f"Growth {suffix}",
        session_name=f"growth-{suffix}",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    operation = AccountOperationConfig(
        account=account,
        operation_mode=AccountOperationMode.GROWTH.value,
        enabled=True,
        auto_ads_enabled=True,
    )
    group = Group(
        group_id=-100900000 - int(suffix),
        title=f"Candidate {suffix}",
        level=GroupLevel.A,
        status="active",
    )
    campaign = AdCampaign(
        name=f"Growth candidate campaign {suffix}",
        enabled=True,
        status="active",
        delivery_policy=AdDeliveryPolicy.GROWTH.value,
    )
    creative = AdCreative(
        name=f"Creative {suffix}",
        content="formal ad",
        enabled=True,
    )
    test_db.add_all([account, operation, group, campaign, creative])
    await test_db.flush()
    membership = GroupAccountMembership(
        group_id=group.id,
        telegram_group_id=group.group_id,
        account_id=account.id,
        status="joined",
        join_method="manual",
    )
    profile = GroupAdProfile(
        group_id=group.id,
        telegram_group_id=group.group_id,
        ad_policy_mode=GroupAdPolicyMode.SOFT_AD_ALLOWED.value,
        ad_policy_confidence=100,
        ad_policy_source="description",
        ad_policy_verified_at=now - timedelta(days=5),
        ad_policy_expires_at=now + timedelta(days=20),
        ad_tier="stable",
        daily_capacity=20,
    )
    evidence = GroupAdPolicyEvent(
        group_id=group.id,
        account_id=account.id,
        telegram_group_id=group.group_id,
        new_mode=GroupAdPolicyMode.SOFT_AD_ALLOWED.value,
        confidence=100,
        source="description",
        reason="group description explicitly allows ads",
        created_at=now - timedelta(days=3),
    )
    test_db.add_all([membership, profile, evidence])
    await test_db.flush()
    for index in range(sample_count):
        sent_at = now - timedelta(days=2) + timedelta(minutes=index)
        test_db.add(
            AdDeliveryLog(
                account_id=account.id,
                group_id=group.id,
                telegram_group_id=group.group_id,
                ad_campaign_id=campaign.id,
                creative_id=creative.id,
                status=DeliveryStatus.SUCCESS.value,
                survival_status=AdSurvivalStatus.SURVIVED.value,
                survival_stage="complete",
                sent_at=sent_at,
                survived_twenty_four_hour_at=sent_at + timedelta(hours=24),
                created_at=sent_at,
            )
        )
    await test_db.commit()
    return {
        "now": now,
        "account": account,
        "group": group,
        "campaign": campaign,
        "creative": creative,
        "membership": membership,
        "profile": profile,
    }


@pytest.mark.asyncio
async def test_candidate_requires_ten_completed_formal_growth_ads(test_db):
    seeded = await _seed_candidate(test_db, suffix="1")
    assessment = await AdOnlyRecommendationService(test_db).evaluate_group(
        seeded["group"].id
    )

    assert assessment.status == "recommended"
    assert assessment.consecutive_success_count == 10
    assert assessment.send_success_percent == 100
    assert assessment.survival_24h_percent == 100
    assert assessment.pending_sample_count == 0
    assert assessment.valid_until is not None


@pytest.mark.asyncio
async def test_probe_logs_are_excluded_and_pending_survival_blocks(test_db):
    seeded = await _seed_candidate(test_db, suffix="2")
    now = datetime.utcnow()
    test_db.add_all(
        [
            AdDeliveryLog(
                account_id=seeded["account"].id,
                group_id=seeded["group"].id,
                telegram_group_id=seeded["group"].group_id,
                ad_campaign_id=seeded["campaign"].id,
                creative_id=None,
                status=DeliveryStatus.SUCCESS.value,
                survival_status=AdSurvivalStatus.SURVIVED.value,
                survived_twenty_four_hour_at=now,
                created_at=now,
            ),
            AdDeliveryLog(
                account_id=seeded["account"].id,
                group_id=seeded["group"].id,
                telegram_group_id=seeded["group"].group_id,
                ad_campaign_id=seeded["campaign"].id,
                creative_id=seeded["creative"].id,
                status=DeliveryStatus.SUCCESS.value,
                survival_status=AdSurvivalStatus.PENDING.value,
                sent_at=now,
                created_at=now,
            ),
        ]
    )
    await test_db.commit()

    assessment = await AdOnlyRecommendationService(test_db).evaluate_group(
        seeded["group"].id
    )

    assert assessment.status == "observing"
    assert assessment.completed_sample_count == 10
    assert assessment.pending_sample_count == 1
    assert "survival_checks_pending" in assessment.blocking_reasons_json


@pytest.mark.asyncio
async def test_only_group_attributable_failure_resets_streak(test_db):
    system_seed = await _seed_candidate(test_db, suffix="3")
    group_seed = await _seed_candidate(test_db, suffix="4")
    now = datetime.utcnow()
    test_db.add_all(
        [
            AdDeliveryLog(
                account_id=system_seed["account"].id,
                group_id=system_seed["group"].id,
                telegram_group_id=system_seed["group"].group_id,
                ad_campaign_id=system_seed["campaign"].id,
                creative_id=system_seed["creative"].id,
                status=DeliveryStatus.FAILED.value,
                error="redis_unavailable",
                created_at=now,
            ),
            AdDeliveryLog(
                account_id=group_seed["account"].id,
                group_id=group_seed["group"].id,
                telegram_group_id=group_seed["group"].group_id,
                ad_campaign_id=group_seed["campaign"].id,
                creative_id=group_seed["creative"].id,
                status=DeliveryStatus.FAILED.value,
                error="group_control:write_forbidden",
                created_at=now,
            ),
        ]
    )
    await test_db.commit()
    service = AdOnlyRecommendationService(test_db)

    system_assessment = await service.evaluate_group(system_seed["group"].id)
    group_assessment = await service.evaluate_group(group_seed["group"].id)

    assert system_assessment.status == "recommended"
    assert system_assessment.consecutive_success_count == 10
    assert group_assessment.status == "observing"
    assert group_assessment.consecutive_success_count == 0
    assert group_assessment.group_failure_count == 1


@pytest.mark.asyncio
async def test_negative_policy_evidence_overrides_positive(test_db):
    seeded = await _seed_candidate(test_db, suffix="5")
    test_db.add(
        GroupAdPolicyEvent(
            group_id=seeded["group"].id,
            account_id=seeded["account"].id,
            telegram_group_id=seeded["group"].group_id,
            previous_mode=GroupAdPolicyMode.SOFT_AD_ALLOWED.value,
            new_mode=GroupAdPolicyMode.FORBIDDEN.value,
            confidence=100,
            source="pinned_message",
            reason="no advertising",
            created_at=datetime.utcnow(),
        )
    )
    await test_db.commit()

    assessment = await AdOnlyRecommendationService(test_db).evaluate_group(
        seeded["group"].id
    )

    assert assessment.status == "observing"
    assert assessment.evidence_json
    assert "negative_permission_evidence" in assessment.blocking_reasons_json


@pytest.mark.asyncio
async def test_decisions_are_append_only_and_do_not_mutate_assessment(test_db):
    seeded = await _seed_candidate(test_db, suffix="6")
    service = AdOnlyRecommendationService(test_db)
    assessment = await service.evaluate_group(seeded["group"].id)
    original_status = assessment.status
    original_hash = assessment.evidence_hash

    event = await service.decide_assessment(
        assessment.id,
        decision="reject",
        actor_user_id=11,
        note="manual review rejected",
    )
    refreshed = await test_db.get(GroupAdOnlyAssessment, assessment.id)

    assert event["event_type"] == "assessment_rejected"
    assert refreshed.status == original_status
    assert refreshed.evidence_hash == original_hash


@pytest.mark.asyncio
async def test_handover_is_idempotent_and_invite_is_never_serialized(test_db):
    seeded = await _seed_candidate(test_db, suffix="7")
    target = TelegramAccount(
        identifier="ad-only-target-7",
        display_name="Ad only target",
        session_name="ad-only-target-7",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        risk_level="normal",
        is_active=True,
    )
    target_config = AccountOperationConfig(
        account=target,
        operation_mode=AccountOperationMode.AD_ONLY.value,
        enabled=True,
        auto_ads_enabled=True,
        max_messages_per_day=50,
    )
    test_db.add_all([target, target_config])
    await test_db.commit()
    await save_ad_only_recommendation_settings(
        test_db,
        {
            "recommendation_enabled": True,
            "handover_execution_enabled": True,
        },
    )
    service = AdOnlyRecommendationService(test_db)
    assessment = await service.evaluate_group(seeded["group"].id)
    await service.decide_assessment(
        assessment.id,
        decision="approve",
        actor_user_id=12,
        note="approved",
    )
    request = {
        "assessment_id": assessment.id,
        "target_account_id": target.id,
        "creative_id": seeded["creative"].id,
        "invite_link": "https://t.me/example_group_7",
        "send_mode": "interval",
        "interval_minutes": 180,
        "scheduled_times": [],
        "idempotency_key": "handover-test-key-7",
        "requested_by_user_id": 12,
    }

    handover, created = await service.create_handover(**request)
    replay, replay_created = await service.create_handover(**request)
    payload = service.handover_payload(handover)

    assert created is True
    assert replay_created is False
    assert replay.id == handover.id
    assert handover.invite_link_encrypted.startswith("vge1:")
    assert "example_group_7" not in handover.invite_link_encrypted
    assert "invite_link" not in payload
    count = (
        await test_db.execute(select(func.count(GroupAdHandover.id)))
    ).scalar_one()
    assert count == 1


@pytest.mark.asyncio
async def test_growth_leave_failure_retains_both_accounts_as_cleanup_pending(
    test_db,
    monkeypatch,
):
    seeded = await _seed_candidate(test_db, suffix="8")
    target = TelegramAccount(
        identifier="ad-only-target-8",
        display_name="Ad only target 8",
        session_name="ad-only-target-8",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        risk_level="normal",
        is_active=True,
    )
    target_config = AccountOperationConfig(
        account=target,
        operation_mode=AccountOperationMode.AD_ONLY.value,
        enabled=True,
        auto_ads_enabled=True,
        max_messages_per_day=50,
    )
    test_db.add_all([target, target_config])
    await test_db.commit()
    await save_ad_only_recommendation_settings(
        test_db,
        {
            "recommendation_enabled": True,
            "handover_execution_enabled": True,
        },
    )
    service = AdOnlyRecommendationService(test_db)
    assessment = await service.evaluate_group(seeded["group"].id)
    await service.decide_assessment(
        assessment.id,
        decision="approve",
        actor_user_id=13,
    )
    handover, _created = await service.create_handover(
        assessment_id=assessment.id,
        target_account_id=target.id,
        creative_id=seeded["creative"].id,
        invite_link="https://t.me/example_group_8",
        send_mode="interval",
        interval_minutes=180,
        scheduled_times=[],
        idempotency_key="handover-test-key-8",
        requested_by_user_id=13,
    )
    target_membership = GroupAccountMembership(
        group_id=seeded["group"].id,
        telegram_group_id=seeded["group"].group_id,
        account_id=target.id,
        status="joined",
        join_method="manual_link_join",
    )
    test_db.add(target_membership)
    await test_db.commit()

    async def fake_verify(
        _handover,
        group,
        target_account,
        _campaign,
        _binding,
        _schedule,
    ):
        group.ad_delivery_account_id = target_account.id
        await test_db.commit()

    async def fake_leave(_handover, _group, _source):
        return "temporary leave failure"

    monkeypatch.setattr(service, "_verify_and_assign_takeover", fake_verify)
    monkeypatch.setattr(service, "_leave_source_growth", fake_leave)

    result = await service.execute_handover(handover.id)
    source_membership = await service._joined_membership(
        seeded["group"].id, seeded["account"].id
    )
    refreshed_target = await service._joined_membership(
        seeded["group"].id, target.id
    )
    refreshed_group = await test_db.get(Group, seeded["group"].id)

    assert result["status"] == "cleanup_pending"
    assert source_membership is not None
    assert refreshed_target is not None
    assert refreshed_group.ad_delivery_account_id == target.id
    cleanup_event = (
        await test_db.execute(
            select(GroupAdOnlyEvent).where(
                GroupAdOnlyEvent.handover_id == handover.id,
                GroupAdOnlyEvent.event_type == "handover_cleanup_pending",
            )
        )
    ).scalar_one()
    assert cleanup_event.status == "cleanup_pending"

    async def fake_leave_success(_handover, group, source):
        membership = await service._joined_membership(group.id, source.id)
        membership.status = "left"
        membership.left_at = datetime.utcnow()
        await test_db.commit()
        return None

    monkeypatch.setattr(service, "_leave_source_growth", fake_leave_success)
    await service.prepare_retry(handover.id, actor_user_id=13)
    retry_result = await service.execute_handover(handover.id)

    assert retry_result["status"] == "completed"
    assert (
        await service._joined_membership(
            seeded["group"].id, seeded["account"].id
        )
        is None
    )


@pytest.mark.asyncio
async def test_candidate_batch_respects_disabled_default(test_db):
    result = await AdOnlyRecommendationService(test_db).evaluate_candidates()
    assert result["status"] == "skipped"
    assert result["reason"] == "recommendation_disabled"


@pytest.mark.asyncio
async def test_handover_execution_respects_disabled_default(test_db):
    seeded = await _seed_candidate(test_db, suffix="9")
    service = AdOnlyRecommendationService(test_db)
    assessment = await service.evaluate_group(seeded["group"].id)

    with pytest.raises(AdOnlyWorkflowError, match="handover_execution_disabled"):
        await service.preflight_handover(
            assessment_id=assessment.id,
            target_account_id=999,
            creative_id=seeded["creative"].id,
            invite_link="https://t.me/example_group_9",
            send_mode="interval",
            interval_minutes=180,
            scheduled_times=[],
        )


@pytest.mark.asyncio
async def test_handover_preflight_requires_admin_approval(test_db):
    seeded = await _seed_candidate(test_db, suffix="10")
    await save_ad_only_recommendation_settings(
        test_db,
        {
            "recommendation_enabled": True,
            "handover_execution_enabled": True,
        },
    )
    service = AdOnlyRecommendationService(test_db)
    assessment = await service.evaluate_group(seeded["group"].id)

    with pytest.raises(AdOnlyWorkflowError, match="assessment_approval_required"):
        await service.preflight_handover(
            assessment_id=assessment.id,
            target_account_id=999,
            creative_id=seeded["creative"].id,
            invite_link="https://t.me/example_group_10",
            send_mode="interval",
            interval_minutes=180,
            scheduled_times=[],
        )


@pytest.mark.asyncio
async def test_admin_api_exposes_disabled_defaults(client):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 21,
        "username": "admin",
        "role": "admin",
    }

    response = await client.get("/api/automation/ad-only/settings")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["recommendation_enabled"] is False
    assert data["handover_execution_enabled"] is False
    assert data["min_consecutive_samples"] == 10


@pytest.mark.asyncio
async def test_manual_group_join_rejects_ad_only_account(client, test_db):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 22,
        "username": "admin",
        "role": "admin",
    }
    account = TelegramAccount(
        identifier="manual-ad-only-blocked",
        session_name="manual-ad-only-blocked",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        risk_level="normal",
        is_active=True,
    )
    operation = AccountOperationConfig(
        account=account,
        operation_mode=AccountOperationMode.AD_ONLY.value,
        enabled=True,
        auto_ads_enabled=True,
    )
    test_db.add_all([account, operation])
    await test_db.commit()

    response = await client.post(
        "/api/groups/join-by-link",
        json={
            "account_id": account.id,
            "group_link": "https://t.me/example_group_blocked",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "ad_only_join_requires_handover_workflow"
