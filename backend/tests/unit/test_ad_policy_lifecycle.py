import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.modules.acquisition.automation as acquisition_automation
from app.core.account.models import AccountStatus, AccountType, TelegramAccount
from app.core.automation_settings import get_ad_capacity_settings
from app.core.group.models import Group, GroupAccountMembership, GroupLevel
from app.modules.acquisition.automation import (
    AcquisitionAutomationService,
    GroupAdRulesAuditResult,
    JoinedGroupAuditResult,
)
from app.modules.acquisition.dynamic_frequency import AccountDynamicFrequencyService
from app.modules.acquisition.models import (
    AccountAdBinding,
    AcquisitionTracking,
    AdCampaign,
    AdDeliveryLog,
    AdSurvivalStatus,
    DeliveryStatus,
    GroupAdPolicyMode,
    GroupAdProfile,
    GroupAdTier,
)


def test_group_capacity_requires_explicit_ad_permission():
    group = Group(group_id=930001, title="Unknown Policy Group", level=GroupLevel.A, status="active")
    profile = GroupAdProfile(
        group_id=1,
        telegram_group_id=group.group_id,
        ad_policy_mode=GroupAdPolicyMode.UNKNOWN.value,
        ad_policy_confidence=100,
        ad_tier=GroupAdTier.PREMIUM.value,
        daily_capacity=400,
    )
    capacity = {
        "group_global_daily_hard_cap": 400,
        "tier_daily_capacities": {"premium": 400},
    }

    assert AccountDynamicFrequencyService.group_ad_daily_capacity(profile, group, capacity) == 0

    profile.ad_policy_mode = GroupAdPolicyMode.SOFT_AD_TRIAL.value
    assert AccountDynamicFrequencyService.group_ad_daily_capacity(profile, group, capacity) == 1
    profile.ad_policy_confidence = 80
    assert AccountDynamicFrequencyService.group_ad_daily_capacity(profile, group, capacity) == 1
    profile.ad_policy_confidence = 79
    assert AccountDynamicFrequencyService.group_ad_daily_capacity(profile, group, capacity) == 0

    profile.ad_policy_mode = GroupAdPolicyMode.SOFT_AD_ALLOWED.value
    profile.ad_policy_confidence = 100
    assert AccountDynamicFrequencyService.group_ad_daily_capacity(profile, group, capacity) == 400

    profile.ad_policy_confidence = 89
    assert AccountDynamicFrequencyService.group_ad_daily_capacity(profile, group, capacity) == 0

    capacity["group_global_daily_hard_cap"] = 999
    profile.daily_capacity = 999
    profile.ad_policy_confidence = 100
    capacity["tier_daily_capacities"]["premium"] = 999
    assert AccountDynamicFrequencyService.group_ad_daily_capacity(profile, group, capacity) == 400


def test_no_link_campaign_generates_profile_cta_creatives_only():
    service = AcquisitionAutomationService(None)
    campaign = SimpleNamespace(name="PipenAI soft ad")
    seed = SimpleNamespace(
        content="GPT Plus、Pro 和 Claude Opus 有低倍率试用，需要的查看个人简介获取平台链接。",
        link_url=None,
    )

    prompt = service._build_ad_creative_generation_prompt(campaign, seed, 3)
    assert "禁止出现网址" in prompt
    assert "{{link_url}} 占位符" in prompt

    response = "\n".join(
        [
            "最近在对比 GPT 和 Claude 的低倍率试用，需要的查看个人简介获取平台链接。",
            "GPT 和 Claude 通道可以先试再用，平台入口在个人资料里。",
            "GPT 和 Claude 通道可以先试再用，需要的看资料。",
            "GPT 通道详情：https://pipenai.xyz",
            "GPT 通道详情：{{link_url}}",
        ]
    )
    parsed = service._parse_generated_ad_creatives(response, require_link=False)

    assert len(parsed) == 2
    assert all("http" not in item and "{{link_url}}" not in item for item in parsed)


def test_unresolved_link_placeholder_is_not_sendable():
    service = AcquisitionAutomationService(None)
    unresolved = SimpleNamespace(content="GPT 通道详情：{{link_url}}", link_url=None)
    resolved = SimpleNamespace(content="GPT 通道详情：{{link_url}}", link_url="https://example.com")
    profile_cta = SimpleNamespace(content="GPT 通道可以试用，需要的看资料。", link_url=None)

    assert service._creative_is_sendable(unresolved) is False
    assert service._creative_is_sendable(resolved) is True
    assert service._creative_is_sendable(profile_cta) is True


