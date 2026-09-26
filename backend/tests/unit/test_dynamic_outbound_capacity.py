from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.core.account.models import (
    AccountOperationConfig,
    AccountOutboundAttempt,
    AccountStatus,
    AccountType,
    TelegramAccount,
)
from app.core.account.outbound_budget import (
    AccountOutboundBudgetService,
    OutboundBudgetBlocked,
    account_capacity_limits,
)
from app.core.account.risk_guard import AccountRiskAction, AccountRiskGuard
from app.core.automation_settings import save_auto_join_scheduler_settings
from app.core.group.models import Group, GroupAccountMembership
from app.modules.acquisition.capacity import capacity_snapshot, inventory_snapshot
from app.modules.acquisition.dynamic_frequency import AccountDynamicFrequencyService
from app.modules.acquisition.join_budget import JoinRequestBudgetService
from app.modules.acquisition.models import AutoJoinAttempt, DeliveryStatus, GroupQualificationAudit

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 9, 23, 10)

@pytest.fixture(autouse=True)
def offline_redis(monkeypatch):
    from app.core.account import rpc_governor
    monkeypatch.setattr(rpc_governor, "read_budget_state", AsyncMock(return_value={"usage": {}, "blocked_windows": [], "retry_after_seconds": 0, "emergency_cooldown_seconds": 0}))
    from app.core.account import rpc_governor
    monkeypatch.setattr(rpc_governor, "read_budget_state", AsyncMock(return_value={
        "usage": {}, "blocked_windows": [], "retry_after_seconds": 0, "emergency_cooldown_seconds": 0,
    }))
    from app.modules.acquisition.automation import AcquisitionAutomationService
    monkeypatch.setattr(AcquisitionAutomationService, "_get_ad_delivery_cooldown_until", AsyncMock(return_value=None))


async def account_config(db, **overrides):
    account = TelegramAccount(
        identifier="capacity-account",
        session_name="capacity-account",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        risk_level="normal",
        is_active=True,
        registered_at=NOW - timedelta(days=365),
    )
    db.add(account)
    await db.flush()
    params = {
        "account_id": account.id,
        "enabled": True,
        "auto_ads_enabled": True,
        "auto_join_enabled": True,
        "dynamic_capacity_enabled": True,
        "max_messages_per_day": 62,
        "max_ads_per_day": 30,
        "max_verification_messages_per_day": 30,
        "max_diagnostic_messages_per_day": 2,
        "message_interval_seconds": 600,
        "max_groups_per_day": 30,
        "max_groups_total": 300,
    }
    params.update(overrides)
    config = AccountOperationConfig(**params)
    db.add(config)
    await db.commit()
    return account, config


def attempt(account_id, key, category="ad", state="succeeded", at=NOW, target=None):
    return AccountOutboundAttempt(
        account_id=account_id,
        attempt_key=key,
        category=category,
        target_key=target or key,
        state=state,
        created_at=at,
        attempted_at=at,
        completed_at=at if state in {"succeeded", "failed"} else None,
        context_json="{}",
    )


async def test_postgresql_account_and_config_reservation_lock_order(test_db):
    db = MagicMock(flush=AsyncMock(), scalar=AsyncMock(side_effect=[None, None]))
    await AccountOutboundBudgetService(db)._account(1, lock=True)
    queries = [
        str(call.args[0].compile(dialect=postgresql.dialect()))
        for call in db.scalar.await_args_list
    ]
    assert queries[0].rstrip().endswith("FOR UPDATE OF telegram_account")
    assert queries[1].rstrip().endswith("FOR UPDATE OF telegram_account_operation_config")


