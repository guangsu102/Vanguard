import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api.automation import _build_ad_delivery_diagnostic, _build_dynamic_health_diagnostic, _enqueue_automation_task, _prepare_operation_config_update
from app.core.account.models import AccountOperationConfig, AccountOperationMode, AccountStatus, AccountType, TelegramAccount
from app.core.group.models import Group, GroupAccountMembership, GroupLevel
import app.modules.acquisition.automation as automation_module
from app.modules.acquisition.automation import AcquisitionAutomationService
from app.modules.acquisition.models import AccountAdBinding, AdCampaign


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
        daily_limit=0,
        run_limit=0,
        now=datetime.utcnow(),
    )

    assert result["primary_reason"] == "health_score_below_floor"
    assert result["negative_adjustments"][0]["reason"] == "ad_peer_flood"
    assert any(item["reason"] == "probe_budget_zero" for item in result["reasons"])


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
        daily_limit=10,
        run_limit=2,
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
        daily_limit=0,
        run_limit=0,
    )

    assert paused_result["probe_execution_allowed"] is True
    assert paused_result["ad_delivery_allowed"] is False
    assert paused_result["next_action"] == "send_probe_while_ads_paused"


@pytest.mark.asyncio
async def test_ad_delivery_diagnostic_reports_zero_dynamic_limit(test_db):
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
        daily_limit=0,
        run_limit=0,
    )

    assert result["primary_block_reason"] == "dynamic_daily_limit_zero"
    assert result["next_action"] == "recover_account_health"
    assert any(item["reason"] == "dynamic_run_limit_zero" for item in result["block_reasons"])


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
        SimpleNamespace(telegram_group_id=704, group=SimpleNamespace(id=705)),
        SimpleNamespace(telegram_group_id=706, group=SimpleNamespace(id=707)),
    ]
    creative = SimpleNamespace(id=708)

    monkeypatch.setattr(service, "_list_enabled_ad_bindings_for_account", AsyncMock(return_value=[binding]))
    monkeypatch.setattr(service, "_list_joined_groups_for_account", AsyncMock(return_value=memberships))
    monkeypatch.setattr(service, "_ad_dynamic_run_limit", AsyncMock(return_value=0))
    monkeypatch.setattr(service, "_choose_delivery_creative", AsyncMock(return_value=creative))
    monkeypatch.setattr(service, "_campaign_is_active", lambda _campaign: True)
    probe_check = AsyncMock(side_effect=["ad_probe_waiting", None])
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

    assert probe_check.await_count == 2
    assert result.processed == 2
    assert result.skipped == 2
    assert [item["reason"] for item in result.details] == [
        "ad_probe_waiting",
        "account_dynamic_health_paused",
    ]
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
            }
        ),
    )

    result = await AcquisitionAutomationService(test_db).run_group_ai_warmup(dry_run=True)

    assert result["details"] == [{"action": "skip", "reason": "group_ai_warmup_no_candidates"}]


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