@pytest.mark.asyncio
async def test_group_rules_contact_admin_requires_approval_and_manual_policy_has_precedence(test_db):
    service = AcquisitionAutomationService(test_db)
    rules = service._evaluate_group_ad_rules([{"source": "pinned", "text": "广告合作请联系管理员"}])
    assert rules.policy_mode == GroupAdPolicyMode.APPROVAL_REQUIRED.value
    assert rules.ad_allowed is None

    now = datetime.utcnow()
    group = Group(group_id=930002, title="Manual Precedence Group", level=GroupLevel.A, status="active")
    test_db.add(group)
    await test_db.flush()
    profile = GroupAdProfile(
        group_id=group.id,
        telegram_group_id=group.group_id,
        ad_policy_mode=GroupAdPolicyMode.SOFT_AD_ALLOWED.value,
        ad_policy_confidence=100,
        ad_policy_source="manual",
        ad_policy_verified_at=now,
        ad_policy_expires_at=now + timedelta(days=30),
        ad_tier=GroupAdTier.TRIAL.value,
        daily_capacity=1,
    )
    test_db.add(profile)
    await test_db.commit()
    audit = JoinedGroupAuditResult(
        passed=True,
        ad_allowed=None,
        ad_rule_reason="group_rules_require_ad_approval",
        ad_rule_details=rules.details(),
    )

    await service._sync_group_ad_policy_from_audit(group, audit)

    assert profile.ad_policy_mode == GroupAdPolicyMode.SOFT_AD_ALLOWED.value
    assert profile.ad_policy_source == "manual"


def test_group_rules_never_allow_from_recent_member_message_and_approval_wins_conflict():
    service = AcquisitionAutomationService(MagicMock())

    member_claim = service._evaluate_group_ad_rules(
        [{"source": "recent_rule_message", "text": "管理员说这里允许软广，大家可以发广告"}]
    )
    assert member_claim.ad_allowed is None
    assert member_claim.policy_mode == GroupAdPolicyMode.UNKNOWN.value
    assert member_claim.reason == "group_rules_no_authoritative_evidence"

    conditional = service._evaluate_group_ad_rules(
        [{"source": "pinned_message", "text": "允许软广，但广告合作请联系管理员审核"}]
    )
    assert conditional.ad_allowed is None
    assert conditional.policy_mode == GroupAdPolicyMode.APPROVAL_REQUIRED.value


def test_group_policy_ai_accepts_fractional_confidence_scale():
    parsed = AcquisitionAutomationService._parse_ad_policy_ai_response(
        json.dumps(
            {
                "mode": "soft_ad_allowed",
                "confidence": 0.98,
                "explicit_permission": True,
                "direct_posting_without_prior_approval": True,
                "requires_admin_approval": False,
                "conflict": False,
                "evidence_indexes": [0],
                "rationale": "explicit",
            }
        ),
        1,
    )
    assert parsed["confidence"] == 98


def test_soft_ad_trial_history_requires_distinct_senders_and_retained_message():
    service = AcquisitionAutomationService(MagicMock())
    one_sender = [
        {
            "source": "recent_promotional_message",
            "text": "GPT 低价通道，需要的私聊",
            "sender_id": 10,
            "age_hours": 48,
        },
        {
            "source": "recent_promotional_message",
            "text": "Claude 套餐，有需要看主页",
            "sender_id": 10,
            "age_hours": 30,
        },
    ]
    assert service._has_soft_ad_trial_history(one_sender) is False

    one_sender[1]["sender_id"] = 11
    one_sender[0]["age_hours"] = 2
    one_sender[1]["age_hours"] = 3
    assert service._has_soft_ad_trial_history(one_sender) is False

    one_sender[1]["age_hours"] = 25
    assert service._has_soft_ad_trial_history(one_sender) is True


@pytest.mark.asyncio
async def test_ad_policy_reads_public_messages_from_before_twenty_four_hour_cutoff():
    old_message = SimpleNamespace(
        id=101,
        sender_id=20,
        date=datetime.utcnow() - timedelta(hours=30),
        message="GPT Plus 低倍率通道，需要的私聊",
    )

    class FakeClient:
        def iter_messages(self, entity, *, limit, offset_date=None):
            assert entity.title == "AI Exchange"
            assert limit == 50
            assert offset_date is not None

            async def iterator():
                yield old_message

            return iterator()

    service = AcquisitionAutomationService(MagicMock())
    entity = SimpleNamespace(title="AI Exchange", username="ai_exchange")
    messages = await service._fetch_messages_before(
        FakeClient(),
        entity,
        before=datetime.utcnow() - timedelta(hours=24),
        limit=50,
    )
    evidence = await service._read_group_ad_rules_evidence(FakeClient(), entity, messages)

    assert len(messages) == 1
    promotional = [item for item in evidence if item["source"] == "recent_promotional_message"]
    assert promotional[0]["sender_id"] == 20
    assert promotional[0]["age_hours"] >= 29