async def test_independent_category_limits_and_shared_total(test_db):
    account, config = await account_config(test_db)
    for category, number in [("ad", 30), ("verification", 29), ("diagnostic", 2)]:
        for i in range(number):
            test_db.add(
                attempt(
                    account.id, f"{category}-{i}", category=category, at=NOW - timedelta(hours=2)
                )
            )
    await test_db.commit()
    service = AccountOutboundBudgetService(test_db)
    info = await service.snapshot(account.id, NOW)
    assert info["remaining"] == 1
    assert info["categories"]["ad"]["remaining"] == 0
    assert info["categories"]["verification"]["remaining"] == 1
    assert info["categories"]["diagnostic"]["remaining"] == 0
    with pytest.raises(OutboundBudgetBlocked, match="outbound_ad_budget"):
        await service.reserve(
            account.id, attempt_key="ad-extra", category="ad", target_key="g", now=NOW
        )
    await service.reserve(
        account.id,
        attempt_key="verification-last",
        category="verification",
        target_key="g",
        now=NOW,
    )
    assert (await service.snapshot(account.id, NOW))["remaining"] == 0
    with pytest.raises(OutboundBudgetBlocked, match="outbound_total_budget"):
        await service.reserve(
            account.id, attempt_key="extra", category="verification", target_key="h", now=NOW
        )


async def test_beijing_day_and_rolling_24h_both_apply(test_db):
    account, _ = await account_config(test_db)
    now = datetime(2026, 9, 23, 16, 1)  # Beijing next day 00:01.
    for i in range(30):
        test_db.add(attempt(account.id, f"yesterday-{i}", at=now - timedelta(minutes=90)))
    await test_db.commit()
    info = await AccountOutboundBudgetService(test_db).snapshot(account.id, now)
    assert info["categories"]["ad"]["used_today"] == 0
    assert info["categories"]["ad"]["used_rolling_24h"] == 30
    assert info["categories"]["ad"]["remaining"] == 0


async def test_unknown_never_expires_and_only_same_target_is_paused(test_db):
    account, _ = await account_config(test_db)
    test_db.add(
        attempt(account.id, "unknown", state="unknown", at=NOW - timedelta(days=7), target="g")
    )
    await test_db.commit()
    service = AccountOutboundBudgetService(test_db)
    info = await service.snapshot(account.id, NOW)
    assert info["used_today"] == info["used_rolling_24h"] == info["unknown_count"] == 1
    with pytest.raises(OutboundBudgetBlocked, match="outbound_target_reconciliation_required"):
        await service.reserve(
            account.id, attempt_key="retry", category="ad", target_key="g", now=NOW
        )
    await service.reserve(account.id, attempt_key="other", category="ad", target_key="h", now=NOW)
    with pytest.raises(OutboundBudgetBlocked, match="outbound_reconciliation_required"):
        await service.finish("unknown", account_id=account.id, state="failed", now=NOW)
    with pytest.raises(OutboundBudgetBlocked, match="outbound_reconciliation_unconfirmed"):
        await service.reconcile("unknown", account_id=account.id, state="failed", now=NOW)
    await service.reconcile(
        "unknown", account_id=account.id, state="failed", confirmed=True, now=NOW
    )
    assert (await service.snapshot(account.id, NOW))["unknown_count"] == 0


async def test_pre_rpc_cancel_refunds_but_attempted_failure_counts(test_db):
    account, _ = await account_config(test_db)
    service = AccountOutboundBudgetService(test_db)
    await service.reserve(
        account.id, attempt_key="cancel", category="verification", target_key="g", now=NOW
    )
    await service.finish("cancel", account_id=account.id, state="cancelled", now=NOW)
    assert (await service.snapshot(account.id, NOW))["remaining"] == 62
    await service.reserve(
        account.id, attempt_key="sent", category="verification", target_key="g", now=NOW
    )
    await service.mark_attempted("sent", account_id=account.id, now=NOW)
    with pytest.raises(OutboundBudgetBlocked, match="outbound_attempt_already_sent"):
        await service.finish("sent", account_id=account.id, state="cancelled", now=NOW)
    await service.finish("sent", account_id=account.id, state="failed", now=NOW)
    assert (await service.snapshot(account.id, NOW))["remaining"] == 61


