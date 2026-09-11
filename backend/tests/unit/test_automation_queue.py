import asyncio
import importlib
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

import app.modules.acquisition.automation as automation_module
from app.api.automation import (
    _build_ad_delivery_diagnostic,
    _build_dynamic_health_diagnostic,
    _enqueue_automation_task,
    _operation_mode_mismatch_reason,
    _prepare_operation_config_update,
)
from app.core.account.models import (
    AccountOperationConfig,
    AccountOperationMode,
    AccountStatus,
    AccountType,
    TelegramAccount,
)
from app.core.group.models import Group, GroupAccountMembership, GroupLevel
from app.modules.acquisition.automation import AcquisitionAutomationService
from app.modules.acquisition.models import AccountAdBinding, AdCampaign
from app.modules.owned_group.models import OwnedGroupAsset

automation_api = importlib.import_module("app.api.automation")


class DummyResult:
    id = "task-123"


class DummyTask:
    def __init__(self) -> None:
        self.called_with = None

    def apply_async(self, **kwargs):
        self.called_with = kwargs
        return DummyResult()


class FailingTask:
    def apply_async(self, **kwargs):
        raise RuntimeError("broker down")


def test_enqueue_automation_task_returns_queued_result():
    task = DummyTask()

    result = _enqueue_automation_task(task, "auto_join_groups_task", dry_run=True, max_accounts=3)

    assert task.called_with == {"kwargs": {"dry_run": True, "max_accounts": 3}, "queue": "automation"}
    assert result["queued"] is True
    assert result["status"] == "queued"
    assert result["task_id"] == "task-123"
    assert result["payload"] == {"dry_run": True, "max_accounts": 3}
    assert result["processed"] == 0


def test_enqueue_automation_task_reports_queue_unavailable():
    try:
        _enqueue_automation_task(FailingTask(), "auto_join_groups_task")
    except HTTPException as exc:
        assert exc.status_code == 503
        assert "Automation queue unavailable" in exc.detail
    else:
        raise AssertionError("expected HTTPException")


def test_dynamic_health_diagnostic_reports_health_floor():
    account = TelegramAccount(
        phone="+15550002000",
        identifier="+15550002000",
        session_name="health_floor",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )

    result = _build_dynamic_health_diagnostic(
        account=account,
        health={
            "health_score": 12.5,
            "risk_score": 30,
            "adjustments": [
                {"reason": "risk_score", "delta": -30},
                {"reason": "ad_peer_flood", "delta": -80},
            ],
        },
        join_metrics={
            "writable_rate": 0.8,
            "probe_success_rate_24h": 0.7,
            "ad_success_rate_24h": 0.4,
        },
        probe_budget={"probe_based_limit": 0, "probe_factor": 0},
        warmup_action_multiplier=1.0,
        health_gate_applies=True,
        health_gate_passed=False,
        now=datetime.utcnow(),
    )

    assert result["primary_reason"] == "health_score_below_floor"
    assert result["negative_adjustments"][0]["reason"] == "ad_peer_flood"
    assert result["health_gate_passed"] is False


