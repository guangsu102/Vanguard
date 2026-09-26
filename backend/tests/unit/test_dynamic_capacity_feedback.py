import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.account.models import (
    AccountOperationConfig,
    AccountStatus,
    AccountType,
    TelegramAccount,
)
from app.core.account.outbound_budget import (
    AccountOutboundBudgetService,
    ActionCapacityFeedbackService,
    OutboundBudgetBlocked,
    effective_capacity_limits,
)
from app.core.account.risk_guard import AccountRiskAction, AccountRiskGuard
from app.modules.acquisition.join_budget import JoinRequestBudgetService

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 9, 23, 10)


async def pair(db, suffix="1", **extra):
    account = TelegramAccount(
        identifier="feedback" + suffix,
        session_name="feedback" + suffix,
        account_type=AccountType.PROMOTER,
        is_active=True,
        status=AccountStatus.ONLINE,
        risk_level="normal",
        registered_at=NOW - timedelta(days=365),
    )
    db.add(account)
    await db.flush()
    config = AccountOperationConfig(
        account_id=account.id,
        enabled=True,
        dynamic_capacity_enabled=True,
        auto_join_enabled=True,
        auto_ads_enabled=True,
        max_messages_per_day=62,
        max_ads_per_day=30,
        max_groups_per_day=30,
        message_interval_seconds=600,
        **extra,
    )
    db.add(config)
    await db.commit()
    return account, config


async def test_flood_is_persistent_and_only_scopes_one_account_action(test_db):
    account, config = await pair(test_db)
    other, other_config = await pair(test_db, "2")
    service = ActionCapacityFeedbackService(test_db)
    await service.record_flood(account.id, "ad_delivery", 120, now=NOW, event_key="send-one")
    limits = await effective_capacity_limits(test_db, account, config, NOW)
    assert limits["ad"] == 15 and limits["ad_interval_seconds"] == 1200
    assert limits["join"] == 30 and limits["join_interval_seconds"] == 2880
    assert datetime.fromisoformat(limits["action_pauses"]["ad"]) == NOW + timedelta(seconds=180)
    assert (await effective_capacity_limits(test_db, other, other_config, NOW))["ad"] == 30
    await ActionCapacityFeedbackService(test_db).record_flood(
        account.id, "ad_delivery", 120, now=NOW, event_key="send-one"
    )
    assert (await effective_capacity_limits(test_db, account, config, NOW))["ad"] == 15
    with pytest.raises(OutboundBudgetBlocked, match="outbound_ad_interval") as error:
        await AccountOutboundBudgetService(test_db).reserve(
            account.id, attempt_key="during-pause", category="ad", target_key="7", now=NOW
        )
    assert error.value.retry_after_seconds == 180
    await AccountOutboundBudgetService(test_db).reserve(
        account.id,
        attempt_key="after-pause",
        category="ad",
        target_key="7",
        now=NOW + timedelta(seconds=180),
    )


async def test_recovery_needs_real_success_and_one_step_per_24h(test_db):
    account, config = await pair(test_db)
    service = ActionCapacityFeedbackService(test_db)
    await service.record_flood(account.id, "join", 1, now=NOW)
    await service.record_success(account.id, "join", now=NOW + timedelta(hours=23))
    assert (await effective_capacity_limits(test_db, account, config, NOW + timedelta(days=7)))[
        "join"
    ] == 15
    await service.record_success(account.id, "join", now=NOW + timedelta(hours=24))
    assert (await effective_capacity_limits(test_db, account, config, NOW + timedelta(hours=24)))[
        "join"
    ] == 21
    await service.record_success(account.id, "join", now=NOW + timedelta(hours=25))
    assert (await effective_capacity_limits(test_db, account, config, NOW + timedelta(hours=25)))[
        "join"
    ] == 21
    await service.record_success(account.id, "join", now=NOW + timedelta(hours=48))
    assert (await effective_capacity_limits(test_db, account, config, NOW + timedelta(hours=48)))[
        "join"
    ] == 27
    await service.record_success(account.id, "join", now=NOW + timedelta(hours=72))
    limits = await effective_capacity_limits(test_db, account, config, NOW + timedelta(hours=72))
    assert limits["join"] == 30 and limits["join_interval_seconds"] == 2880