async def test_idempotency_key_and_rpc_boundary_are_single_use(test_db):
    account, _ = await account_config(test_db)
    service = AccountOutboundBudgetService(test_db)
    a = await service.reserve(
        account.id,
        attempt_key="key",
        category="verification",
        target_key="g",
        context={"x": 1},
        now=NOW,
    )
    b = await service.reserve(
        account.id,
        attempt_key="key",
        category="verification",
        target_key="g",
        context={"x": 1},
        now=NOW,
    )
    assert a.id == b.id
    with pytest.raises(OutboundBudgetBlocked, match="outbound_idempotency_conflict"):
        await service.reserve(
            account.id,
            attempt_key="key",
            category="verification",
            target_key="g",
            context={"x": 2},
            now=NOW,
        )
    await service.mark_attempted("key", account_id=account.id, now=NOW)
    with pytest.raises(OutboundBudgetBlocked, match="outbound_attempt_not_reserved"):
        await service.mark_attempted("key", account_id=account.id, now=NOW)
    with pytest.raises(OutboundBudgetBlocked, match="outbound_message_id_required"):
        await service.finish("key", account_id=account.id, state="succeeded", now=NOW)
    await service.finish("key", account_id=account.id, state="succeeded", message_id=17, now=NOW)
    assert (
        await service.reserve(
            account.id,
            attempt_key="key",
            category="verification",
            target_key="g",
            context={"x": 1},
            now=NOW,
        )
    ).state == "succeeded"


async def test_expired_reservation_and_changed_account_cannot_reach_rpc(test_db):
    account, _ = await account_config(test_db)
    service = AccountOutboundBudgetService(test_db)
    await service.reserve(account.id, attempt_key="expired", category="ad", target_key="g", now=NOW)
    with pytest.raises(OutboundBudgetBlocked, match="outbound_reservation_expired"):
        await service.mark_attempted(
            "expired", account_id=account.id, now=NOW + timedelta(minutes=6)
        )
    assert (await service.snapshot(account.id, NOW + timedelta(minutes=6)))["remaining"] == 62
    await service.reserve(
        account.id,
        attempt_key="live",
        category="ad",
        target_key="h",
        now=NOW + timedelta(minutes=6),
    )
    account.status = AccountStatus.RESTRICTED
    await test_db.commit()
    with pytest.raises(OutboundBudgetBlocked, match="account_capacity_unavailable"):
        await service.mark_attempted("live", account_id=account.id, now=NOW + timedelta(minutes=6))


@pytest.mark.parametrize(
    "risk,recovery,status,expected,interval",
    [
        ("normal", None, "online", 30, 600),
        ("watch", None, "online", 10, 1800),
        ("normal", NOW + timedelta(days=1), "online", 10, 1800),
        ("normal", None, "restricted", 0, 600),
        ("frozen", None, "online", 0, 600),
    ],
)
async def test_explicit_capacity_health_states(test_db, risk, recovery, status, expected, interval):
    account, config = await account_config(test_db)
    account.risk_level, account.risk_recovery_until, account.status = (
        risk,
        recovery,
        AccountStatus(status),
    )
    limits = account_capacity_limits(account, config, NOW)
    assert limits["join"] == limits["ad"] == expected
    assert limits["ad_interval_seconds"] == interval
    assert limits["join_interval_seconds"] == (7200 if interval == 1800 else 2880)


async def test_stricter_config_and_account_age_cannot_be_bypassed(test_db):
    account, config = await account_config(
        test_db, max_ads_per_day=3, max_messages_per_day=4, message_interval_seconds=4000
    )
    limits = account_capacity_limits(account, config, NOW)
    assert (limits["ad"], limits["total"], limits["ad_interval_seconds"]) == (3, 4, 4000)
    account.registered_at = NOW - timedelta(days=2)
    await test_db.commit()
    with pytest.raises(OutboundBudgetBlocked, match="account_age_not_verified_six_months"):
        await AccountOutboundBudgetService(test_db).reserve(
            account.id, attempt_key="young", category="ad", target_key="g", now=NOW
        )