@pytest.mark.asyncio
async def test_group_history_two_pass_ai_can_enable_one_per_day_soft_ad_trial(test_db):
    service = AcquisitionAutomationService(test_db)
    verdict = {
        "mode": "soft_ad_trial",
        "confidence": 92,
        "explicit_permission": False,
        "direct_posting_without_prior_approval": False,
        "requires_admin_approval": False,
        "observed_soft_ad_tolerance": True,
        "low_risk_trial_suitable": True,
        "conflict": False,
        "evidence_indexes": [1, 2],
        "rationale": "Multiple distinct users have retained promotional posts in public history.",
    }
    service._ad_policy_llm_client = SimpleNamespace(
        generate=AsyncMock(side_effect=[json.dumps(verdict), json.dumps(verdict)])
    )
    evidence = [
        {"source": "group_profile", "text": "title=AI交流; username=ai_chat"},
        {
            "source": "recent_promotional_message",
            "text": "GPT Plus 低价通道，需要的私聊",
            "sender_id": 10,
            "age_hours": 49,
        },
        {
            "source": "recent_promotional_message",
            "text": "Claude 套餐试用，有需要看主页",
            "sender_id": 11,
            "age_hours": 25,
        },
    ]
    result = await service._evaluate_group_ad_rules_with_ai(
        evidence,
        GroupAdRulesAuditResult(evidence=evidence),
        {
            "ad_policy_ai_enabled": True,
            "ad_policy_ai_model": "gpt-5.6-terra",
            "ad_policy_ai_timeout_seconds": 30,
            "ad_policy_ai_min_confidence": 95,
            "ad_policy_ai_require_second_pass": True,
        },
    )

    assert result.ad_allowed is True
    assert result.policy_mode == GroupAdPolicyMode.SOFT_AD_TRIAL.value
    assert result.reason == "group_history_supports_soft_ad_trial"
    assert result.confidence == 92


@pytest.mark.asyncio
async def test_relevant_public_group_profile_can_enable_controlled_soft_ad_trial(test_db):
    service = AcquisitionAutomationService(test_db)
    verdict = {
        "mode": "soft_ad_trial",
        "confidence": 82,
        "explicit_permission": False,
        "direct_posting_without_prior_approval": False,
        "requires_admin_approval": False,
        "observed_soft_ad_tolerance": False,
        "low_risk_trial_suitable": True,
        "conflict": False,
        "evidence_indexes": [0],
        "rationale": "Open ChatGPT discussion group with no public advertising restriction.",
    }
    service._ad_policy_llm_client = SimpleNamespace(
        generate=AsyncMock(side_effect=[json.dumps(verdict), json.dumps(verdict)])
    )
    evidence = [{"source": "group_profile", "text": "title=ChatGPT交流群; username=gpt_chat"}]

    result = await service._evaluate_group_ad_rules_with_ai(
        evidence,
        GroupAdRulesAuditResult(evidence=evidence),
        {
            "ad_policy_ai_enabled": True,
            "ad_policy_ai_model": "gpt-5.6-terra",
            "ad_policy_ai_timeout_seconds": 30,
            "ad_policy_ai_min_confidence": 95,
            "ad_policy_ai_require_second_pass": True,
        },
    )

    assert result.ad_allowed is True
    assert result.policy_mode == GroupAdPolicyMode.SOFT_AD_TRIAL.value


@pytest.mark.asyncio
async def test_sync_group_policy_preserves_ai_soft_ad_trial_mode(test_db):
    group = Group(group_id=930003, title="Observed Soft Ads", level=GroupLevel.A, status="active")
    test_db.add(group)
    await test_db.commit()
    service = AcquisitionAutomationService(test_db)
    rules = GroupAdRulesAuditResult(
        ad_allowed=True,
        policy_mode=GroupAdPolicyMode.SOFT_AD_TRIAL.value,
        reason="group_history_supports_soft_ad_trial",
        confidence=92,
        decision_source="gpt-5.6-terra_two_pass",
    )

    profile = await service._sync_group_ad_policy_from_audit(
        group,
        JoinedGroupAuditResult(
            passed=True,
            ad_allowed=True,
            ad_rule_reason=rules.reason,
            ad_rule_details=rules.details(),
        ),
    )

    assert profile.ad_policy_mode == GroupAdPolicyMode.SOFT_AD_TRIAL.value
    assert profile.ad_tier == GroupAdTier.TRIAL.value
    assert profile.daily_capacity == 1