@pytest.mark.asyncio
async def test_ad_delivery_diagnostic_reports_pending_probe(test_db):
    account = TelegramAccount(
        phone="+15550002001",
        identifier="+15550002001",
        session_name="diag_pending_probe",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    config = AccountOperationConfig(account=account, enabled=True, auto_join_enabled=True, auto_ads_enabled=True)
    campaign = AdCampaign(name="Diagnostic Campaign", enabled=True, status="active")
    group = Group(group_id=920001, title="Diagnostic Group", level=GroupLevel.A, status="active")
    test_db.add_all([account, config, campaign, group])
    await test_db.flush()
    test_db.add_all(
        [
            AccountAdBinding(account_id=account.id, ad_campaign_id=campaign.id, enabled=True),
            GroupAccountMembership(
                group_id=group.id,
                telegram_group_id=group.group_id,
                account_id=account.id,
                status="joined",
                join_method="manual",
                warmup_status="joined_pending_test",
                probe_status="not_started",
                ad_status="warming",
                interaction_started_at=group.created_at,
                interaction_sent_today=1,
                note='{"event":"group_ai_warmup_interaction"}',
            ),
        ]
    )
    await test_db.commit()

    result = await _build_ad_delivery_diagnostic(
        test_db,
        account=account,
        op_config=config,
        campaign=campaign,
        now=group.created_at,
        growth_health_allowed=True,
    )

    assert result["primary_block_reason"] == "groups_pending_probe"
    assert result["next_action"] == "send_probe"
    assert result["group_diagnostics"]["pending_probe"] == 1
    assert result["group_diagnostics"]["ai_warmed"] == 1
    assert result["blocked_group_samples"][0]["reason"] == "probe_pending"
    assert result["blocked_group_samples"][0]["label"] == "已 AI 暖群，等待探针"

    paused_result = await _build_ad_delivery_diagnostic(
        test_db,
        account=account,
        op_config=config,
        campaign=campaign,
        now=group.created_at,
        growth_health_allowed=False,
    )

    assert paused_result["probe_execution_allowed"] is True
    assert paused_result["ad_delivery_allowed"] is False
    assert paused_result["next_action"] == "send_probe_while_ads_paused"


@pytest.mark.asyncio
async def test_ad_delivery_diagnostic_reports_growth_health_gate(test_db):
    account = TelegramAccount(
        phone="+15550002002",
        identifier="+15550002002",
        session_name="diag_zero_limit",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    config = AccountOperationConfig(account=account, enabled=True, auto_join_enabled=True, auto_ads_enabled=True)
    campaign = AdCampaign(name="Diagnostic Zero Campaign", enabled=True, status="active")
    test_db.add_all([account, config, campaign])
    await test_db.flush()
    test_db.add(AccountAdBinding(account_id=account.id, ad_campaign_id=campaign.id, enabled=True))
    await test_db.commit()

    result = await _build_ad_delivery_diagnostic(
        test_db,
        account=account,
        op_config=config,
        campaign=campaign,
        now=campaign.created_at,
        growth_health_allowed=False,
    )

    assert result["primary_block_reason"] == "growth_health_gate_blocked"
    assert result["next_action"] == "recover_account_health"
    assert [item["reason"] for item in result["block_reasons"]].count(
        "growth_health_gate_blocked"
    ) == 1


@pytest.mark.asyncio
async def test_group_ai_warmup_marks_ad_interaction_start(test_db):
    account = TelegramAccount(
        phone="+15550002003",
        identifier="+15550002003",
        session_name="ai_warmup_interaction",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    group = Group(group_id=920003, title="AI Warmup Group", level=GroupLevel.A, status="active")
    test_db.add_all([account, group])
    await test_db.flush()
    membership = GroupAccountMembership(
        group_id=group.id,
        telegram_group_id=group.group_id,
        account_id=account.id,
        status="joined",
        join_method="auto",
        warmup_status="joined_pending_test",
        probe_status="not_started",
        ad_status="warming",
    )
    test_db.add(membership)
    await test_db.commit()

    service = AcquisitionAutomationService(test_db)
    await service._record_group_ai_warmup_interaction(membership, message_id=88001, now=group.created_at)

    assert membership.interaction_started_at == group.created_at
    assert membership.interaction_sent_today == 1
    assert membership.last_checked_at == group.created_at
    assert "group_ai_warmup_interaction" in (membership.note or "")
    assert membership.probe_status == "not_started"


@pytest.mark.asyncio
async def test_zero_ad_health_limit_still_runs_probe_checks_but_blocks_ad_send(test_db, monkeypatch):
    service = AcquisitionAutomationService(test_db)
    campaign = SimpleNamespace(id=701)
    binding = SimpleNamespace(id=702, account_id=703, campaign=campaign)
    memberships = [
        SimpleNamespace(telegram_group_id=704, probe_status="scheduled", group=SimpleNamespace(id=705)),
        SimpleNamespace(telegram_group_id=706, probe_status="success", group=SimpleNamespace(id=707)),
    ]
    creative = SimpleNamespace(id=708)

    monkeypatch.setattr(service, "_list_enabled_ad_bindings_for_account", AsyncMock(return_value=[binding]))
    monkeypatch.setattr(service, "_list_joined_groups_for_account", AsyncMock(return_value=memberships))
    monkeypatch.setattr(service, "_growth_ad_health_allowed", AsyncMock(return_value=False))
    monkeypatch.setattr(service, "_choose_delivery_creative", AsyncMock(return_value=creative))
    monkeypatch.setattr(service, "_campaign_is_active", lambda _campaign: True)
    probe_check = AsyncMock(return_value="ad_probe_waiting")
    monkeypatch.setattr(service, "_ad_skip_reason", probe_check)
    send_ad = AsyncMock()
    monkeypatch.setattr(service, "_send_ad", send_ad)

    result = await service._run_ad_delivery_for_account(
        binding.account_id,
        binding_ids=[binding.id],
        dry_run=False,
        delivery_budget={"remaining": 1},
        delivery_budget_lock=asyncio.Lock(),
        reserved_ad_targets=set(),
        ad_target_lock=asyncio.Lock(),
        max_deliveries_per_account=1,
        stop_after_success=False,
        stop_after_failure=False,
    )

    assert probe_check.await_count == 1
    assert result.processed == 1
    assert result.skipped == 2
    assert [item["reason"] for item in result.details] == [
        "account_dynamic_health_paused",
        "ad_probe_waiting",
    ]
    service._choose_delivery_creative.assert_not_awaited()
    send_ad.assert_not_awaited()


@pytest.mark.asyncio
async def test_ad_dispatcher_excludes_owned_group_before_creative_or_delivery_state(
    test_db, monkeypatch
):
    account = TelegramAccount(
        identifier="owned-group-ad-exclusion",
        session_name="owned-group-ad-exclusion",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    group = Group(
        group_id=920011,
        title="Owned ad exclusion",
        level=GroupLevel.A,
        status="active",
    )
    test_db.add_all([account, group])
    await test_db.flush()
    test_db.add(
        OwnedGroupAsset(
            internal_name="owned-ad-exclusion",
            telegram_chat_id=group.group_id,
            title=group.title,
            owner_account_id=account.id,
            core_group_id=group.id,
            status="active",
        )
    )
    await test_db.commit()

    campaign = SimpleNamespace(id=920012, delivery_policy="growth")
    binding = SimpleNamespace(id=920013, account_id=account.id, campaign=campaign)
    membership = SimpleNamespace(
        group_id=group.id,
        telegram_group_id=group.group_id,
        group=group,
    )
    service = AcquisitionAutomationService(test_db)
    monkeypatch.setattr(
        service, "_list_enabled_ad_bindings_for_account", AsyncMock(return_value=[binding])
    )
    monkeypatch.setattr(
        service, "_list_joined_groups_for_account", AsyncMock(return_value=[membership])
    )
    monkeypatch.setattr(service, "_growth_ad_health_allowed", AsyncMock(return_value=True))
    monkeypatch.setattr(service, "_campaign_is_active", lambda _campaign: True)
    choose_creative = AsyncMock()
    claim_schedule = AsyncMock()
    record_delivery = AsyncMock()
    send_ad = AsyncMock()
    monkeypatch.setattr(service, "_choose_delivery_creative", choose_creative)
    monkeypatch.setattr(service, "_claim_ad_schedule_state", claim_schedule)
    monkeypatch.setattr(service, "_record_ad_delivery", record_delivery)
    monkeypatch.setattr(service, "_send_ad", send_ad)
    monkeypatch.setattr(
        automation_module,
        "get_ad_delivery_execution_settings",
        AsyncMock(return_value={"job_lease_seconds": 300}),
    )

    result = await service._run_ad_delivery_for_account(
        account.id,
        binding_ids=[binding.id],
        dry_run=False,
        delivery_budget={"remaining": 1},
        delivery_budget_lock=asyncio.Lock(),
        reserved_ad_targets=set(),
        ad_target_lock=asyncio.Lock(),
        max_deliveries_per_account=1,
        stop_after_success=False,
        stop_after_failure=False,
    )

    assert result.processed == 1
    assert result.skipped == 1
    assert result.details[0]["reason"] == "OWNED_GROUP_AD_DOMAIN_EXCLUDED"
    choose_creative.assert_not_awaited()
    claim_schedule.assert_not_awaited()
    record_delivery.assert_not_awaited()
    send_ad.assert_not_awaited()


@pytest.mark.asyncio
async def test_ad_dispatcher_final_owned_group_recheck_runs_inside_chat_lock(
    test_db, monkeypatch
):
    group = SimpleNamespace(id=920021, group_id=920022)
    membership = SimpleNamespace(
        group_id=group.id,
        telegram_group_id=group.group_id,
        group=group,
    )
    campaign = SimpleNamespace(id=920023, delivery_policy="growth")
    binding = SimpleNamespace(id=920024, account_id=920025, campaign=campaign)
    creative = SimpleNamespace(id=920026)
    service = AcquisitionAutomationService(test_db)
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

    async def owned_after_early_check(core_group_id, telegram_chat_id):
        assert lock_active is True
        assert (core_group_id, telegram_chat_id) == (group.id, group.group_id)
        return True

    monkeypatch.setattr(
        service, "_list_enabled_ad_bindings_for_account", AsyncMock(return_value=[binding])
    )
    monkeypatch.setattr(
        service, "_list_joined_groups_for_account", AsyncMock(return_value=[membership])
    )
    monkeypatch.setattr(service, "_growth_ad_health_allowed", AsyncMock(return_value=True))
    monkeypatch.setattr(service, "_campaign_is_active", lambda _campaign: True)
    monkeypatch.setattr(service, "_ad_skip_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(
        service, "_choose_delivery_creative", AsyncMock(return_value=creative)
    )
    monkeypatch.setattr(service, "_reserve_ad_delivery_target", AsyncMock(return_value=True))
    monkeypatch.setattr(
        service, "_is_owned_group_ad_domain_excluded", owned_after_early_check
    )
    monkeypatch.setattr(automation_module, "telegram_chat_advisory_lock", tracked_chat_lock)
    claim_schedule = AsyncMock()
    record_delivery = AsyncMock()
    send_ad = AsyncMock()
    monkeypatch.setattr(service, "_claim_ad_schedule_state", claim_schedule)
    monkeypatch.setattr(service, "_record_ad_delivery", record_delivery)
    monkeypatch.setattr(service, "_send_ad", send_ad)
    monkeypatch.setattr(
        automation_module,
        "get_ad_delivery_execution_settings",
        AsyncMock(return_value={"job_lease_seconds": 300}),
    )

    result = await service._run_ad_delivery_for_account(
        binding.account_id,
        binding_ids=[binding.id],
        dry_run=False,
        delivery_budget={"remaining": 1},
        delivery_budget_lock=asyncio.Lock(),
        reserved_ad_targets=set(),
        ad_target_lock=asyncio.Lock(),
        max_deliveries_per_account=1,
        stop_after_success=False,
        stop_after_failure=False,
    )

    assert lock_active is False
    assert result.skipped == 1
    assert result.details[0]["reason"] == "OWNED_GROUP_AD_DOMAIN_EXCLUDED"
    claim_schedule.assert_not_awaited()
    record_delivery.assert_not_awaited()
    send_ad.assert_not_awaited()


def test_ad_only_mode_disables_growth_controls():
    config = AccountOperationConfig(join_interval_min_seconds=60, join_interval_max_seconds=900)
    payload = _prepare_operation_config_update(
        config,
        {
            "operation_mode": AccountOperationMode.AD_ONLY.value,
            "auto_join_enabled": True,
            "keyword_auto_replenish_enabled": True,
        },
    )

    assert payload["operation_mode"] == AccountOperationMode.AD_ONLY.value
    assert payload["auto_join_enabled"] is False
    assert payload["keyword_auto_replenish_enabled"] is False


def test_batch_operation_config_only_accepts_same_mode_accounts():
    growth_config = AccountOperationConfig(
        operation_mode=AccountOperationMode.GROWTH.value
    )
    ad_only_config = AccountOperationConfig(
        operation_mode=AccountOperationMode.AD_ONLY.value
    )

    assert (
        _operation_mode_mismatch_reason(
            ad_only_config, AccountOperationMode.AD_ONLY.value
        )
        is None
    )
    assert (
        _operation_mode_mismatch_reason(
            growth_config, AccountOperationMode.AD_ONLY.value
        )
        == "operation_mode_mismatch: expected ad_only, found growth"
    )
    assert _operation_mode_mismatch_reason(growth_config, None) is None


@pytest.mark.asyncio
async def test_ad_only_account_is_excluded_from_group_ai_warmup(test_db, monkeypatch):
    account = TelegramAccount(
        identifier="ad-only-warmup",
        session_name="ad-only-warmup",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    group = Group(group_id=920010, title="Ad-only group", level=GroupLevel.A, status="active")
    config = AccountOperationConfig(
        account=account,
        enabled=True,
        auto_join_enabled=False,
        auto_ads_enabled=True,
        operation_mode=AccountOperationMode.AD_ONLY.value,
    )
    membership = GroupAccountMembership(
        group=group,
        account=account,
        telegram_group_id=group.group_id,
        status="joined",
    )
    test_db.add_all([account, group, config, membership])
    await test_db.commit()

    monkeypatch.setattr(
        automation_module,
        "get_group_ai_interaction_settings",
        AsyncMock(
            return_value={
                "enabled": True,
                "allowProactiveWarmup": True,
                "proactiveWarmupMaxPerGroupPerDay": 1,
                "proactiveWarmupMaxPerAccountPerDay": 1,
                "proactiveWarmupWindowStartHour": 12,
                "proactiveWarmupWindowEndHour": 12,
            }
        ),
    )

    result = await AcquisitionAutomationService(test_db).run_group_ai_warmup(dry_run=True)

    assert result["details"] == [{"action": "skip", "reason": "group_ai_warmup_no_candidates"}]


@pytest.mark.asyncio
async def test_owned_group_is_excluded_from_legacy_group_ai_warmup(test_db, monkeypatch):
    account = TelegramAccount(
        identifier="owned-group-warmup",
        session_name="owned-group-warmup",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    group = Group(
        group_id=920014,
        title="Owned warmup exclusion",
        level=GroupLevel.A,
        status="active",
    )
    config = AccountOperationConfig(
        account=account,
        enabled=True,
        auto_join_enabled=False,
        auto_ads_enabled=True,
        operation_mode=AccountOperationMode.GROWTH.value,
    )
    membership = GroupAccountMembership(
        group=group,
        account=account,
        telegram_group_id=group.group_id,
        status="joined",
    )
    test_db.add_all([account, group, config, membership])
    await test_db.flush()
    test_db.add(
        OwnedGroupAsset(
            internal_name="owned-warmup-exclusion",
            telegram_chat_id=group.group_id,
            title=group.title,
            owner_account_id=account.id,
            core_group_id=group.id,
            status="active",
        )
    )
    await test_db.commit()

    monkeypatch.setattr(
        automation_module,
        "get_group_ai_interaction_settings",
        AsyncMock(
            return_value={
                "enabled": True,
                "allowProactiveWarmup": True,
                "proactiveWarmupMaxPerGroupPerDay": 1,
                "proactiveWarmupMaxPerAccountPerDay": 1,
                "proactiveWarmupWindowStartHour": 12,
                "proactiveWarmupWindowEndHour": 12,
            }
        ),
    )

    result = await AcquisitionAutomationService(test_db).run_group_ai_warmup(dry_run=True)

    assert result["details"] == [
        {"action": "skip", "reason": "group_ai_warmup_no_candidates"}
    ]


@pytest.mark.asyncio
async def test_group_ai_warmup_final_owned_recheck_blocks_all_send_side_effects(
    test_db, monkeypatch
):
    account = TelegramAccount(
        identifier="owned-group-warmup-final",
        session_name="owned-group-warmup-final",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    group = Group(
        group_id=920015,
        title="Owned warmup final exclusion",
        level=GroupLevel.A,
        status="active",
    )
    config = AccountOperationConfig(
        account=account,
        enabled=True,
        auto_join_enabled=False,
        auto_ads_enabled=True,
        operation_mode=AccountOperationMode.GROWTH.value,
    )
    membership = GroupAccountMembership(
        group=group,
        account=account,
        telegram_group_id=group.group_id,
        status="joined",
    )
    test_db.add_all([account, group, config, membership])
    await test_db.commit()

    monkeypatch.setattr(
        automation_module,
        "get_group_ai_interaction_settings",
        AsyncMock(
            return_value={
                "enabled": True,
                "allowProactiveWarmup": True,
                "proactiveWarmupMaxPerGroupPerDay": 1,
                "proactiveWarmupMaxPerAccountPerDay": 1,
                "proactiveWarmupWindowStartHour": 0,
                "proactiveWarmupWindowEndHour": 0,
            }
        ),
    )
    lock_active = False
    checks: list[tuple[int, int, bool]] = []

    @asynccontextmanager
    async def tracked_chat_lock(_db, chat_id):
        nonlocal lock_active
        assert chat_id == group.group_id
        lock_active = True
        try:
            yield
        finally:
            lock_active = False

    async def owned_after_candidate_selection(core_group_id, telegram_chat_id):
        checks.append((core_group_id, telegram_chat_id, lock_active))
        return len(checks) > 1

    pool = SimpleNamespace(acquire_by_id=AsyncMock(), release=AsyncMock())
    fake_speaker = SimpleNamespace(speak_in_group=AsyncMock())
    template_engine = SimpleNamespace(load_templates=AsyncMock())
    monkeypatch.setattr(automation_module, "telegram_chat_advisory_lock", tracked_chat_lock)
    monkeypatch.setattr(automation_module, "Speaker", MagicMock(return_value=fake_speaker))
    monkeypatch.setattr(
        automation_module,
        "TemplateEngine",
        MagicMock(return_value=template_engine),
    )
    service = AcquisitionAutomationService(test_db, account_pool=pool)
    monkeypatch.setattr(
        service,
        "_is_owned_group_ad_domain_excluded",
        AsyncMock(side_effect=owned_after_candidate_selection),
    )
    record_interaction = AsyncMock()
    monkeypatch.setattr(
        service,
        "_record_group_ai_warmup_interaction",
        record_interaction,
    )

    result = await service.run_group_ai_warmup(dry_run=False)

    assert result["processed"] == 1
    assert result["skipped"] == 1
    assert result["details"] == [
        {
            "action": "skip",
            "reason": "OWNED_GROUP_AD_DOMAIN_EXCLUDED",
            "account_id": account.id,
            "group_id": group.group_id,
        }
    ]
    assert checks == [
        (group.id, group.group_id, False),
        (group.id, group.group_id, True),
    ]
    assert lock_active is False
    pool.acquire_by_id.assert_not_awaited()
    fake_speaker.speak_in_group.assert_not_awaited()
    record_interaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_ad_interaction_final_owned_recheck_blocks_all_send_side_effects(
    test_db, monkeypatch
):
    group = SimpleNamespace(id=920016, group_id=920017)
    membership = SimpleNamespace(
        group_id=group.id,
        telegram_group_id=group.group_id,
        group=group,
        account_id=920018,
        note="unchanged",
        interaction_sent_today=0,
    )
    membership_before = vars(membership).copy()
    pool = SimpleNamespace(acquire_by_id=AsyncMock(), release=AsyncMock())
    service = AcquisitionAutomationService(test_db, account_pool=pool)
    service.telegram_execution.send_group_message = AsyncMock()
    locked_path = AsyncMock()
    monkeypatch.setattr(service, "_maybe_send_ad_interaction_locked", locked_path)
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

    monkeypatch.setattr(automation_module, "telegram_chat_advisory_lock", tracked_chat_lock)
    monkeypatch.setattr(
        service,
        "_is_owned_group_ad_domain_excluded",
        AsyncMock(side_effect=owned_inside_lock),
    )

    result = await service._maybe_send_ad_interaction(
        membership.account_id,
        membership,
        datetime.utcnow(),
        {},
        phase="warmup",
        dry_run=False,
    )

    assert result == "OWNED_GROUP_AD_DOMAIN_EXCLUDED"
    assert lock_active is False
    assert vars(membership) == membership_before
    locked_path.assert_not_awaited()
    pool.acquire_by_id.assert_not_awaited()
    service.telegram_execution.send_group_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_ad_policy_probe_owned_group_maps_to_http_409(test_db, monkeypatch):
    service = SimpleNamespace(
        send_group_ad_policy_probe=AsyncMock(
            side_effect=RuntimeError("OWNED_GROUP_AD_DOMAIN_EXCLUDED")
        )
    )
    monkeypatch.setattr(
        automation_api,
        "AcquisitionAutomationService",
        MagicMock(return_value=service),
    )

    with pytest.raises(HTTPException) as exc_info:
        await automation_api.trigger_group_ad_policy_probe(
            920019,
            SimpleNamespace(account_id=920020),
            current_user={"id": 1},
            db=test_db,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "OWNED_GROUP_AD_DOMAIN_EXCLUDED"
    service.send_group_ad_policy_probe.assert_awaited_once_with(
        920019,
        account_id=920020,
        changed_by_user_id=1,
    )


@pytest.mark.asyncio
async def test_ad_only_account_skips_ad_probe_and_warmup(test_db):
    account = TelegramAccount(
        identifier="ad-only-probe",
        session_name="ad-only-probe",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    config = AccountOperationConfig(
        account=account,
        enabled=True,
        auto_ads_enabled=True,
        operation_mode=AccountOperationMode.AD_ONLY.value,
    )
    test_db.add_all([account, config])
    await test_db.commit()
    membership = SimpleNamespace(
        warmup_status="joined_pending_test",
        probe_status="not_started",
        note="",
    )

    reason = await AcquisitionAutomationService(test_db)._ad_warmup_skip_reason(
        account.id,
        membership,
        datetime.utcnow(),
        dry_run=False,
    )

    assert reason is None