async def test_ad_interval_serializes_parallel_reservations(test_db):
    account, _ = await account_config(test_db)
    service = AccountOutboundBudgetService(test_db)
    await service.reserve(account.id, attempt_key="first", category="ad", target_key="g", now=NOW)
    with pytest.raises(OutboundBudgetBlocked, match="outbound_ad_reconciliation_required"):
        await service.reserve(
            account.id, attempt_key="parallel", category="ad", target_key="h", now=NOW
        )
    await service.mark_attempted("first", account_id=account.id, now=NOW)
    await service.finish("first", account_id=account.id, state="succeeded", message_id=1, now=NOW)
    with pytest.raises(OutboundBudgetBlocked, match="outbound_ad_interval"):
        await service.reserve(
            account.id,
            attempt_key="early",
            category="ad",
            target_key="h",
            now=NOW + timedelta(seconds=599),
        )
    await service.reserve(
        account.id,
        attempt_key="due",
        category="ad",
        target_key="h",
        now=NOW + timedelta(seconds=600),
    )


async def add_member(
    db,
    account_id,
    index,
    state="completed",
    decision="observe",
    review="review_2h",
    next_retry=None,
    stale=False,
):
    group = Group(group_id=-1000000010000 - index, title=f"group {index}")
    db.add(group)
    await db.flush()
    member = GroupAccountMembership(
        group_id=group.id,
        telegram_group_id=group.group_id,
        account_id=account_id,
        status="joined",
        joined_at=NOW - timedelta(days=2),
        review_status=review,
    )
    db.add(member)
    await db.flush()
    audit = GroupQualificationAudit(
        batch_id=f"batch{index}",
        membership_id=member.id,
        account_id=account_id,
        group_id=group.id,
        policy_version="test",
        state=state,
        decision=decision,
        membership_joined_at=member.joined_at - (timedelta(days=1) if stale else timedelta()),
        next_retry_at=next_retry,
        expires_at=NOW + timedelta(hours=20),
        checked_at=NOW - timedelta(hours=1),
    )
    db.add(audit)
    return member, audit


async def test_inventory_excludes_manual_observe_historical_and_stale_audits(test_db):
    account, config = await account_config(test_db)
    await add_member(test_db, account.id, 1, state="queued", next_retry=NOW - timedelta(hours=3))
    await add_member(test_db, account.id, 2, state="waiting_ai", next_retry=NOW)
    await add_member(test_db, account.id, 3, state="manual_required", review="manual_required")
    await add_member(test_db, account.id, 4, decision="observe", next_retry=NOW)
    await add_member(test_db, account.id, 5, state="queued", stale=True)
    member, _ = await add_member(test_db, account.id, 6, state="queued")
    test_db.add(
        GroupQualificationAudit(
            batch_id="latest",
            membership_id=member.id,
            account_id=account.id,
            group_id=member.group_id,
            policy_version="test",
            state="completed",
            decision="allowed",
            membership_joined_at=member.joined_at,
            expires_at=NOW + timedelta(hours=2),
        )
    )
    await test_db.commit()
    info = await inventory_snapshot(test_db, account.id, NOW)
    assert info == {
        "total": 6,
        "qualified": 1,
        "active_backlog": 2,
        "overdue": 1,
        "manual": 1,
        "target_hours": 48,
        "review_due": 3,
    }
    await save_auto_join_scheduler_settings(test_db, {"enabled": True})
    status = await JoinRequestBudgetService(test_db).status(account.id, now=NOW)
    assert status.effective_limit == 30
    assert "join_review_overdue" not in status.blocked_reasons
    assert "join_review_backlog" not in status.blocked_reasons
    cap = await capacity_snapshot(test_db, account.id, NOW)
    assert cap["inventory"]["target"] == 60
    assert cap["remaining"]["join"] == 30