@pytest.mark.asyncio
async def test_group_rules_require_two_gpt_reviews_for_direct_soft_ad_permission(test_db):
    service = AcquisitionAutomationService(test_db)
    verdict = {
        "mode": "soft_ad_allowed",
        "confidence": 98,
        "explicit_permission": True,
        "direct_posting_without_prior_approval": True,
        "requires_admin_approval": False,
        "conflict": False,
        "evidence_indexes": [0],
        "rationale": "Pinned rule explicitly permits soft ads without approval.",
    }
    service._ad_policy_llm_client = SimpleNamespace(
        generate=AsyncMock(side_effect=[json.dumps(verdict), json.dumps(verdict)])
    )
    evidence = [{"source": "pinned_message", "text": "本群允许软广，可直接发布，无需联系管理员"}]
    local = service._evaluate_group_ad_rules(evidence)
    result = await service._evaluate_group_ad_rules_with_ai(
        evidence,
        local,
        {
            "ad_policy_ai_enabled": True,
            "ad_policy_ai_model": "gpt-5.4",
            "ad_policy_ai_timeout_seconds": 30,
            "ad_policy_ai_min_confidence": 95,
            "ad_policy_ai_require_second_pass": True,
        },
    )

    assert result.ad_allowed is True
    assert result.policy_mode == GroupAdPolicyMode.SOFT_AD_ALLOWED.value
    assert result.confidence == 98
    assert result.decision_source == "gpt-5.4_two_pass"
    assert len(result.ai_reviews) == 2


