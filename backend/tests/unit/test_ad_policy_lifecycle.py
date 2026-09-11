import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

import app.modules.acquisition.automation as acquisition_automation
from app.core.account.models import AccountStatus, AccountType, TelegramAccount
from app.core.account.operation_lease import (
    AccountOperationLeaseBusy,
    AccountOperationLeaseUnavailable,
)
from app.core.account.telegram_execution import TelegramExecutionService
from app.core.automation_settings import get_ad_capacity_settings
from app.core.group.models import Group, GroupAccountMembership, GroupLevel
from app.modules.acquisition.automation import (
    AcquisitionAutomationService,
    GroupAdRulesAuditResult,
    JoinedGroupAuditResult,
)
from app.modules.acquisition.models import (
    AccountAdBinding,
    AcquisitionMessage,
    AcquisitionTracking,
    AdCampaign,
    AdCreative,
    AdDeliveryLog,
    AdDeliveryScheduleState,
    AdScheduleStatus,
    AdSurvivalStatus,
    DeliveryStatus,
    GroupAdPolicyEvent,
    GroupAdPolicyMode,
    GroupAdProfile,
    GroupAdTier,
)


@pytest.mark.asyncio
async def test_ad_probe_final_owned_recheck_blocks_account_telegram_and_state(
    test_db,
    monkeypatch,
):
    account = TelegramAccount(
        phone="+15550003991",
        identifier="+15550003991",
        session_name="owned_ad_probe_guard",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    group = Group(
        group_id=939991,
        title="Owned ad probe guard",
        level=GroupLevel.A,
        status="active",
    )
    membership = GroupAccountMembership(
        group=group,
        account=account,
        telegram_group_id=group.group_id,
        status="joined",
        warmup_status="probe_scheduled",
        probe_status="scheduled",
        ad_status="warming",
        note="unchanged",
    )
    test_db.add_all([account, group, membership])
    await test_db.commit()
    membership_before = {
        "warmup_status": membership.warmup_status,
        "probe_status": membership.probe_status,
        "ad_status": membership.ad_status,
        "note": membership.note,
        "last_probe_at": membership.last_probe_at,
        "ad_eligible_after": membership.ad_eligible_after,
    }

    pool = SimpleNamespace(acquire_by_id=AsyncMock(), release=AsyncMock())
    service = AcquisitionAutomationService(test_db, account_pool=pool)
    locked_path = AsyncMock()
    monkeypatch.setattr(service, "_send_ad_probe_locked", locked_path)
    service.telegram_execution.send_group_message = AsyncMock()
    lock_active = False

    @asynccontextmanager
    async def tracked_chat_lock(_db, chat_id):
        nonlocal lock_active
        assert chat_id == group.group_id
        lock_active = True
        try:
            yield
        finally:
            lock_active = False

    async def owned_inside_lock(core_group_id, telegram_chat_id):
        assert lock_active is True
        assert (core_group_id, telegram_chat_id) == (group.id, group.group_id)
        return True

    monkeypatch.setattr(
        acquisition_automation,
        "telegram_chat_advisory_lock",
        tracked_chat_lock,
    )
    monkeypatch.setattr(
        service,
        "_is_owned_group_ad_domain_excluded",
        AsyncMock(side_effect=owned_inside_lock),
    )

    result = await service._send_ad_probe(account.id, membership)

    assert result == "OWNED_GROUP_AD_DOMAIN_EXCLUDED"
    assert lock_active is False
    locked_path.assert_not_awaited()
    pool.acquire_by_id.assert_not_awaited()
    pool.release.assert_not_awaited()
    service.telegram_execution.send_group_message.assert_not_awaited()
    assert {
        "warmup_status": membership.warmup_status,
        "probe_status": membership.probe_status,
        "ad_status": membership.ad_status,
        "note": membership.note,
        "last_probe_at": membership.last_probe_at,
        "ad_eligible_after": membership.ad_eligible_after,
    } == membership_before
    assert list(
        (
            await test_db.execute(
                select(AcquisitionMessage).where(
                    AcquisitionMessage.group_id == group.group_id
                )
            )
        )
        .scalars()
        .all()
    ) == []
    assert list(
        (
            await test_db.execute(
                select(AdDeliveryLog).where(AdDeliveryLog.group_id == group.id)
            )
        )
        .scalars()
        .all()
    ) == []
    assert list(
        (
            await test_db.execute(
                select(GroupAdPolicyEvent).where(GroupAdPolicyEvent.group_id == group.id)
            )
        )
        .scalars()
        .all()
    ) == []
    assert list(
        (
            await test_db.execute(
                select(GroupAdProfile).where(GroupAdProfile.group_id == group.id)
            )
        )
        .scalars()
        .all()
    ) == []


@pytest.mark.asyncio
async def test_manual_policy_probe_final_owned_recheck_blocks_all_state(
    test_db,
    monkeypatch,
):
    group = Group(
        group_id=939992,
        title="Owned manual policy probe guard",
        level=GroupLevel.A,
        status="active",
    )
    test_db.add(group)
    await test_db.commit()

    pool = SimpleNamespace(acquire_by_id=AsyncMock(), release=AsyncMock())
    service = AcquisitionAutomationService(test_db, account_pool=pool)
    locked_path = AsyncMock()
    monkeypatch.setattr(service, "_send_group_ad_policy_probe_locked", locked_path)
    service._send_ad_text = AsyncMock()
    lock_active = False

    @asynccontextmanager
    async def tracked_chat_lock(_db, chat_id):
        nonlocal lock_active
        assert chat_id == group.group_id
        lock_active = True
        try:
            yield
        finally:
            lock_active = False

    async def owned_inside_lock(core_group_id, telegram_chat_id):
        assert lock_active is True
        assert (core_group_id, telegram_chat_id) == (group.id, group.group_id)
        return True

    monkeypatch.setattr(
        acquisition_automation,
        "telegram_chat_advisory_lock",
        tracked_chat_lock,
    )
    monkeypatch.setattr(
        service,
        "_is_owned_group_ad_domain_excluded",
        AsyncMock(side_effect=owned_inside_lock),
    )

    with pytest.raises(RuntimeError, match="^OWNED_GROUP_AD_DOMAIN_EXCLUDED$"):
        await service.send_group_ad_policy_probe(group.id, account_id=939993)

    assert lock_active is False
    locked_path.assert_not_awaited()
    pool.acquire_by_id.assert_not_awaited()
    pool.release.assert_not_awaited()
    service._send_ad_text.assert_not_awaited()
    assert list(
        (
            await test_db.execute(
                select(AdDeliveryLog).where(AdDeliveryLog.group_id == group.id)
            )
        )
        .scalars()
        .all()
    ) == []
    assert list(
        (
            await test_db.execute(
                select(GroupAdPolicyEvent).where(GroupAdPolicyEvent.group_id == group.id)
            )
        )
        .scalars()
        .all()
    ) == []
    assert list(
        (
            await test_db.execute(
                select(GroupAdProfile).where(GroupAdProfile.group_id == group.id)
            )
        )
        .scalars()
        .all()
    ) == []


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
async def test_group_rules_contact_admin_requires_approval_and_manual_policy_has_precedence(
    test_db,
):
    service = AcquisitionAutomationService(test_db)
    rules = service._evaluate_group_ad_rules([{"source": "pinned", "text": "广告合作请联系管理员"}])
    assert rules.policy_mode == GroupAdPolicyMode.APPROVAL_REQUIRED.value
    assert rules.ad_allowed is None

    now = datetime.utcnow()
    group = Group(
        group_id=930002, title="Manual Precedence Group", level=GroupLevel.A, status="active"
    )
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
async def test_group_history_high_confidence_ai_uses_two_pass_for_soft_ad_trial(test_db):
    service = AcquisitionAutomationService(test_db)
    verdict = {
        "mode": "soft_ad_trial",
        "confidence": 97,
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
    assert result.decision_source == "gpt-5.6-terra_two_pass"
    assert len(result.ai_reviews) == 2
    assert service._ad_policy_llm_client.generate.await_count == 2
    assert result.reason == "group_history_supports_soft_ad_trial"
    assert result.confidence == 97


@pytest.mark.asyncio
async def test_relevant_public_group_profile_can_enable_controlled_soft_ad_trial(test_db):
    service = AcquisitionAutomationService(test_db)
    verdict = {
        "mode": "soft_ad_trial",
        "confidence": 97,
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
    assert result.decision_source == "gpt-5.6-terra_two_pass"
    assert len(result.ai_reviews) == 2
    assert service._ad_policy_llm_client.generate.await_count == 2


@pytest.mark.asyncio
async def test_soft_ad_trial_below_configured_confidence_fails_closed(test_db):
    service = AcquisitionAutomationService(test_db)
    verdict = {
        "mode": "soft_ad_trial",
        "confidence": 94,
        "explicit_permission": False,
        "direct_posting_without_prior_approval": False,
        "requires_admin_approval": False,
        "observed_soft_ad_tolerance": True,
        "low_risk_trial_suitable": True,
        "conflict": False,
        "evidence_indexes": [0, 1],
        "rationale": "The trial context is plausible but below the configured floor.",
    }
    service._ad_policy_llm_client = SimpleNamespace(
        generate=AsyncMock(side_effect=[json.dumps(verdict), json.dumps(verdict)])
    )
    evidence = [
        {
            "source": "recent_promotional_message",
            "text": "GPT 低价通道，需要的私聊",
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

    assert result.ad_allowed is None
    assert result.policy_mode == GroupAdPolicyMode.UNKNOWN.value
    assert result.reason == "group_rules_ai_consensus_failed"
    assert len(result.ai_reviews) == 2
    assert service._ad_policy_llm_client.generate.await_count == 2


@pytest.mark.asyncio
async def test_explicit_permission_must_cite_authoritative_group_rule(test_db):
    service = AcquisitionAutomationService(test_db)
    verdict = {
        "mode": "soft_ad_allowed",
        "confidence": 99,
        "explicit_permission": True,
        "direct_posting_without_prior_approval": True,
        "requires_admin_approval": False,
        "conflict": False,
        # Index 1 is only a member promotional post, not a group rule.
        "evidence_indexes": [1],
        "rationale": "The member history looks permissive.",
    }
    service._ad_policy_llm_client = SimpleNamespace(
        generate=AsyncMock(side_effect=[json.dumps(verdict), json.dumps(verdict)])
    )
    evidence = [
        {"source": "pinned_message", "text": "本群允许软广，可直接发布"},
        {
            "source": "recent_promotional_message",
            "text": "GPT 低价通道，需要的私聊",
            "sender_id": 10,
            "age_hours": 49,
        },
    ]

    result = await service._evaluate_group_ad_rules_with_ai(
        evidence,
        service._evaluate_group_ad_rules(evidence),
        {
            "ad_policy_ai_enabled": True,
            "ad_policy_ai_model": "gpt-5.6-terra",
            "ad_policy_ai_timeout_seconds": 30,
            "ad_policy_ai_min_confidence": 95,
            "ad_policy_ai_require_second_pass": True,
        },
    )

    assert result.ad_allowed is None
    assert result.policy_mode == GroupAdPolicyMode.UNKNOWN.value
    assert result.reason == "group_rules_ai_consensus_failed"
    assert len(result.ai_reviews) == 2


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
    assert profile.daily_capacity == 0


@pytest.mark.asyncio
async def test_group_rules_high_confidence_direct_permission_uses_two_gpt_reviews(test_db):
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
    assert service._ad_policy_llm_client.generate.await_count == 2


@pytest.mark.asyncio
async def test_group_rules_gpt_disagreement_and_api_failure_fail_closed(test_db):
    service = AcquisitionAutomationService(test_db)
    allow = {
        "mode": "soft_ad_allowed",
        "confidence": 99,
        "explicit_permission": True,
        "direct_posting_without_prior_approval": True,
        "requires_admin_approval": False,
        "conflict": True,
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

    service._ad_policy_llm_client = SimpleNamespace(
        generate=AsyncMock(side_effect=TimeoutError("timeout"))
    )
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
async def test_group_rules_low_confidence_first_review_triggers_second_pass_and_fails_closed(
    test_db,
):
    service = AcquisitionAutomationService(test_db)
    verdict = {
        "mode": "soft_ad_allowed",
        "confidence": 90,
        "explicit_permission": True,
        "direct_posting_without_prior_approval": True,
        "requires_admin_approval": False,
        "conflict": False,
        "evidence_indexes": [0],
        "rationale": "Permission appears direct but confidence is below the configured threshold.",
    }
    service._ad_policy_llm_client = SimpleNamespace(
        generate=AsyncMock(side_effect=[json.dumps(verdict), json.dumps(verdict)])
    )
    evidence = [{"source": "pinned_message", "text": "本群允许软广，可直接发布"}]

    result = await service._evaluate_group_ad_rules_with_ai(
        evidence,
        service._evaluate_group_ad_rules(evidence),
        {
            "ad_policy_ai_enabled": True,
            "ad_policy_ai_model": "gpt-5.6-terra",
            "ad_policy_ai_timeout_seconds": 30,
            "ad_policy_ai_min_confidence": 95,
            "ad_policy_ai_require_second_pass": True,
        },
    )

    assert result.ad_allowed is None
    assert result.policy_mode == GroupAdPolicyMode.UNKNOWN.value
    assert result.reason == "group_rules_ai_consensus_failed"
    assert result.decision_source == "gpt-5.6-terra_two_pass"
    assert len(result.ai_reviews) == 2
    assert service._ad_policy_llm_client.generate.await_count == 2


def test_ad_policy_evidence_hash_is_stable_until_retention_bucket_changes():
    capacity = {
        "ad_policy_ai_enabled": True,
        "ad_policy_ai_model": "gpt-5.6-terra",
        "ad_policy_ai_min_confidence": 95,
        "ad_policy_ai_require_second_pass": True,
    }
    retained = [
        {
            "source": "recent_promotional_message",
            "text": "GPT Plus\n 低价通道",
            "message_id": 101,
            "sender_id": 201,
            "age_hours": 25,
        },
        {"source": "group_profile", "text": "title=AI交流群"},
    ]
    retained_reordered = [
        {"source": "group_profile", "text": "title=AI交流群"},
        {
            "source": "recent_promotional_message",
            "text": "GPT Plus 低价通道",
            "message_id": 101,
            "sender_id": 201,
            "age_hours": 50,
        },
    ]
    not_yet_retained = [
        {**retained[0], "age_hours": 23},
        retained[1],
    ]

    retained_hash = AcquisitionAutomationService._ad_policy_evidence_hash(retained, capacity)
    assert retained_hash == AcquisitionAutomationService._ad_policy_evidence_hash(
        retained_reordered,
        capacity,
    )
    assert retained_hash != AcquisitionAutomationService._ad_policy_evidence_hash(
        not_yet_retained,
        capacity,
    )


@pytest.mark.asyncio
async def test_group_rules_audit_reuses_matching_evidence_hash_without_llm(test_db, monkeypatch):
    service = AcquisitionAutomationService(test_db)
    capacity = {
        "ad_policy_ai_enabled": True,
        "ad_policy_ai_model": "gpt-5.6-terra",
        "ad_policy_ai_timeout_seconds": 30,
        "ad_policy_ai_min_confidence": 95,
        "ad_policy_ai_require_second_pass": True,
    }
    evidence = [{"source": "pinned_message", "text": "本群允许软广，可直接发布"}]
    evidence_hash = service._ad_policy_evidence_hash(evidence, capacity)
    profile = GroupAdProfile(
        group_id=1,
        telegram_group_id=930004,
        ad_policy_mode=GroupAdPolicyMode.SOFT_AD_ALLOWED.value,
        ad_policy_confidence=98,
        ad_policy_verified_at=datetime.utcnow(),
        ad_policy_evidence_hash=evidence_hash,
    )
    service._ad_policy_llm_client = SimpleNamespace(generate=AsyncMock())
    monkeypatch.setattr(
        acquisition_automation, "get_ad_capacity_settings", AsyncMock(return_value=capacity)
    )
    monkeypatch.setattr(service, "_fetch_messages_before", AsyncMock(return_value=[]))
    monkeypatch.setattr(service, "_read_group_ad_rules_evidence", AsyncMock(return_value=evidence))

    result = await service._audit_group_ad_rules(
        SimpleNamespace(),
        SimpleNamespace(),
        [],
        profile=profile,
    )

    assert result.ad_allowed is True
    assert result.policy_mode == GroupAdPolicyMode.SOFT_AD_ALLOWED.value
    assert result.decision_source == "evidence_cache"
    assert result.evidence_hash == evidence_hash
    assert result.cache_hit is True
    service._ad_policy_llm_client.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_ad_policy_is_reaudited_only_after_twenty_four_hours(test_db, monkeypatch):
    now = datetime.utcnow()
    account = TelegramAccount(
        phone="+15550003004",
        identifier="+15550003004",
        session_name="ad_policy_reaudit_cooldown",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    recent_group = Group(
        group_id=930005, title="Recent Unknown", level=GroupLevel.A, status="active"
    )
    due_group = Group(group_id=930006, title="Due Unknown", level=GroupLevel.A, status="active")
    test_db.add_all([account, recent_group, due_group])
    await test_db.flush()
    test_db.add_all(
        [
            GroupAccountMembership(
                group_id=recent_group.id,
                telegram_group_id=recent_group.group_id,
                account_id=account.id,
                status="joined",
            ),
            GroupAccountMembership(
                group_id=due_group.id,
                telegram_group_id=due_group.group_id,
                account_id=account.id,
                status="joined",
            ),
            GroupAdProfile(
                group_id=recent_group.id,
                telegram_group_id=recent_group.group_id,
                ad_policy_mode=GroupAdPolicyMode.UNKNOWN.value,
                ad_policy_verified_at=now - timedelta(hours=23),
            ),
            GroupAdProfile(
                group_id=due_group.id,
                telegram_group_id=due_group.group_id,
                ad_policy_mode=GroupAdPolicyMode.UNKNOWN.value,
                ad_policy_verified_at=now - timedelta(hours=25),
            ),
        ]
    )
    await test_db.commit()
    service = AcquisitionAutomationService(test_db)
    monkeypatch.setattr(service, "_sync_account_pool", AsyncMock())

    result = await service.refresh_group_ad_policies(limit=10, dry_run=True)

    assert result["processed"] == 1
    assert result["skipped"] == 1
    assert result["details"] == [
        {
            "group_id": due_group.id,
            "telegram_group_id": due_group.group_id,
            "account_id": account.id,
            "action": "would_audit_ad_policy",
        }
    ]


@pytest.mark.asyncio
async def test_group_ad_policy_audit_waits_when_all_accounts_are_busy(test_db, monkeypatch):
    now = datetime.utcnow()
    account = TelegramAccount(
        phone="+15550003005",
        identifier="+15550003005",
        session_name="ad_policy_busy",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    group = Group(group_id=930007, title="Busy Audit", level=GroupLevel.A, status="active")
    test_db.add_all([account, group])
    await test_db.flush()
    test_db.add_all(
        [
            GroupAccountMembership(
                group_id=group.id,
                telegram_group_id=group.group_id,
                account_id=account.id,
                status="joined",
            ),
            GroupAdProfile(
                group_id=group.id,
                telegram_group_id=group.group_id,
                ad_policy_mode=GroupAdPolicyMode.UNKNOWN.value,
                ad_policy_verified_at=now - timedelta(hours=25),
            ),
        ]
    )
    await test_db.commit()
    service = AcquisitionAutomationService(test_db)
    monkeypatch.setattr(service, "_sync_account_pool", AsyncMock())
    service.account_pool = SimpleNamespace(
        acquire_by_id=AsyncMock(return_value=None),
        release=AsyncMock(),
    )

    result = await service.refresh_group_ad_policies(limit=10)

    assert result["processed"] == 1
    assert result["skipped"] == 1
    assert result["failed"] == 0
    assert result["details"][0]["action"] == "waiting_account_available"
    assert result["details"][0]["retryable"] is True


@pytest.mark.asyncio
async def test_group_ad_policy_audit_tries_next_joined_account(test_db, monkeypatch):
    now = datetime.utcnow()
    first = TelegramAccount(
        phone="+15550003006",
        identifier="+15550003006",
        session_name="ad_policy_first_busy",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    second = TelegramAccount(
        phone="+15550003007",
        identifier="+15550003007",
        session_name="ad_policy_second_available",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    group = Group(group_id=930008, title="Fallback Audit", level=GroupLevel.A, status="active")
    test_db.add_all([first, second, group])
    await test_db.flush()
    test_db.add_all(
        [
            GroupAccountMembership(
                group_id=group.id,
                telegram_group_id=group.group_id,
                account_id=first.id,
                status="joined",
            ),
            GroupAccountMembership(
                group_id=group.id,
                telegram_group_id=group.group_id,
                account_id=second.id,
                status="joined",
            ),
            GroupAdProfile(
                group_id=group.id,
                telegram_group_id=group.group_id,
                ad_policy_mode=GroupAdPolicyMode.UNKNOWN.value,
                ad_policy_verified_at=now - timedelta(hours=25),
            ),
        ]
    )
    await test_db.commit()
    client = SimpleNamespace(get_entity=AsyncMock(return_value=SimpleNamespace()))
    wrapper = SimpleNamespace(client=client)
    pool = SimpleNamespace(
        acquire_by_id=AsyncMock(side_effect=[None, wrapper]),
        release=AsyncMock(),
    )
    service = AcquisitionAutomationService(test_db, account_pool=pool)
    monkeypatch.setattr(service, "_sync_account_pool", AsyncMock())
    monkeypatch.setattr(service, "_fetch_recent_messages", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        service,
        "_audit_group_ad_rules",
        AsyncMock(
            return_value=SimpleNamespace(
                cache_hit=False,
                ai_reviews=[],
                ad_allowed=True,
                reason="group_rules_allow_ads",
                policy_mode=GroupAdPolicyMode.SOFT_AD_ALLOWED.value,
                confidence=95,
                decision_source="group_rules",
                details=lambda: {},
            )
        ),
    )
    monkeypatch.setattr(service, "_sync_group_ad_policy_from_audit", AsyncMock())

    result = await service.refresh_group_ad_policies(limit=10)

    assert result["updated"] == 1
    assert result["failed"] == 0
    assert result["details"][0]["account_id"] == second.id
    assert pool.acquire_by_id.await_count == 2
    pool.release.assert_awaited_once_with(wrapper)


@pytest.mark.asyncio
async def test_group_ad_policy_audit_marks_lost_membership(test_db, monkeypatch):
    now = datetime.utcnow()
    account = TelegramAccount(
        phone="+15550003008",
        identifier="+15550003008",
        session_name="ad_policy_membership_lost",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    group = Group(group_id=930009, title="Lost Audit", level=GroupLevel.A, status="active")
    test_db.add_all([account, group])
    await test_db.flush()
    membership = GroupAccountMembership(
        group_id=group.id,
        telegram_group_id=group.group_id,
        account_id=account.id,
        status="joined",
        ad_status="active",
    )
    test_db.add_all(
        [
            membership,
            GroupAdProfile(
                group_id=group.id,
                telegram_group_id=group.group_id,
                ad_policy_mode=GroupAdPolicyMode.UNKNOWN.value,
                ad_policy_verified_at=now - timedelta(hours=25),
            ),
        ]
    )
    await test_db.commit()
    client = SimpleNamespace(
        get_entity=AsyncMock(
            side_effect=RuntimeError(
                "The channel specified is private and you lack permission; you were banned"
            )
        )
    )
    wrapper = SimpleNamespace(client=client)
    pool = SimpleNamespace(
        acquire_by_id=AsyncMock(return_value=wrapper),
        release=AsyncMock(),
    )
    service = AcquisitionAutomationService(test_db, account_pool=pool)
    monkeypatch.setattr(service, "_sync_account_pool", AsyncMock())

    result = await service.refresh_group_ad_policies(limit=10)
    await test_db.refresh(membership)

    assert result["failed"] == 1
    assert membership.status == "banned"
    assert membership.ad_status == "blocked"
    assert membership.left_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy_mode", "policy_source", "clean_days"),
    [
        (GroupAdPolicyMode.SOFT_AD_ALLOWED.value, "group_rules", 5),
        (GroupAdPolicyMode.SOFT_AD_ALLOWED.value, "manual", 3),
        (GroupAdPolicyMode.HIGH_VOLUME_AD_ALLOWED.value, "manual", 3),
    ],
)
async def test_premium_evidence_tier_requires_clean_24h_samples_and_real_conversion(
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
    group = Group(
        group_id=930010 + clean_days, title="Premium Candidate", level=GroupLevel.A, status="active"
    )
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
    assert profile.daily_capacity == 0


@pytest.mark.asyncio
async def test_manual_policy_api_synchronizes_group_status_and_evidence_tier(test_db, client):
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
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1,
        "username": "test",
        "role": "admin",
    }
    try:
        low_confidence = await client.put(
            f"/api/automation/ads/group-profiles/{group.id}/policy",
            json={
                "mode": GroupAdPolicyMode.SOFT_AD_ALLOWED.value,
                "confidence": 89,
                "note": "insufficient",
            },
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
        assert "daily_capacity" not in allowed_data
        await test_db.refresh(group)
        assert group.status == "active"

        forbidden = await client.put(
            f"/api/automation/ads/group-profiles/{group.id}/policy",
            json={
                "mode": GroupAdPolicyMode.FORBIDDEN.value,
                "confidence": 100,
                "note": "admin revoked",
            },
        )
        assert forbidden.status_code == 200
        forbidden_data = forbidden.json()["data"]
        assert forbidden_data["ad_tier"] == GroupAdTier.BLOCKED.value
        assert "daily_capacity" not in forbidden_data
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
    assert (
        await service._mark_ad_survival_checkpoint(log, sent_at + timedelta(minutes=2))
        == "pending_one_hour"
    )
    assert log.survival_status == AdSurvivalStatus.PENDING.value
    assert log.survived_two_minute_at is not None

    assert (
        await service._mark_ad_survival_checkpoint(log, sent_at + timedelta(hours=1))
        == "pending_twenty_four_hour"
    )
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
@pytest.mark.parametrize(
    (
        "leave_enabled",
        "block_enabled",
        "expected_group_status",
        "expected_policy_mode",
        "expected_membership_status",
        "expected_ad_status",
    ),
    [
        (False, False, "active", GroupAdPolicyMode.UNKNOWN.value, "joined", "paused"),
        (True, False, "active", GroupAdPolicyMode.UNKNOWN.value, "left", "blocked"),
        (False, True, "ad_blocked", GroupAdPolicyMode.FORBIDDEN.value, "joined", "blocked"),
    ],
)
async def test_deleted_policy_probe_honors_leave_and_group_block_switches(
    test_db,
    monkeypatch,
    leave_enabled,
    block_enabled,
    expected_group_status,
    expected_policy_mode,
    expected_membership_status,
    expected_ad_status,
):
    now = datetime.utcnow()
    account = TelegramAccount(
        phone="+15550003012",
        identifier="+15550003012",
        session_name=f"deleted_probe_switch_{leave_enabled}_{block_enabled}",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    group = Group(
        group_id=930022,
        title="Deleted Probe Switch Group",
        level=GroupLevel.A,
        status="active",
    )
    campaign = AdCampaign(name="Deleted Probe Switch Campaign", enabled=True, status="active")
    profile = GroupAdProfile(
        group_id=0,
        telegram_group_id=group.group_id,
        ad_policy_mode=GroupAdPolicyMode.UNKNOWN_PROBE.value,
        ad_policy_probe_status="sent",
        ad_policy_probe_at=now - timedelta(hours=1),
        ad_tier=GroupAdTier.TRIAL.value,
        daily_capacity=1,
    )
    membership = GroupAccountMembership(
        group_id=0,
        telegram_group_id=group.group_id,
        account_id=0,
        status="joined",
        join_method="manual",
        probe_status="success",
        warmup_status="ad_eligible",
        ad_status="active",
    )
    test_db.add_all([account, group, campaign])
    await test_db.flush()
    profile.group_id = group.id
    membership.group_id = group.id
    membership.account_id = account.id
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

    capacity = {
        **acquisition_automation.DEFAULT_AD_CAPACITY_SETTINGS,
        "leave_on_deleted_ad": leave_enabled,
        "block_group_on_probe_failure": block_enabled,
    }
    monkeypatch.setattr(
        acquisition_automation,
        "get_ad_capacity_settings",
        AsyncMock(return_value=capacity),
    )
    service = AcquisitionAutomationService(test_db)
    service._leave_group = AsyncMock(return_value=None)

    await service._mark_ad_survival_deleted(log, now, "message_missing_or_deleted")
    await test_db.refresh(group)
    await test_db.refresh(profile)
    await test_db.refresh(membership)

    assert group.status == expected_group_status
    assert profile.ad_policy_mode == expected_policy_mode
    assert membership.status == expected_membership_status
    assert membership.ad_status == expected_ad_status
    if leave_enabled:
        service._leave_group.assert_awaited_once()
    else:
        service._leave_group.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("block_enabled", [False, True])
async def test_write_probe_failure_honors_group_block_switch(test_db, monkeypatch, block_enabled):
    account = TelegramAccount(
        phone="+15550003013",
        identifier="+15550003013",
        session_name=f"write_probe_switch_{block_enabled}",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    group = Group(group_id=930023, title="Write Probe Switch Group", level=GroupLevel.A, status="active")
    test_db.add_all([account, group])
    await test_db.flush()
    membership = GroupAccountMembership(
        group_id=group.id,
        telegram_group_id=group.group_id,
        account_id=account.id,
        status="joined",
        join_method="manual",
        probe_status="scheduled",
        warmup_status="probe_scheduled",
        ad_status="warming",
    )
    membership.group = group
    test_db.add(membership)
    await test_db.commit()
    capacity = {
        **acquisition_automation.DEFAULT_AD_CAPACITY_SETTINGS,
        "block_group_on_probe_failure": block_enabled,
    }
    monkeypatch.setattr(
        acquisition_automation,
        "get_ad_capacity_settings",
        AsyncMock(return_value=capacity),
    )
    wrapper = SimpleNamespace(record_message=MagicMock())
    pool = SimpleNamespace(
        acquire_by_id=AsyncMock(return_value=wrapper),
        release=AsyncMock(),
    )
    service = AcquisitionAutomationService(test_db, account_pool=pool)
    service.telegram_execution.send_group_message = AsyncMock(
        side_effect=RuntimeError("ChatWriteForbiddenError")
    )
    service._handle_group_control_ad_failure = AsyncMock()
    service._pause_account_if_group_control_looks_account_wide = AsyncMock()

    result = await service._send_ad_probe(account.id, membership)
    assert result == "ad_probe_group_control"
    await test_db.refresh(group)
    await test_db.refresh(membership)
    assert group.status == ("ad_blocked" if block_enabled else "active")
    assert membership.warmup_status == "blocked"
    assert membership.probe_status == "failed"
    assert membership.ad_status == "blocked"
    if block_enabled:
        profile = (
            await test_db.execute(
                select(GroupAdProfile).where(GroupAdProfile.group_id == group.id)
            )
        ).scalar_one()
        assert profile.ad_policy_mode == GroupAdPolicyMode.FORBIDDEN.value
        assert profile.ad_policy_probe_status == "failed"
    else:
        profile = (
            await test_db.execute(
                select(GroupAdProfile).where(GroupAdProfile.group_id == group.id)
            )
        ).scalar_one_or_none()
        assert profile is None
    pool.release.assert_awaited_once_with(wrapper)


@pytest.mark.asyncio
async def test_expired_membership_ad_pause_is_reactivated_without_resetting_streak(
    test_db,
):
    now = datetime.utcnow()
    account = TelegramAccount(
        phone="+15550003041",
        identifier="+15550003041",
        session_name="expired_membership_pause",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    future_account = TelegramAccount(
        phone="+15550003042",
        identifier="+15550003042",
        session_name="future_membership_pause",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    blocked_account = TelegramAccount(
        phone="+15550003043",
        identifier="+15550003043",
        session_name="blocked_membership_pause",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    group = Group(
        group_id=930041,
        title="Expired Pause Group",
        level=GroupLevel.A,
        status="active",
    )
    test_db.add_all([account, future_account, blocked_account, group])
    await test_db.flush()
    expired = GroupAccountMembership(
        group_id=group.id,
        telegram_group_id=group.group_id,
        account_id=account.id,
        status="joined",
        join_method="manual",
        probe_status="success",
        ad_status="paused",
        ad_failure_streak=1,
        ad_pause_until=now - timedelta(minutes=1),
    )
    future = GroupAccountMembership(
        group_id=group.id,
        telegram_group_id=group.group_id,
        account_id=future_account.id,
        status="joined",
        join_method="manual",
        probe_status="success",
        ad_status="paused",
        ad_failure_streak=2,
        ad_pause_until=now + timedelta(minutes=1),
    )
    blocked = GroupAccountMembership(
        group_id=group.id,
        telegram_group_id=group.group_id,
        account_id=blocked_account.id,
        status="joined",
        join_method="manual",
        probe_status="success",
        ad_status="blocked",
        ad_failure_streak=6,
        ad_pause_until=now - timedelta(minutes=1),
    )
    test_db.add_all([expired, future, blocked])
    await test_db.commit()

    service = AcquisitionAutomationService(test_db)
    resumed = await service._resume_expired_membership_ad_pauses(now)
    await test_db.refresh(expired)
    await test_db.refresh(future)
    await test_db.refresh(blocked)

    assert resumed == 1
    assert expired.ad_status == "active"
    assert expired.ad_pause_until is None
    assert expired.ad_failure_streak == 1
    assert future.ad_status == "paused"
    assert future.ad_pause_until is not None
    assert blocked.ad_status == "blocked"
    assert blocked.ad_failure_streak == 6


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
async def test_telegram_success_never_releases_dispatcher_budget_when_log_confirmation_fails(
    monkeypatch,
):
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
    service._growth_ad_health_allowed = AsyncMock(return_value=True)
    service._claim_ad_schedule_state = AsyncMock(return_value=(1, "schedule-token", None))
    monkeypatch.setattr(
        acquisition_automation,
        "get_ad_delivery_execution_settings",
        AsyncMock(return_value={"job_lease_seconds": 300}),
    )
    service._choose_delivery_creative = AsyncMock(return_value=creative)
    service._ad_skip_reason = AsyncMock(return_value=None)
    service._reserve_ad_delivery_target = AsyncMock(return_value=True)
    service._is_owned_group_ad_domain_excluded = AsyncMock(return_value=False)
    service._record_ad_delivery = AsyncMock(return_value=pending_log)
    service._claim_growth_campaign_daily_quota = AsyncMock(return_value=(pending_log, None))
    service._send_ad = AsyncMock(return_value=9001)
    service._finalize_ad_delivery_log = AsyncMock(
        side_effect=RuntimeError("database commit failed")
    )
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
    service._release_ad_delivery_budget.assert_not_awaited()
    db.rollback.assert_awaited_once()


async def _create_policy_probe_target(test_db, now: datetime, suffix: int):
    account = TelegramAccount(
        phone=f"+15550004{suffix:03d}",
        identifier=f"+15550004{suffix:03d}",
        session_name=f"policy_probe_target_{suffix}",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    group = Group(
        group_id=940000 + suffix,
        title=f"Policy Probe Target {suffix}",
        level=GroupLevel.A,
        status="active",
    )
    campaign = AdCampaign(
        name=f"Policy Probe Campaign {suffix}",
        enabled=True,
        status="active",
        max_sends_per_account_per_day=10,
        max_sends_per_group_per_day=10,
    )
    test_db.add_all([account, group, campaign])
    await test_db.flush()
    profile = GroupAdProfile(
        group_id=group.id,
        telegram_group_id=group.group_id,
        ad_policy_mode=GroupAdPolicyMode.UNKNOWN.value,
        ad_policy_confidence=0,
        ad_policy_source="fixture_source",
        ad_policy_probe_status="not_started",
        ad_policy_probe_at=None,
        ad_policy_probe_error="previous_probe_error",
        ad_tier=GroupAdTier.OBSERVING.value,
        daily_capacity=0,
    )
    membership = GroupAccountMembership(
        group_id=group.id,
        telegram_group_id=group.group_id,
        account_id=account.id,
        status="joined",
        join_method="manual",
        probe_status="success",
        ad_status="active",
        interaction_started_at=now - timedelta(days=10),
        first_ad_allowed_at=now - timedelta(days=5),
        ad_eligible_after=now - timedelta(days=1),
    )
    binding = AccountAdBinding(
        account_id=account.id,
        ad_campaign_id=campaign.id,
        enabled=True,
    )
    test_db.add_all([profile, membership, binding])
    await test_db.commit()
    return SimpleNamespace(
        account=account,
        group=group,
        campaign=campaign,
        profile=profile,
        membership=membership,
    )


def _configure_policy_probe_service(monkeypatch, service, now_fn) -> None:
    monkeypatch.setattr(acquisition_automation, "_now", now_fn)
    monkeypatch.setattr(
        acquisition_automation,
        "get_ad_delivery_execution_settings",
        AsyncMock(
            return_value={
                "job_lease_seconds": 300,
                "growth_group_global_cooldown_seconds": 86400,
                "dispatcher_interval_seconds": 60,
            }
        ),
    )
    monkeypatch.setattr(service, "_get_account_operation_config", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_ad_account_risk_skip_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_group_can_receive_ads", AsyncMock(return_value=True))
    monkeypatch.setattr(service, "_ad_window_skip_reason", MagicMock(return_value=None))
    monkeypatch.setattr(service, "_sync_account_pool", AsyncMock())


@pytest.mark.asyncio
async def test_stale_ad_policy_probe_keeps_group_lock_until_sending_state_is_committed(
    test_db,
    monkeypatch,
):
    now = datetime.utcnow()
    telegram_sent_at = now + timedelta(minutes=5)
    telegram_returned = False

    def current_time():
        return telegram_sent_at if telegram_returned else now

    async def send_probe(*_args, **_kwargs):
        nonlocal telegram_returned
        telegram_returned = True
        return 9000

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
        interaction_started_at=now - timedelta(days=10),
        first_ad_allowed_at=now - timedelta(days=5),
        ad_eligible_after=now - timedelta(days=1),
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
    monkeypatch.setattr(acquisition_automation, "_now", current_time)
    monkeypatch.setattr(
        acquisition_automation,
        "get_ad_delivery_execution_settings",
        AsyncMock(
            return_value={
                "job_lease_seconds": 300,
                "growth_group_global_cooldown_seconds": 86400,
                "dispatcher_interval_seconds": 60,
            }
        ),
    )
    wrapper = SimpleNamespace(record_message=MagicMock())
    pool = SimpleNamespace(
        acquire_by_id=AsyncMock(return_value=wrapper),
        release=AsyncMock(),
    )
    service = AcquisitionAutomationService(test_db, account_pool=pool)
    monkeypatch.setattr(service, "_get_account_operation_config", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_ad_account_risk_skip_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_group_can_receive_ads", AsyncMock(return_value=True))
    monkeypatch.setattr(service, "_ad_window_skip_reason", MagicMock(return_value=None))
    monkeypatch.setattr(service, "_sync_account_pool", AsyncMock())
    pending_log = SimpleNamespace(id=1, reservation_token="probe-reservation")
    monkeypatch.setattr(
        service,
        "_claim_growth_campaign_daily_quota",
        AsyncMock(return_value=(pending_log, None)),
    )
    monkeypatch.setattr(service, "_send_ad_text", AsyncMock(side_effect=send_probe))
    monkeypatch.setattr(service, "_finalize_ad_delivery_log", AsyncMock())

    result = await service.send_group_ad_policy_probe(group.id, account_id=account.id)

    assert result["ad_policy_probe_status"] == "sent"
    assert commit_states[0] == "sending"
    assert commit_states[-1] == "sent"
    assert "sent" not in commit_states[:-1]
    pool.acquire_by_id.assert_awaited_once_with(
        account.id,
        purpose="ad_policy_probe",
        raise_on_lease_failure=True,
    )
    service._claim_growth_campaign_daily_quota.assert_awaited_once_with(
        campaign=campaign,
        account_id=account.id,
        group=group,
        creative=None,
    )
    assert service._send_ad_text.await_args.kwargs["acquired_account"] is wrapper
    pool.release.assert_awaited_once_with(wrapper)
    schedule = (
        await test_db.execute(
            select(AdDeliveryScheduleState).where(
                AdDeliveryScheduleState.campaign_id == campaign.id,
                AdDeliveryScheduleState.account_id == account.id,
                AdDeliveryScheduleState.group_id == group.id,
            )
        )
    ).scalar_one()
    assert schedule.status == AdScheduleStatus.IDLE.value
    assert schedule.last_success_at == telegram_sent_at
    assert schedule.next_due_at == telegram_sent_at + timedelta(hours=24)
    assert profile.ad_policy_probe_at == telegram_sent_at
    assert profile.ad_policy_expires_at == telegram_sent_at + timedelta(days=2)
    assert service._finalize_ad_delivery_log.await_args.kwargs["sent_at"] == telegram_sent_at


@pytest.mark.asyncio
async def test_recent_formal_ad_blocks_policy_probe_without_blocking_another_group(
    test_db,
    monkeypatch,
):
    now = datetime(2026, 8, 30, 8, 0, 0)
    account = TelegramAccount(
        phone="+15550003001",
        identifier="+15550003001",
        session_name="recent_formal_ad_probe",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    other_account = TelegramAccount(
        phone="+15550003002",
        identifier="+15550003002",
        session_name="cross_account_formal_ad",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    recent_group = Group(
        group_id=930001,
        title="Recent Formal Ad Group",
        level=GroupLevel.A,
        status="active",
    )
    other_group = Group(
        group_id=930003,
        title="Independent Probe Group",
        level=GroupLevel.A,
        status="active",
    )
    sync_error_group = Group(
        group_id=930004,
        title="Sync Error Probe Group",
        level=GroupLevel.A,
        status="active",
    )
    daily_limit_group = Group(
        group_id=930005,
        title="Daily Limit Probe Group",
        level=GroupLevel.A,
        status="active",
    )
    campaign = AdCampaign(
        name="Probe Frequency Campaign",
        enabled=True,
        status="active",
        max_sends_per_account_per_day=1,
        max_sends_per_group_per_day=1,
    )
    other_campaign = AdCampaign(
        name="Cross-campaign Formal Ad",
        enabled=True,
        status="active",
    )
    creative = AdCreative(name="Formal Creative", content="formal ad", enabled=True)
    test_db.add_all(
        [
            account,
            other_account,
            recent_group,
            other_group,
            sync_error_group,
            daily_limit_group,
            campaign,
            other_campaign,
            creative,
        ]
    )
    await test_db.flush()

    memberships = []
    for group in (recent_group, other_group, sync_error_group, daily_limit_group):
        memberships.append(
            GroupAccountMembership(
                group_id=group.id,
                telegram_group_id=group.group_id,
                account_id=account.id,
                status="joined",
                join_method="manual",
                probe_status="success",
                ad_status="active",
                interaction_started_at=now - timedelta(days=10),
                first_ad_allowed_at=now - timedelta(days=5),
                ad_eligible_after=now - timedelta(days=1),
            )
        )
        test_db.add(
            GroupAdProfile(
                group_id=group.id,
                telegram_group_id=group.group_id,
                ad_policy_mode=GroupAdPolicyMode.UNKNOWN.value,
                ad_policy_probe_status="not_started",
                ad_tier=GroupAdTier.OBSERVING.value,
                daily_capacity=0,
            )
        )

    binding = AccountAdBinding(
        account_id=account.id,
        ad_campaign_id=campaign.id,
        enabled=True,
    )
    recent_schedule = AdDeliveryScheduleState(
        campaign_id=campaign.id,
        account_id=account.id,
        group_id=recent_group.id,
        telegram_group_id=recent_group.group_id,
        next_due_at=now - timedelta(minutes=1),
        status=AdScheduleStatus.IDLE.value,
    )
    test_db.add_all(
        [
            *memberships,
            binding,
            recent_schedule,
            AdDeliveryLog(
                account_id=other_account.id,
                group_id=recent_group.id,
                telegram_group_id=recent_group.group_id,
                ad_campaign_id=other_campaign.id,
                creative_id=creative.id,
                status=DeliveryStatus.SUCCESS.value,
                sent_at=now - timedelta(hours=1),
            ),
        ]
    )
    await test_db.commit()

    monkeypatch.setattr(acquisition_automation, "_now", lambda: now)
    monkeypatch.setattr(
        acquisition_automation,
        "get_ad_delivery_execution_settings",
        AsyncMock(
            return_value={
                "job_lease_seconds": 300,
                "growth_group_global_cooldown_seconds": 86400,
                "dispatcher_interval_seconds": 60,
            }
        ),
    )
    wrapper = SimpleNamespace(record_message=MagicMock())
    pool = SimpleNamespace(
        acquire_by_id=AsyncMock(return_value=wrapper),
        release=AsyncMock(),
    )
    service = AcquisitionAutomationService(test_db, account_pool=pool)
    monkeypatch.setattr(service, "_get_account_operation_config", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_ad_account_risk_skip_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_group_can_receive_ads", AsyncMock(return_value=True))
    monkeypatch.setattr(service, "_ad_window_skip_reason", MagicMock(return_value=None))
    monkeypatch.setattr(service, "_sync_account_pool", AsyncMock())
    monkeypatch.setattr(service, "_send_ad_text", AsyncMock(return_value=9001))

    with pytest.raises(RuntimeError, match="^delivery_schedule_not_due$"):
        await service.send_group_ad_policy_probe(recent_group.id, account_id=account.id)
    service._send_ad_text.assert_not_awaited()
    await test_db.refresh(recent_schedule)
    assert recent_schedule.status == AdScheduleStatus.IDLE.value
    assert recent_schedule.last_success_at == now - timedelta(hours=1)
    assert recent_schedule.next_due_at == now + timedelta(hours=23)

    service._sync_account_pool.side_effect = RuntimeError("pool sync failed")
    with pytest.raises(RuntimeError, match="^unknown:pool sync failed$"):
        await service.send_group_ad_policy_probe(sync_error_group.id, account_id=account.id)
    service._sync_account_pool.side_effect = None

    sync_error_profile = (
        await test_db.execute(
            select(GroupAdProfile).where(GroupAdProfile.group_id == sync_error_group.id)
        )
    ).scalar_one()
    sync_error_schedule = (
        await test_db.execute(
            select(AdDeliveryScheduleState).where(
                AdDeliveryScheduleState.campaign_id == campaign.id,
                AdDeliveryScheduleState.account_id == account.id,
                AdDeliveryScheduleState.group_id == sync_error_group.id,
            )
        )
    ).scalar_one()
    assert sync_error_schedule.status == AdScheduleStatus.RETRY.value
    assert sync_error_schedule.lock_token is None
    assert sync_error_schedule.lease_expires_at is None
    assert sync_error_schedule.last_reason == "unknown:pool sync failed"
    assert sync_error_profile.ad_policy_mode == GroupAdPolicyMode.UNKNOWN.value
    assert sync_error_profile.ad_policy_probe_status == "not_started"
    assert sync_error_profile.ad_policy_probe_at is None
    assert sync_error_profile.ad_policy_probe_error == "unknown:pool sync failed"
    sync_error_logs = list(
        (
            await test_db.execute(
                select(AdDeliveryLog).where(AdDeliveryLog.group_id == sync_error_group.id)
            )
        )
        .scalars()
        .all()
    )
    assert sync_error_logs == []

    result = await service.send_group_ad_policy_probe(other_group.id, account_id=account.id)

    assert result["group_id"] == other_group.id
    assert result["message_id"] == 9001
    service._send_ad_text.assert_awaited_once()
    other_schedule = (
        await test_db.execute(
            select(AdDeliveryScheduleState).where(
                AdDeliveryScheduleState.campaign_id == campaign.id,
                AdDeliveryScheduleState.account_id == account.id,
                AdDeliveryScheduleState.group_id == other_group.id,
            )
        )
    ).scalar_one()
    assert other_schedule.status == AdScheduleStatus.IDLE.value
    assert other_schedule.last_success_at == now
    assert other_schedule.next_due_at == now + timedelta(hours=24)
    pool.acquire_by_id.assert_awaited_once_with(
        account.id,
        purpose="ad_policy_probe",
        raise_on_lease_failure=True,
    )
    pool.release.assert_awaited_once_with(wrapper)

    with pytest.raises(RuntimeError, match="^campaign_account_daily_limit$"):
        await service.send_group_ad_policy_probe(daily_limit_group.id, account_id=account.id)
    service._send_ad_text.assert_awaited_once()
    daily_limit_schedule = (
        await test_db.execute(
            select(AdDeliveryScheduleState).where(
                AdDeliveryScheduleState.campaign_id == campaign.id,
                AdDeliveryScheduleState.account_id == account.id,
                AdDeliveryScheduleState.group_id == daily_limit_group.id,
            )
        )
    ).scalar_one_or_none()
    assert daily_limit_schedule is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lease_error", "expected_reason"),
    [
        (
            AccountOperationLeaseBusy("account operation lease busy"),
            "account_operation_lease_busy",
        ),
        (
            AccountOperationLeaseUnavailable("redis unavailable"),
            "account_operation_lease_unavailable",
        ),
    ],
)
async def test_policy_probe_lease_defer_restores_profile_without_consuming_attempt(
    test_db,
    monkeypatch,
    lease_error,
    expected_reason,
):
    now = datetime.utcnow()
    suffix = 10 if expected_reason.endswith("busy") else 11
    target = await _create_policy_probe_target(test_db, now, suffix)
    pool = SimpleNamespace(
        acquire_by_id=AsyncMock(side_effect=lease_error),
        release=AsyncMock(),
    )
    service = AcquisitionAutomationService(test_db, account_pool=pool)
    _configure_policy_probe_service(monkeypatch, service, lambda: now)
    service._claim_growth_campaign_daily_quota = AsyncMock()
    service._send_ad_text = AsyncMock()

    with pytest.raises(RuntimeError, match=f"^{expected_reason}$"):
        await service.send_group_ad_policy_probe(
            target.group.id,
            account_id=target.account.id,
        )

    await test_db.refresh(target.profile)
    schedule = (
        await test_db.execute(
            select(AdDeliveryScheduleState).where(
                AdDeliveryScheduleState.campaign_id == target.campaign.id,
                AdDeliveryScheduleState.account_id == target.account.id,
                AdDeliveryScheduleState.group_id == target.group.id,
            )
        )
    ).scalar_one()
    delivery_logs = list(
        (
            await test_db.execute(
                select(AdDeliveryLog).where(AdDeliveryLog.group_id == target.group.id)
            )
        )
        .scalars()
        .all()
    )
    attempt_events = list(
        (
            await test_db.execute(
                select(GroupAdPolicyEvent).where(
                    GroupAdPolicyEvent.group_id == target.group.id,
                    GroupAdPolicyEvent.reason
                    == acquisition_automation.AD_POLICY_PROBE_ATTEMPT_REASON,
                )
            )
        )
        .scalars()
        .all()
    )

    assert target.profile.ad_policy_mode == GroupAdPolicyMode.UNKNOWN.value
    assert target.profile.ad_policy_source == "fixture_source"
    assert target.profile.ad_policy_probe_status == "not_started"
    assert target.profile.ad_policy_probe_at is None
    assert target.profile.ad_policy_probe_account_id is None
    assert target.profile.ad_policy_probe_error == expected_reason
    assert schedule.status == AdScheduleStatus.RETRY.value
    assert schedule.lock_token is None
    assert schedule.lease_expires_at is None
    assert schedule.last_reason == expected_reason
    assert delivery_logs == []
    assert attempt_events == []
    service._claim_growth_campaign_daily_quota.assert_not_awaited()
    service._send_ad_text.assert_not_awaited()
    pool.release.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "expected_skipped", "expected_failed", "expected_action"),
    [
        ("account_operation_lease_busy", 1, 0, "waiting_account_available"),
        ("account_operation_lease_unavailable", 1, 0, "waiting_account_available"),
        ("ad_policy_probe_pre_send_unavailable", 1, 0, "deferred"),
        ("risk_guard_blocked:content_repeat_account", 1, 0, "deferred"),
    ],
)
async def test_auto_policy_probe_marks_lease_defers_retryable(
    test_db,
    monkeypatch,
    reason,
    expected_skipped,
    expected_failed,
    expected_action,
):
    now = datetime.utcnow()
    suffix = 20 if reason.endswith("busy") else 21
    target = await _create_policy_probe_target(test_db, now, suffix)
    capacity = {
        **acquisition_automation.DEFAULT_AD_CAPACITY_SETTINGS,
        "enabled": True,
        "ad_policy_auto_probe_enabled": True,
        "ad_policy_auto_probe_daily_limit_per_account": 1,
        "window_start_hour": 0,
        "window_end_hour": 0,
    }
    monkeypatch.setattr(acquisition_automation, "_now", lambda: now)
    monkeypatch.setattr(
        acquisition_automation,
        "get_ad_capacity_settings",
        AsyncMock(return_value=capacity),
    )
    service = AcquisitionAutomationService(test_db)
    service._ad_window_skip_reason = MagicMock(return_value=None)
    service._advance_disabled_group_warmup_to_write_probe = AsyncMock(
        return_value={"applied": False}
    )
    service.send_group_ad_policy_probe = AsyncMock(side_effect=RuntimeError(reason))

    result = await service.auto_probe_unknown_group_ad_policies()

    assert result["processed"] == 1
    assert result["skipped"] == expected_skipped
    assert result["failed"] == expected_failed
    assert result["errors"] == []
    assert result["details"] == [
        {
            "group_id": target.group.id,
            "telegram_group_id": target.group.group_id,
            "account_id": target.account.id,
            "error": reason,
            "action": expected_action,
            "reason": reason,
            "retryable": True,
        }
    ]
    service.send_group_ad_policy_probe.assert_awaited_once_with(
        target.group.id,
        account_id=target.account.id,
        source=acquisition_automation.AD_POLICY_PROBE_AUTO_SOURCE,
    )


@pytest.mark.asyncio
async def test_policy_probe_atomic_quota_loss_restores_state_and_releases_account(
    test_db,
    monkeypatch,
):
    now = datetime.utcnow()
    target = await _create_policy_probe_target(test_db, now, 12)
    wrapper = SimpleNamespace(record_message=MagicMock())
    pool = SimpleNamespace(
        acquire_by_id=AsyncMock(return_value=wrapper),
        release=AsyncMock(),
    )
    service = AcquisitionAutomationService(test_db, account_pool=pool)
    _configure_policy_probe_service(monkeypatch, service, lambda: now)
    service._claim_growth_campaign_daily_quota = AsyncMock(
        return_value=(None, "campaign_account_daily_limit")
    )
    service._send_ad_text = AsyncMock()

    with pytest.raises(RuntimeError, match="^campaign_account_daily_limit$"):
        await service.send_group_ad_policy_probe(
            target.group.id,
            account_id=target.account.id,
        )

    await test_db.refresh(target.profile)
    schedule = (
        await test_db.execute(
            select(AdDeliveryScheduleState).where(
                AdDeliveryScheduleState.campaign_id == target.campaign.id,
                AdDeliveryScheduleState.account_id == target.account.id,
                AdDeliveryScheduleState.group_id == target.group.id,
            )
        )
    ).scalar_one()
    attempt_events = list(
        (
            await test_db.execute(
                select(GroupAdPolicyEvent).where(
                    GroupAdPolicyEvent.group_id == target.group.id,
                    GroupAdPolicyEvent.reason
                    == acquisition_automation.AD_POLICY_PROBE_ATTEMPT_REASON,
                )
            )
        )
        .scalars()
        .all()
    )

    assert target.profile.ad_policy_mode == GroupAdPolicyMode.UNKNOWN.value
    assert target.profile.ad_policy_probe_status == "not_started"
    assert target.profile.ad_policy_probe_at is None
    assert target.profile.ad_policy_probe_error == "campaign_account_daily_limit"
    assert schedule.status == AdScheduleStatus.RETRY.value
    assert schedule.lock_token is None
    assert schedule.lease_expires_at is None
    assert schedule.last_reason == "campaign_account_daily_limit"
    assert attempt_events == []
    service._send_ad_text.assert_not_awaited()
    pool.release.assert_awaited_once_with(wrapper)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "block_reason",
    [
        "content_repeat_account",
        "content_repeat_target",
        "content_similar_target",
    ],
)
async def test_policy_probe_risk_guard_block_after_reservation_is_retryable_and_skipped(
    test_db,
    monkeypatch,
    block_reason,
):
    now = datetime.utcnow()
    target = await _create_policy_probe_target(test_db, now, 14)
    client = SimpleNamespace(send_message=AsyncMock())
    wrapper = SimpleNamespace(client=client, record_message=MagicMock())
    pool = SimpleNamespace(
        acquire_by_id=AsyncMock(return_value=wrapper),
        release=AsyncMock(),
    )
    risk_guard = SimpleNamespace(
        check_and_reserve=AsyncMock(
            return_value=SimpleNamespace(allowed=False, reason=block_reason)
        ),
        record_failure=AsyncMock(),
        record_success=AsyncMock(),
    )
    service = AcquisitionAutomationService(test_db, account_pool=pool)
    service.telegram_execution = TelegramExecutionService(risk_guard)
    _configure_policy_probe_service(monkeypatch, service, lambda: now)
    expected_reason = f"risk_guard_blocked:{block_reason}"

    with pytest.raises(RuntimeError, match=f"^{expected_reason}$"):
        await service.send_group_ad_policy_probe(
            target.group.id,
            account_id=target.account.id,
        )

    await test_db.refresh(target.profile)
    schedule = (
        await test_db.execute(
            select(AdDeliveryScheduleState).where(
                AdDeliveryScheduleState.campaign_id == target.campaign.id,
                AdDeliveryScheduleState.account_id == target.account.id,
                AdDeliveryScheduleState.group_id == target.group.id,
            )
        )
    ).scalar_one()
    delivery_log = (
        await test_db.execute(
            select(AdDeliveryLog).where(AdDeliveryLog.group_id == target.group.id)
        )
    ).scalar_one()
    attempt_events = list(
        (
            await test_db.execute(
                select(GroupAdPolicyEvent).where(
                    GroupAdPolicyEvent.group_id == target.group.id,
                    GroupAdPolicyEvent.reason
                    == acquisition_automation.AD_POLICY_PROBE_ATTEMPT_REASON,
                )
            )
        )
        .scalars()
        .all()
    )

    assert target.profile.ad_policy_mode == GroupAdPolicyMode.UNKNOWN.value
    assert target.profile.ad_policy_probe_status == "not_started"
    assert target.profile.ad_policy_probe_at is None
    assert target.profile.ad_policy_probe_error == expected_reason
    assert schedule.status == AdScheduleStatus.RETRY.value
    assert schedule.lock_token is None
    assert schedule.lease_expires_at is None
    assert schedule.last_reason == expected_reason
    assert (
        now + timedelta(seconds=acquisition_automation.AD_POLICY_PROBE_CONTENT_RETRY_SECONDS)
        <= schedule.next_due_at
        <= now
        + timedelta(
            seconds=acquisition_automation.AD_POLICY_PROBE_CONTENT_RETRY_SECONDS
            + acquisition_automation.AD_POLICY_PROBE_CONTENT_RETRY_JITTER_SECONDS
        )
    )
    assert delivery_log.status == DeliveryStatus.SKIPPED.value
    assert delivery_log.error == expected_reason
    assert delivery_log.reservation_token
    assert delivery_log.sent_at is None
    assert attempt_events == []
    client.send_message.assert_not_awaited()
    risk_guard.record_failure.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_policy_probe_continues_after_first_candidate_rolls_back(
    test_db,
    monkeypatch,
):
    now = datetime.utcnow()
    first = await _create_policy_probe_target(test_db, now, 31)
    second = await _create_policy_probe_target(test_db, now, 32)
    first_ids = (first.group.id, first.account.id)
    second_ids = (second.group.id, second.account.id)
    capacity = {
        **acquisition_automation.DEFAULT_AD_CAPACITY_SETTINGS,
        "enabled": True,
        "ad_policy_auto_probe_enabled": True,
        "ad_policy_auto_probe_daily_limit_per_account": 1,
        "window_start_hour": 0,
        "window_end_hour": 0,
    }
    monkeypatch.setattr(acquisition_automation, "_now", lambda: now)
    monkeypatch.setattr(
        acquisition_automation,
        "get_ad_capacity_settings",
        AsyncMock(return_value=capacity),
    )
    service = AcquisitionAutomationService(test_db)
    service._ad_window_skip_reason = MagicMock(return_value=None)
    service._advance_disabled_group_warmup_to_write_probe = AsyncMock(
        return_value={"applied": False}
    )
    calls: list[int] = []

    async def send_probe(group_id, **_kwargs):
        calls.append(group_id)
        await test_db.rollback()
        if len(calls) == 1:
            raise RuntimeError("risk_guard_blocked:content_repeat_account")
        return {
            "group_id": group_id,
            "account_id": _kwargs["account_id"],
            "message_id": 9002,
        }

    service.send_group_ad_policy_probe = AsyncMock(side_effect=send_probe)

    result = await service.auto_probe_unknown_group_ad_policies()

    assert result["processed"] == 2
    assert result["skipped"] == 1
    assert result["failed"] == 0
    assert result["succeeded"] == 1
    assert calls == [first_ids[0], second_ids[0]]
    assert {(detail["group_id"], detail["account_id"]) for detail in result["details"]} == {
        first_ids,
        second_ids,
    }


@pytest.mark.asyncio
async def test_disabled_warmup_progress_continues_after_first_candidate_rolls_back(
    test_db,
    monkeypatch,
):
    now = datetime.utcnow()
    first = await _create_policy_probe_target(test_db, now, 33)
    second = await _create_policy_probe_target(test_db, now, 34)
    first.membership.probe_status = "scheduled"
    second.membership.probe_status = "scheduled"
    await test_db.commit()
    expected_ids = {
        (first.group.id, first.account.id),
        (second.group.id, second.account.id),
    }
    monkeypatch.setattr(
        acquisition_automation,
        "get_group_ai_interaction_settings",
        AsyncMock(return_value={"enabled": False}),
    )
    service = AcquisitionAutomationService(test_db)
    service._sync_account_pool = AsyncMock()
    service._group_can_receive_ads = AsyncMock(return_value=True)
    service._ad_account_risk_skip_reason = AsyncMock(return_value=None)
    calls = 0

    async def warmup_result(_account_id, _membership, _now, *, dry_run):
        nonlocal calls
        calls += 1
        await test_db.rollback()
        return "ad_probe_risk_guard_skipped" if calls == 1 else "ad_probe_success_wait"

    service._ad_warmup_skip_reason = AsyncMock(side_effect=warmup_result)

    result = await service._advance_disabled_group_warmup_to_write_probe(
        now,
        dry_run=False,
    )

    assert result["processed"] == 2
    assert result["skipped"] == 1
    assert result["failed"] == 0
    assert result["succeeded"] == 1
    assert {
        (detail["group_id"], detail["account_id"]) for detail in result["details"]
    } == expected_ids


@pytest.mark.asyncio
async def test_policy_probe_confirmation_failure_stays_inflight_and_blocks_resend(
    test_db,
    monkeypatch,
):
    clock = {"now": datetime.utcnow()}
    target = await _create_policy_probe_target(test_db, clock["now"], 13)
    wrapper = SimpleNamespace(record_message=MagicMock())
    pool = SimpleNamespace(
        acquire_by_id=AsyncMock(return_value=wrapper),
        release=AsyncMock(),
    )
    service = AcquisitionAutomationService(test_db, account_pool=pool)
    _configure_policy_probe_service(monkeypatch, service, lambda: clock["now"])
    service._send_ad_text = AsyncMock(return_value=9100)
    service._finalize_ad_delivery_log = AsyncMock(
        side_effect=RuntimeError("database confirmation failed")
    )

    with pytest.raises(RuntimeError) as exc_info:
        await service.send_group_ad_policy_probe(
            target.group.id,
            account_id=target.account.id,
        )

    assert str(exc_info.value).startswith("telegram_sent_log_confirmation_failed:")
    pending_log = (
        await test_db.execute(
            select(AdDeliveryLog).where(AdDeliveryLog.group_id == target.group.id)
        )
    ).scalar_one()
    await test_db.refresh(target.profile)
    schedule = (
        await test_db.execute(
            select(AdDeliveryScheduleState).where(
                AdDeliveryScheduleState.campaign_id == target.campaign.id,
                AdDeliveryScheduleState.account_id == target.account.id,
                AdDeliveryScheduleState.group_id == target.group.id,
            )
        )
    ).scalar_one()
    attempt_events = list(
        (
            await test_db.execute(
                select(GroupAdPolicyEvent).where(
                    GroupAdPolicyEvent.group_id == target.group.id,
                    GroupAdPolicyEvent.reason
                    == acquisition_automation.AD_POLICY_PROBE_ATTEMPT_REASON,
                )
            )
        )
        .scalars()
        .all()
    )
    sent_events = list(
        (
            await test_db.execute(
                select(GroupAdPolicyEvent).where(
                    GroupAdPolicyEvent.group_id == target.group.id,
                    GroupAdPolicyEvent.reason == "unknown_group_probe_sent",
                )
            )
        )
        .scalars()
        .all()
    )

    assert pending_log.status == DeliveryStatus.PENDING.value
    assert pending_log.telegram_message_id is None
    assert pending_log.sent_at is None
    assert pending_log.reservation_token
    assert pending_log.reservation_token in str(exc_info.value)
    assert target.profile.ad_policy_mode == GroupAdPolicyMode.UNKNOWN_PROBE.value
    assert target.profile.ad_policy_probe_status == "sending"
    assert schedule.status == AdScheduleStatus.SENDING.value
    assert schedule.lock_token
    assert schedule.lease_expires_at is not None
    assert schedule.last_success_at is None
    assert len(attempt_events) == 1
    assert sent_events == []
    service._send_ad_text.assert_awaited_once()
    pool.release.assert_awaited_once_with(wrapper)

    clock["now"] += timedelta(minutes=31)
    with pytest.raises(RuntimeError, match="^group_delivery_inflight$"):
        await service.send_group_ad_policy_probe(
            target.group.id,
            account_id=target.account.id,
        )
    service._send_ad_text.assert_awaited_once()
    assert pool.acquire_by_id.await_count == 1