async def test_dynamic_backlog_high_and_low_watermarks(test_db):
    account, config = await account_config(test_db)
    await save_auto_join_scheduler_settings(test_db, {"enabled": True})
    for i in range(12):
        await add_member(test_db, account.id, i, state="queued")
    await test_db.commit()
    service = JoinRequestBudgetService(test_db)
    assert "join_review_backlog" in (await service.status(account.id, now=NOW)).blocked_reasons
    config.join_review_backlog_paused = True
    rows = list(
        (
            await test_db.scalars(
                select(GroupQualificationAudit).order_by(GroupQualificationAudit.id)
            )
        ).all()
    )
    for row in rows[:5]:
        row.state, row.decision = "completed", "observe"
    await test_db.commit()
    assert "join_review_backlog" in (await service.status(account.id, now=NOW)).blocked_reasons
    rows[5].state, rows[5].decision = "completed", "observe"
    await test_db.commit()
    assert "join_review_backlog" not in (await service.status(account.id, now=NOW)).blocked_reasons


async def test_dynamic_limits_bypass_legacy_multipliers_but_keep_health(test_db):
    account, config = await account_config(test_db)
    service = AccountDynamicFrequencyService(test_db)
    service.account_join_quality_metrics = AsyncMock(
        side_effect=AssertionError("legacy probe metrics must not gate dynamic capacity")
    )
    assert await service.auto_join_dynamic_daily_limit(config, NOW) == 30
    assert await service.sync_account_business_stage(config, NOW) == "normal"
    account.risk_level = "watch"
    await test_db.commit()
    assert await service.auto_join_dynamic_daily_limit(config, NOW) == 10
    assert await service.growth_ad_health_allowed(account.id, NOW)


@pytest.mark.parametrize("method", ["release", "finalize"])
async def test_join_finalization_can_share_caller_transaction(test_db, method):
    account, _ = await account_config(test_db)
    row = AutoJoinAttempt(
        account_id=account.id,
        status="pending",
        request_state="reserved",
        telegram_action_attempted=False,
        reason="initial",
    )
    test_db.add(row)
    await test_db.commit()
    row_id, account_id = row.id, account.id
    account.display_name = "pending-change"
    service = JoinRequestBudgetService(test_db)
    if method == "release":
        await service.release(row, reason="rollback-test", now=NOW, commit=False)
    else:
        row.telegram_action_attempted = True
        await service.finalize(
            row, status=DeliveryStatus.SUCCESS, reason="rollback-test", commit=False
        )
        assert row.telegram_action_attempted is True
    assert (await test_db.get(TelegramAccount, account_id)).display_name == "pending-change"
    await test_db.rollback()
    assert (await test_db.get(TelegramAccount, account_id)).display_name is None
    assert (await test_db.get(AutoJoinAttempt, row_id)).reason == "initial"


async def test_risk_guard_requires_real_reserved_ledger_and_current_ad_evidence(
    test_db, monkeypatch
):
    account, _ = await account_config(test_db)
    guard = AccountRiskGuard(test_db, cache=SimpleNamespace(client=None))
    assert not await guard._validated_dynamic_outbound(
        account.id,
        AccountRiskAction.AD_DELIVERY,
        7,
        {"source": "qualified", "outbound_attempt_key": "invented"},
    )
    now = datetime.utcnow()
    service = AccountOutboundBudgetService(test_db)
    context = {
        "qualification_audit_id": 4,
        "evidence_hash": "hash",
        "content_scope": "text_profile",
        "policy_version": "v",
        "telegram_group_id": 7,
        "membership_id": 6,
    }
    await service.reserve(
        account.id, attempt_key="real", category="ad", target_key="7", context=context, now=now
    )
    from app.modules.acquisition import qualification_service as qualification

    row = SimpleNamespace(
        id=4, evidence_hash="hash", policy_version="v", content_scope="text_profile"
    )
    monkeypatch.setattr(qualification, "policy", AsyncMock(return_value={"enabled": True}))
    monkeypatch.setattr(
        qualification,
        "current_authorization",
        AsyncMock(return_value=(row, SimpleNamespace(group_id=7), SimpleNamespace(id=6))),
    )
    gate = AsyncMock(return_value=None)
    monkeypatch.setattr(qualification, "send_gate", gate)
    details = {
        "outbound_attempt_key": "real",
        "content": "text without URL",
        "reservation_token": "exact",
    }
    assert await guard._validated_dynamic_outbound(
        account.id, AccountRiskAction.AD_DELIVERY, 7, details
    )
    assert gate.await_args.kwargs["reservation_token"] == "exact"
    row.evidence_hash = "new-hash"
    assert not await guard._validated_dynamic_outbound(
        account.id, AccountRiskAction.AD_DELIVERY, 7, details
    )
    row.evidence_hash = "hash"
    gate.return_value = "qualification_review_expired"
    assert not await guard._validated_dynamic_outbound(
        account.id, AccountRiskAction.AD_DELIVERY, 7, details
    )
    gate.return_value = None
    await service.mark_attempted("real", account_id=account.id, now=now)
    assert not await guard._validated_dynamic_outbound(
        account.id, AccountRiskAction.AD_DELIVERY, 7, details
    )