@pytest.mark.asyncio
async def test_group_rules_gpt_disagreement_and_api_failure_fail_closed(test_db):
    service = AcquisitionAutomationService(test_db)
    allow = {
        "mode": "soft_ad_allowed",
        "confidence": 99,
        "explicit_permission": True,
        "direct_posting_without_prior_approval": True,
        "requires_admin_approval": False,
        "conflict": False,
        "evidence_indexes": [0],
        "rationale": "Appears allowed.",
    }
    approval = {
        **allow,
        "mode": "approval_required",
        "direct_posting_without_prior_approval": False,
        "requires_admin_approval": True,
    }
    capacity = {
        "ad_policy_ai_enabled": True,
        "ad_policy_ai_model": "gpt-5.4",
        "ad_policy_ai_timeout_seconds": 30,
        "ad_policy_ai_min_confidence": 95,
        "ad_policy_ai_require_second_pass": True,
    }
    evidence = [{"source": "full_about", "text": "本群可接广告，具体请联系管理员"}]

    service._ad_policy_llm_client = SimpleNamespace(
        generate=AsyncMock(side_effect=[json.dumps(allow), json.dumps(approval)])
    )
    disagreement = await service._evaluate_group_ad_rules_with_ai(
        evidence,
        GroupAdRulesAuditResult(evidence=evidence),
        capacity,
    )
    assert disagreement.ad_allowed is None
    assert disagreement.policy_mode == GroupAdPolicyMode.UNKNOWN.value
    assert disagreement.reason == "group_rules_ai_consensus_failed"

    service._ad_policy_llm_client = SimpleNamespace(generate=AsyncMock(side_effect=TimeoutError("timeout")))
    failed = await service._evaluate_group_ad_rules_with_ai(
        evidence,
        GroupAdRulesAuditResult(evidence=evidence),
        capacity,
    )
    assert failed.ad_allowed is None
    assert failed.policy_mode == GroupAdPolicyMode.UNKNOWN.value
    assert failed.reason == "group_rules_ai_unavailable"
    assert failed.decision_source == "gpt_fail_closed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy_mode", "policy_source", "clean_days"),
    [
        (GroupAdPolicyMode.SOFT_AD_ALLOWED.value, "group_rules", 5),
        (GroupAdPolicyMode.SOFT_AD_ALLOWED.value, "manual", 3),
        (GroupAdPolicyMode.HIGH_VOLUME_AD_ALLOWED.value, "manual", 3),
    ],
)
async def test_premium_requires_clean_24h_samples_and_real_conversion(
    test_db,
    policy_mode,
    policy_source,
    clean_days,
):
    now = datetime.utcnow()
    account = TelegramAccount(
        phone="+15550003001",
        identifier="+15550003001",
        session_name=f"premium_policy_{clean_days}",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    group = Group(group_id=930010 + clean_days, title="Premium Candidate", level=GroupLevel.A, status="active")
    campaign = AdCampaign(name=f"Premium Campaign {clean_days}", enabled=True, status="active")
    test_db.add_all([account, group, campaign])
    await test_db.flush()
    profile = GroupAdProfile(
        group_id=group.id,
        telegram_group_id=group.group_id,
        ad_policy_mode=policy_mode,
        ad_policy_confidence=100,
        ad_policy_source=policy_source,
        ad_policy_verified_at=now - timedelta(days=clean_days, minutes=5),
        ad_policy_expires_at=now + timedelta(days=10),
        ad_tier=GroupAdTier.TRIAL.value,
        daily_capacity=1,
    )
    test_db.add(profile)
    for index in range(20):
        sent_at = now - timedelta(days=clean_days) + timedelta(minutes=index)
        test_db.add(
            AdDeliveryLog(
                account_id=account.id,
                group_id=group.id,
                telegram_group_id=group.group_id,
                ad_campaign_id=campaign.id,
                status=DeliveryStatus.SUCCESS.value,
                survival_status=AdSurvivalStatus.SURVIVED.value,
                survival_stage="complete",
                survived_twenty_four_hour_at=sent_at + timedelta(hours=24),
                sent_at=sent_at,
            )
        )
    test_db.add(
        AcquisitionTracking(
            tracking_code=f"premium-{clean_days}",
            group_id=group.group_id,
            converted=True,
            converted_at=now - timedelta(days=1),
            external_user_id=f"xboard-{clean_days}",
        )
    )
    await test_db.commit()

    service = AcquisitionAutomationService(test_db)
    capacity = await get_ad_capacity_settings(test_db)
    metrics = await service._refresh_group_ad_profile_tier(profile, group, now, capacity)

    assert metrics["premium_ready"] is True
    assert metrics["completed_samples"] == 20
    assert metrics["survival_rate_24h"] == 1.0
    assert metrics["conversions"] == 1
    assert profile.ad_tier == GroupAdTier.PREMIUM.value
    assert profile.daily_capacity == 20


def test_premium_capacity_ramps_without_jumping_to_400():
    capacity = {
        "premium_min_samples": 20,
        "premium_growth_samples": 100,
        "premium_full_capacity_samples": 1000,
        "premium_entry_capacity": 20,
        "premium_growth_capacity": 50,
        "premium_conversion_capacity_step": 20,
    }

    assert AcquisitionAutomationService._premium_evidence_capacity(20, 1, capacity, 400) == 20
    assert AcquisitionAutomationService._premium_evidence_capacity(40, 2, capacity, 400) == 28
    assert AcquisitionAutomationService._premium_evidence_capacity(100, 3, capacity, 400) == 50
    assert AcquisitionAutomationService._premium_evidence_capacity(500, 10, capacity, 400) == 200
    assert AcquisitionAutomationService._premium_evidence_capacity(1000, 20, capacity, 400) == 400

    unsafe_runtime = {
        **capacity,
        "premium_entry_capacity": 400,
        "premium_growth_capacity": 400,
        "premium_conversion_capacity_step": 100,
    }
    assert AcquisitionAutomationService._premium_evidence_capacity(20, 1, unsafe_runtime, 400) == 20


@pytest.mark.asyncio
async def test_manual_policy_api_synchronizes_group_status_and_safe_trial_capacity(test_db, client):
    from app.core.security import get_current_user
    from app.main import app

    group = Group(
        group_id=930019,
        title="Manual Policy Group",
        level=GroupLevel.A,
        status="ad_blocked",
    )
    test_db.add(group)
    await test_db.commit()
    await test_db.refresh(group)
    app.dependency_overrides[get_current_user] = lambda: {"id": 1, "username": "test", "role": "admin"}
    try:
        low_confidence = await client.put(
            f"/api/automation/ads/group-profiles/{group.id}/policy",
            json={"mode": GroupAdPolicyMode.SOFT_AD_ALLOWED.value, "confidence": 89, "note": "insufficient"},
        )
        assert low_confidence.status_code == 400

        allowed = await client.put(
            f"/api/automation/ads/group-profiles/{group.id}/policy",
            json={
                "mode": GroupAdPolicyMode.SOFT_AD_ALLOWED.value,
                "confidence": 100,
                "expires_days": 30,
                "note": "admin confirmed direct soft ads",
            },
        )
        assert allowed.status_code == 200
        allowed_data = allowed.json()["data"]
        assert allowed_data["ad_policy_mode"] == GroupAdPolicyMode.SOFT_AD_ALLOWED.value
        assert allowed_data["ad_policy_source"] == "manual"
        assert allowed_data["ad_tier"] == GroupAdTier.TRIAL.value
        assert allowed_data["daily_capacity"] == 1
        await test_db.refresh(group)
        assert group.status == "active"

        forbidden = await client.put(
            f"/api/automation/ads/group-profiles/{group.id}/policy",
            json={"mode": GroupAdPolicyMode.FORBIDDEN.value, "confidence": 100, "note": "admin revoked"},
        )
        assert forbidden.status_code == 200
        forbidden_data = forbidden.json()["data"]
        assert forbidden_data["ad_tier"] == GroupAdTier.BLOCKED.value
        assert forbidden_data["daily_capacity"] == 0
        await test_db.refresh(group)
        assert group.status == "ad_blocked"

        events = await client.get(f"/api/automation/ads/group-profiles/{group.id}/policy-events")
        assert events.status_code == 200
        assert [item["new_mode"] for item in events.json()["data"][:2]] == [
            GroupAdPolicyMode.FORBIDDEN.value,
            GroupAdPolicyMode.SOFT_AD_ALLOWED.value,
        ]
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_survival_is_only_final_after_twenty_four_hour_checkpoint(test_db):
    now = datetime.utcnow()
    sent_at = now - timedelta(days=1, minutes=1)
    account = TelegramAccount(
        phone="+15550003010",
        identifier="+15550003010",
        session_name="survival_checkpoints",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    group = Group(group_id=930020, title="Checkpoint Group", level=GroupLevel.A, status="active")
    campaign = AdCampaign(name="Checkpoint Campaign", enabled=True, status="active")
    test_db.add_all([account, group, campaign])
    await test_db.flush()
    profile = GroupAdProfile(
        group_id=group.id,
        telegram_group_id=group.group_id,
        ad_policy_mode=GroupAdPolicyMode.SOFT_AD_ALLOWED.value,
        ad_policy_confidence=100,
        ad_policy_verified_at=sent_at - timedelta(days=5),
        ad_policy_expires_at=now + timedelta(days=5),
        ad_tier=GroupAdTier.TRIAL.value,
        daily_capacity=1,
    )
    log = AdDeliveryLog(
        account_id=account.id,
        group_id=group.id,
        telegram_group_id=group.group_id,
        ad_campaign_id=campaign.id,
        status=DeliveryStatus.SUCCESS.value,
        survival_status=AdSurvivalStatus.PENDING.value,
        survival_stage="two_minute",
        survival_check_due_at=sent_at + timedelta(minutes=2),
        sent_at=sent_at,
        group=group,
    )
    test_db.add_all([profile, log])
    await test_db.commit()

    service = AcquisitionAutomationService(test_db)
    assert await service._mark_ad_survival_checkpoint(log, sent_at + timedelta(minutes=2)) == "pending_one_hour"
    assert log.survival_status == AdSurvivalStatus.PENDING.value
    assert log.survived_two_minute_at is not None

    assert await service._mark_ad_survival_checkpoint(log, sent_at + timedelta(hours=1)) == "pending_twenty_four_hour"
    assert log.survival_status == AdSurvivalStatus.PENDING.value
    assert log.survived_one_hour_at is not None

    assert await service._mark_ad_survival_checkpoint(log, now) == "survived"
    assert log.survival_status == AdSurvivalStatus.SURVIVED.value
    assert log.survival_stage == "complete"
    assert log.survived_twenty_four_hour_at == now


@pytest.mark.asyncio
async def test_first_deleted_ad_pauses_membership_without_global_group_block(test_db):
    now = datetime.utcnow()
    account = TelegramAccount(
        phone="+15550003011",
        identifier="+15550003011",
        session_name="first_delete_pause",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    group = Group(group_id=930021, title="Delete Pause Group", level=GroupLevel.A, status="active")
    campaign = AdCampaign(name="Delete Pause Campaign", enabled=True, status="active")
    test_db.add_all([account, group, campaign])
    await test_db.flush()
    profile = GroupAdProfile(
        group_id=group.id,
        telegram_group_id=group.group_id,
        ad_policy_mode=GroupAdPolicyMode.SOFT_AD_ALLOWED.value,
        ad_policy_confidence=100,
        ad_policy_verified_at=now - timedelta(days=5),
        ad_policy_expires_at=now + timedelta(days=5),
        ad_tier=GroupAdTier.HIGH.value,
        daily_capacity=200,
    )
    membership = GroupAccountMembership(
        group_id=group.id,
        telegram_group_id=group.group_id,
        account_id=account.id,
        status="joined",
        join_method="manual",
        probe_status="success",
        ad_status="active",
    )
    log = AdDeliveryLog(
        account_id=account.id,
        group_id=group.id,
        telegram_group_id=group.group_id,
        ad_campaign_id=campaign.id,
        status=DeliveryStatus.SUCCESS.value,
        survival_status=AdSurvivalStatus.PENDING.value,
        survival_stage="two_minute",
        sent_at=now - timedelta(minutes=3),
        group=group,
    )
    test_db.add_all([profile, membership, log])
    await test_db.commit()

    service = AcquisitionAutomationService(test_db)
    await service._mark_ad_survival_deleted(log, now, "message_missing_or_deleted")

    assert group.status == "active"
    assert profile.ad_policy_mode == GroupAdPolicyMode.SOFT_AD_ALLOWED.value
    assert profile.ad_tier == GroupAdTier.STABLE.value
    assert profile.paused_until is None
    assert membership.status == "joined"
    assert membership.ad_status == "paused"
    assert membership.ad_failure_streak == 1
    assert membership.ad_pause_until is not None


@pytest.mark.asyncio
async def test_group_daily_reservation_seeds_redis_from_persisted_delivery_count(test_db, monkeypatch):
    class FakeRedis:
        def __init__(self):
            self.values: dict[str, int] = {}

        async def exists(self, key):
            return key in self.values

        async def set(self, key, value, *, ex=None, nx=False):
            if nx and key in self.values:
                return False
            self.values[key] = int(value)
            return True

        async def incr(self, key):
            self.values[key] = self.values.get(key, 0) + 1
            return self.values[key]

        async def expire(self, key, ttl):
            return key in self.values

        async def decr(self, key):
            self.values[key] = max(0, self.values.get(key, 0) - 1)
            return self.values[key]

    now = datetime.utcnow()
    group = Group(group_id=930030, title="Redis Baseline Group", level=GroupLevel.A, status="active")
    test_db.add(group)
    await test_db.flush()
    profile = GroupAdProfile(
        group_id=group.id,
        telegram_group_id=group.group_id,
        ad_policy_mode=GroupAdPolicyMode.SOFT_AD_ALLOWED.value,
        ad_policy_confidence=100,
        ad_policy_verified_at=now - timedelta(days=5),
        ad_policy_expires_at=now + timedelta(days=5),
        ad_tier=GroupAdTier.PREMIUM.value,
        daily_capacity=400,
    )
    membership = SimpleNamespace(
        telegram_group_id=group.group_id,
        group=group,
    )
    test_db.add(profile)
    await test_db.commit()

    capacity = {
        "timezone_offset_hours": 8,
        "window_start_hour": 9,
        "group_global_daily_hard_cap": 400,
        "tier_daily_capacities": {"premium": 400},
    }
    monkeypatch.setattr(acquisition_automation, "get_ad_capacity_settings", AsyncMock(return_value=capacity))
    service = AcquisitionAutomationService(test_db)
    fake_redis = FakeRedis()
    service._new_ad_delivery_redis_client = AsyncMock(return_value=fake_redis)
    service._close_ad_delivery_redis_client = AsyncMock()
    service._count_successful_ads = AsyncMock(return_value=399)

    first = await service._reserve_group_daily_delivery_slot(membership, now)
    second = await service._reserve_group_daily_delivery_slot(membership, now)

    assert first[0] is True
    assert second[:2] == (False, "group_global_daily_budget")
    assert next(iter(fake_redis.values.values())) == 400


@pytest.mark.asyncio
async def test_survival_check_failure_retries_before_becoming_inconclusive(test_db):
    now = datetime.utcnow()
    account = TelegramAccount(
        phone="+15550003040",
        identifier="+15550003040",
        session_name="survival_retry",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    group = Group(group_id=930040, title="Retry Group", level=GroupLevel.A, status="active")
    campaign = AdCampaign(name="Retry Campaign", enabled=True, status="active")
    test_db.add_all([account, group, campaign])
    await test_db.flush()
    log = AdDeliveryLog(
        account_id=account.id,
        group_id=group.id,
        telegram_group_id=group.group_id,
        ad_campaign_id=campaign.id,
        status=DeliveryStatus.SUCCESS.value,
        telegram_message_id=123,
        survival_status=AdSurvivalStatus.PENDING.value,
        survival_stage="two_minute",
        survival_check_due_at=now,
        sent_at=now - timedelta(minutes=3),
    )
    test_db.add(log)
    await test_db.commit()
    service = AcquisitionAutomationService(test_db)
    service.account_pool.acquire_by_id = AsyncMock(return_value=None)

    assert await service._check_one_ad_survival(log, now) == "check_failed"
    assert log.survival_status == AdSurvivalStatus.PENDING.value
    assert log.survival_retry_count == 1
    assert log.survival_check_due_at > now

    log.survival_retry_count = 3
    assert await service._check_one_ad_survival(log, now) == "check_failed"
    assert log.survival_status == AdSurvivalStatus.CHECK_FAILED.value
    assert log.survival_check_due_at is None


@pytest.mark.asyncio
async def test_telegram_success_never_releases_reserved_capacity_when_log_confirmation_fails():
    db = MagicMock()
    db.rollback = AsyncMock()
    service = AcquisitionAutomationService(db)
    campaign = SimpleNamespace(id=1, enabled=True, status="active", start_at=None, end_at=None)
    binding = SimpleNamespace(id=1, account_id=1, campaign=campaign)
    group = SimpleNamespace(id=1, group_id=930050)
    membership = SimpleNamespace(group=group, telegram_group_id=group.group_id)
    creative = SimpleNamespace(id=1)
    pending_log = SimpleNamespace(reservation_token="reserved-token")
    service._list_enabled_ad_bindings_for_account = AsyncMock(return_value=[binding])
    service._list_joined_groups_for_account = AsyncMock(return_value=[membership])
    service._ad_dynamic_run_limit = AsyncMock(return_value=1)
    service._choose_delivery_creative = AsyncMock(return_value=creative)
    service._ad_skip_reason = AsyncMock(return_value=None)
    service._reserve_ad_delivery_target = AsyncMock(return_value=True)
    service._reserve_group_daily_delivery_slot = AsyncMock(return_value=(True, "reserved", "group-key"))
    service._record_ad_delivery = AsyncMock(return_value=pending_log)
    service._send_ad = AsyncMock(return_value=9001)
    service._finalize_ad_delivery_log = AsyncMock(side_effect=RuntimeError("database commit failed"))
    service._release_group_daily_delivery_slot = AsyncMock()
    service._release_ad_delivery_budget = AsyncMock()

    result = await service._run_ad_delivery_for_account(
        1,
        binding_ids=[1],
        dry_run=False,
        delivery_budget={"remaining": 1},
        delivery_budget_lock=asyncio.Lock(),
        reserved_ad_targets=set(),
        ad_target_lock=asyncio.Lock(),
        max_deliveries_per_account=1,
        stop_after_success=False,
        stop_after_failure=True,
    )

    assert result.failed == 1
    assert service._send_ad.await_count == 1
    service._release_group_daily_delivery_slot.assert_not_awaited()
    service._release_ad_delivery_budget.assert_not_awaited()
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_ad_policy_probe_keeps_group_lock_until_sending_state_is_committed(
    test_db,
    monkeypatch,
):
    now = datetime.utcnow()
    account = TelegramAccount(
        phone="+15550003000",
        identifier="+15550003000",
        session_name="stale_ad_policy_probe",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    group = Group(group_id=930000, title="Stale Probe Group", level=GroupLevel.A, status="active")
    campaign = AdCampaign(name="Stale Probe Campaign", enabled=True, status="active")
    test_db.add_all([account, group, campaign])
    await test_db.flush()
    profile = GroupAdProfile(
        group_id=group.id,
        telegram_group_id=group.group_id,
        ad_policy_mode=GroupAdPolicyMode.UNKNOWN.value,
        ad_policy_probe_status="sending",
        ad_policy_probe_at=now - timedelta(hours=1),
        ad_tier=GroupAdTier.TRIAL.value,
        daily_capacity=1,
    )
    membership = GroupAccountMembership(
        group_id=group.id,
        telegram_group_id=group.group_id,
        account_id=account.id,
        status="joined",
        join_method="manual",
        probe_status="success",
        ad_status="active",
    )
    binding = AccountAdBinding(
        account_id=account.id,
        ad_campaign_id=campaign.id,
        enabled=True,
    )
    test_db.add_all([profile, membership, binding])
    await test_db.commit()

    commit_states: list[str] = []
    session_type = type(test_db)
    original_commit = session_type.commit

    async def capture_commit(session):
        commit_states.append(profile.ad_policy_probe_status)
        await original_commit(session)

    monkeypatch.setattr(session_type, "commit", capture_commit)
    service = AcquisitionAutomationService(test_db)
    monkeypatch.setattr(service, "_get_account_operation_config", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_ad_account_risk_skip_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_group_can_receive_ads", AsyncMock(return_value=True))
    monkeypatch.setattr(service, "_sync_account_pool", AsyncMock())
    monkeypatch.setattr(
        service,
        "_reserve_group_daily_delivery_slot",
        AsyncMock(return_value=(True, "reserved", "group-slot")),
    )
    monkeypatch.setattr(
        service,
        "_record_ad_delivery",
        AsyncMock(return_value=SimpleNamespace(id=1)),
    )
    monkeypatch.setattr(service, "_send_ad_text", AsyncMock(return_value=9000))
    monkeypatch.setattr(service, "_finalize_ad_delivery_log", AsyncMock())

    result = await service.send_group_ad_policy_probe(group.id, account_id=account.id)

    assert result["ad_policy_probe_status"] == "sent"
    assert commit_states == ["sending", "sent"]