async def test_new_flood_resets_success_and_recovery_clock(test_db):
    account, config = await pair(test_db)
    service = ActionCapacityFeedbackService(test_db)
    await service.record_flood(account.id, "ad_delivery", 1, now=NOW)
    await service.record_success(account.id, "ad_delivery", now=NOW + timedelta(hours=24))
    await service.record_flood(account.id, "ad_delivery", 3600, now=NOW + timedelta(hours=25))
    limits = await effective_capacity_limits(test_db, account, config, NOW + timedelta(hours=26))
    assert limits["ad"] == 10 and limits["join"] == 30
    await service.record_success(account.id, "ad_delivery", now=NOW + timedelta(hours=48))
    assert (await effective_capacity_limits(test_db, account, config, NOW + timedelta(hours=48)))[
        "ad"
    ] == 10
    await service.record_success(account.id, "ad_delivery", now=NOW + timedelta(hours=49))
    assert (await effective_capacity_limits(test_db, account, config, NOW + timedelta(hours=49)))[
        "ad"
    ] == 16


async def test_watch_health_and_action_feedback_use_stricter_explicit_caps(test_db):
    account, config = await pair(test_db)
    account.risk_level = "watch"
    await test_db.commit()
    service = ActionCapacityFeedbackService(test_db)
    await service.record_flood(account.id, "join", 5, now=NOW)
    limits = await effective_capacity_limits(test_db, account, config, NOW)
    assert (limits["join"], limits["join_interval_seconds"], limits["ad"]) == (5, 14400, 10)
    await service.record_success(account.id, "join", now=NOW + timedelta(hours=24))
    limits = await effective_capacity_limits(test_db, account, config, NOW + timedelta(hours=24))
    assert limits["join"] == 10 and limits["join_interval_seconds"] >= 7200


async def test_risk_guard_records_dynamic_flood_without_freezing_other_actions(test_db):
    account, config = await pair(test_db)
    guard = AccountRiskGuard(test_db)

    class FloodWaitError(Exception):
        seconds = 120

    await guard.record_failure(
        SimpleNamespace(account_id=account.id),
        AccountRiskAction.JOIN,
        FloodWaitError("opaque RPC"),
        target_type="group",
        target_id=7,
        details={"join_reservation_key": "join-one"},
    )
    await test_db.refresh(account)
    assert (
        account.risk_pause_until is None
        and account.risk_score == 0
        and account.risk_level == "normal"
    )
    state = (await ActionCapacityFeedbackService(test_db).read(account.id))["join"]
    assert datetime.fromisoformat(state["pause_until"]) - datetime.fromisoformat(
        state["last_flood_at"]
    ) == timedelta(seconds=180)
    blocked = await guard.check_and_reserve(
        SimpleNamespace(account_id=account.id),
        AccountRiskAction.JOIN,
        target_type="group",
        target_id=8,
    )
    assert (
        not blocked.allowed
        and blocked.reason == "action_flood_wait"
        and 178 <= blocked.retry_after_seconds <= 181
    )


async def test_legacy_join_accepts_auditable_age_lower_bound(test_db):
    account, config = await pair(test_db)
    account.registered_at = None
    account.asset_verified_at = None
    account.age_attestation_json = json.dumps(
        {
            "source": "owner_confirmation",
            "minimum_age_days": 180,
            "confirmed_at": NOW.isoformat(),
            "confirmed_by": 1,
            "version": 1,
        }
    )
    config.dynamic_capacity_enabled = False
    await test_db.commit()
    assert (
        await JoinRequestBudgetService(test_db).eligibility_reason(
            account, config, NOW, require_auto_join_enabled=True, global_enabled=True
        )
        is None
    )


async def test_group_slow_mode_does_not_trigger_account_action_flood_controller():
    assert (
        AccountRiskGuard.classify_error(
            "SlowModeWaitError: A wait of 60 seconds is required",
            action=AccountRiskAction.AD_DELIVERY,
            target_type="group",
        )
        == "group_write_forbidden"
    )