async def test_verification_ledger_does_not_double_charge_redis_or_legacy_text_dedup(test_db):
    account, _ = await account_config(test_db)
    now = datetime.utcnow()
    await AccountOutboundBudgetService(test_db).reserve(
        account.id, attempt_key="verify", category="verification", target_key="7", now=now
    )
    guard = AccountRiskGuard(test_db, cache=SimpleNamespace(client=None))
    guard._reserve_budget = AsyncMock(
        side_effect=AssertionError("durable category must not use old shared Redis counter")
    )
    result = await guard.check_and_reserve(
        SimpleNamespace(account_id=account.id),
        AccountRiskAction.VERIFICATION_ANSWER,
        target_type="group",
        target_id=7,
        details={
            "outbound_attempt_key": "verify",
            "content": "a sufficiently long verification phrase",
        },
    )
    assert result.allowed
    guard._reserve_budget.assert_not_awaited()


async def test_cancelled_preflight_can_renew_original_key_without_renewing_unknown(test_db):
    account, _ = await account_config(test_db)
    service = AccountOutboundBudgetService(test_db)
    first = await service.reserve(
        account.id, attempt_key="original", category="verification", target_key="7", now=NOW
    )
    await service.finish("original", account_id=account.id, state="cancelled", now=NOW)
    renewed = await service.reserve(
        account.id,
        attempt_key="original",
        category="verification",
        target_key="7",
        now=NOW + timedelta(minutes=8),
    )
    assert renewed.id == first.id and renewed.state == "reserved" and renewed.completed_at is None
    await service.mark_attempted("original", account_id=account.id, now=NOW + timedelta(minutes=8))
    await service.finish(
        "original", account_id=account.id, state="unknown", now=NOW + timedelta(minutes=8)
    )
    repeated = await service.reserve(
        account.id,
        attempt_key="original",
        category="verification",
        target_key="7",
        now=NOW + timedelta(days=3),
    )
    assert repeated.state == "unknown" and repeated.lease_expires_at is None


async def test_expired_pre_rpc_lease_can_renew_with_fresh_budget_check(test_db):
    account, config = await account_config(test_db)
    service = AccountOutboundBudgetService(test_db)
    first = await service.reserve(
        account.id, attempt_key="expired-renew", category="verification", target_key="7", now=NOW
    )
    config.max_verification_messages_per_day = 0
    await test_db.commit()
    with pytest.raises(OutboundBudgetBlocked, match="outbound_verification_budget"):
        await service.reserve(
            account.id,
            attempt_key="expired-renew",
            category="verification",
            target_key="7",
            now=NOW + timedelta(minutes=6),
        )
    config.max_verification_messages_per_day = 30
    await test_db.commit()
    renewed = await service.reserve(
        account.id,
        attempt_key="expired-renew",
        category="verification",
        target_key="7",
        now=NOW + timedelta(minutes=6),
    )
    assert first.id == renewed.id and renewed.lease_expires_at == NOW + timedelta(minutes=11)


async def test_restricted_official_diagnostic_has_two_slots_without_promotion(test_db):
    account, config = await account_config(test_db)
    account.status, account.risk_level = AccountStatus.RESTRICTED, "quarantined"
    config.enabled = False  # Restriction may have paused automation; explicit official diagnostics remain available.
    await test_db.commit()
    service = AccountOutboundBudgetService(test_db)
    info = await service.snapshot(account.id, NOW)
    assert info["limits"]["join"] == info["limits"]["ad"] == info["limits"]["verification"] == 0
    assert info["categories"]["diagnostic"]["remaining"] == 2
    with pytest.raises(OutboundBudgetBlocked, match="official_diagnostic_target_required"):
        await service.reserve(
            account.id,
            attempt_key="fake-diagnostic",
            category="diagnostic",
            target_key="8",
            now=NOW,
        )
    for index in range(2):
        key = f"diagnostic:spam-check:{index}"
        await service.reserve(
            account.id, attempt_key=key, category="diagnostic", target_key="178220800", now=NOW
        )
        await service.mark_attempted(key, account_id=account.id, now=NOW)
        await service.finish(
            key, account_id=account.id, state="succeeded", message_id=index + 1, now=NOW
        )
    with pytest.raises(OutboundBudgetBlocked, match="outbound_diagnostic_budget"):
        await service.reserve(
            account.id,
            attempt_key="third-diagnostic",
            category="diagnostic",
            target_key="178220800",
            now=NOW,
        )
    with pytest.raises(OutboundBudgetBlocked, match="account_capacity_unavailable"):
        await service.reserve(
            account.id, attempt_key="forbidden-ad", category="ad", target_key="7", now=NOW
        )
    assert account.status == AccountStatus.RESTRICTED


async def test_capacity_distinguishes_unused_quota_from_executable_material_and_rollout(
    test_db, monkeypatch
):
    from app.core.automation_settings import save_ad_delivery_execution_settings
    from app.modules.acquisition import qualification_service as qualification
    from app.modules.acquisition.automation import AcquisitionAutomationService
    from app.modules.acquisition.models import (
        AccountAdBinding,
        AdCampaign,
        AdCreative,
        AdDeliveryLog,
    )

    account, config = await account_config(test_db)
    member, _ = await add_member(test_db, account.id, 17, decision="allowed", review="approved")
    await test_db.commit()
    await save_auto_join_scheduler_settings(test_db, {"enabled": True})
    await save_ad_delivery_execution_settings(test_db, {"enabled": True})
    policy = {"enabled": True, "rollout_accounts": {str(account.id): {"phase": "dynamic"}}}
    monkeypatch.setattr(qualification, "policy", AsyncMock(return_value=policy))
    monkeypatch.setattr(qualification, "send_gate", AsyncMock(return_value=None))
    monkeypatch.setattr(
        AcquisitionAutomationService,
        "_qualified_ad_context",
        AsyncMock(
            return_value=(
                {"decision": "allowed", "rollout_phase": "dynamic", "account_id": account.id},
                None,
            )
        ),
    )
    missing = await capacity_snapshot(test_db, account.id, NOW)
    assert missing["account_id"] == account.id
    assert missing["quota_remaining"]["ad"] == 30 and missing["executable_now"]["ad"] == 0
    assert "qualification_text_profile_material_missing" in missing["blockers"]
    creative = AdCreative(
        name="profile", content="查看我的简介了解更多", creative_type="text", enabled=True
    )
    campaign = AdCampaign(
        name="capacity-growth", enabled=True, delivery_policy="growth", send_mode="interval"
    )
    test_db.add_all([creative, campaign])
    await test_db.flush()
    test_db.add(
        AccountAdBinding(
            account_id=account.id, ad_campaign_id=campaign.id, creative_id=creative.id, enabled=True
        )
    )
    await test_db.commit()
    ready = await capacity_snapshot(test_db, account.id, NOW)
    assert ready["executable_now"]["ad"] == 1
    from datetime import UTC
    due = NOW + timedelta(minutes=23)
    AcquisitionAutomationService._get_ad_delivery_cooldown_until.return_value = due.replace(tzinfo=UTC).timestamp()
    cooled = await capacity_snapshot(test_db, account.id, NOW)
    assert cooled["executable_now"]["ad"] == 0
    assert cooled["ad_next_allowed_at"] == due.isoformat()
    assert "account_ad_cooldown" in cooled["blockers"]
    AcquisitionAutomationService._get_ad_delivery_cooldown_until.return_value = None
    from app.modules.acquisition.models import GroupAdProfile
    hold = GroupAdProfile(group_id=member.group_id, telegram_group_id=member.telegram_group_id, ad_policy_source="manual", ad_policy_mode="forbidden")
    test_db.add(hold); await test_db.commit()
    held = await capacity_snapshot(test_db, account.id, NOW)
    assert held["executable_now"]["ad"] == 0 and "group_manual_ad_hold" in held["blockers"]
    await test_db.delete(hold); await test_db.commit()
    policy["rollout_accounts"][str(account.id)]["phase"] = "pilot"
    qualification.send_gate.return_value = "qualification_pilot_reservation_required"
    pilot = await capacity_snapshot(test_db, account.id, NOW)
    assert pilot["executable_now"]["ad"] == 1
    qualification.send_gate.return_value = "group_rule_prohibits"
    forbidden = await capacity_snapshot(test_db, account.id, NOW)
    assert forbidden["executable_now"]["ad"] == 0
    assert "group_rule_prohibits" in forbidden["blockers"]
    qualification.send_gate.return_value = None
    policy["rollout_accounts"][str(account.id)]["phase"] = "paused"
    paused = await capacity_snapshot(test_db, account.id, NOW)
    assert paused["quota_remaining"]["ad"] == 30 and paused["executable_now"]["ad"] == 0
    policy["rollout_accounts"][str(account.id)]["phase"] = "dynamic"
    test_db.add(
        AdDeliveryLog(
            account_id=account.id,
            group_id=member.group_id,
            telegram_group_id=member.telegram_group_id,
            ad_campaign_id=campaign.id,
            creative_id=creative.id,
            status="success",
            sent_at=NOW - timedelta(hours=1),
            created_at=NOW - timedelta(hours=1),
            survival_status="pending",
        )
    )
    await test_db.commit()
    capped = await capacity_snapshot(test_db, account.id, NOW)
    assert capped["executable_now"]["ad"] == 0
    assert "qualification_group_daily_cap" in capped["blockers"]


async def test_join_executable_requires_candidates_and_verification_budget(test_db):
    from app.core.settings_models import SystemSetting

    test_db.add(SystemSetting(key="automation.group_qualification", value='{"enabled":true}'))
    await test_db.commit()
    account, config = await account_config(test_db)
    await save_auto_join_scheduler_settings(test_db, {"enabled": True})
    empty = await capacity_snapshot(test_db, account.id, NOW)
    assert empty["quota_remaining"]["join"] == 30 and empty["executable_now"]["join"] == 0
    test_db.add(
        Group(group_id=-1000000999999, username="capacity_candidate", status="pending_join")
    )
    await test_db.commit()
    pending = await capacity_snapshot(test_db, account.id, NOW)
    assert pending["executable_now"]["join"] == 0
    assert pending["workload"]["join_candidates_preview_pending"] == 1
    from app.modules.acquisition import candidate_inventory
    from app.modules.acquisition.candidate_preview import CandidatePreview
    candidate = (await test_db.scalars(select(Group).where(Group.username == "capacity_candidate"))).one()
    await candidate_inventory.save(test_db, account.id, candidate.id, candidate_inventory.record(
        candidate, account.id, CandidatePreview("sampled", peer_namespace="channel", member_count=100,
                                                rule_signal="explicit_allow"), NOW
    ))
    await test_db.commit()
    ready = await capacity_snapshot(test_db, account.id, NOW)
    assert ready["executable_now"]["join"] == 1, str(ready["workload"])
    config.max_verification_messages_per_day = 0
    await test_db.commit()
    exhausted = await capacity_snapshot(test_db, account.id, NOW)
    assert exhausted["executable_now"]["join"] == 0
    assert "verification_budget_unavailable" in exhausted["blockers"]
