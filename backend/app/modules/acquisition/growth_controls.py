"""Current growth limits and durable pauses, without issuing Telegram calls."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from app.core.account.models import AccountOperationConfig, TelegramAccount
from app.core.group.models import GroupAccountMembership as Member
from app.core.settings_models import SystemSetting
from app.modules.acquisition.models import AdDeliveryLog, AutoJoinAttempt

FAILURE_WINDOW = timedelta(hours=3)
FAILURE_GROUPS = 3


async def pause_repeated_permission_failures(db: Any, account_id: int, now: datetime) -> None:
    """Pause for diagnosis; a send error alone never proves an account is banned."""
    config = await db.scalar(select(AccountOperationConfig).where(
        AccountOperationConfig.account_id == account_id))
    if config is None or not config.enabled or not config.auto_ads_enabled:
        return
    last_success = await db.scalar(select(func.max(AdDeliveryLog.created_at)).where(
        AdDeliveryLog.account_id == account_id, AdDeliveryLog.status == "success"))
    start = max(now - FAILURE_WINDOW, last_success or now - FAILURE_WINDOW)
    groups = list((await db.scalars(select(AdDeliveryLog.telegram_group_id).where(
        AdDeliveryLog.account_id == account_id, AdDeliveryLog.status == "permission_changed",
        AdDeliveryLog.created_at > start, AdDeliveryLog.created_at <= now,
    ).distinct().limit(FAILURE_GROUPS))).all())
    if len(groups) < FAILURE_GROUPS:
        return
    key = f"growth.diagnostic_pause.account.{account_id}"
    marker = await db.get(SystemSetting, key)
    if marker:
        previous = json.loads(marker.value)
        if datetime.fromisoformat(previous["at"]) > now - timedelta(minutes=30):
            return
    payload = {"reason": "permission_failures_across_groups", "at": now.isoformat(), "until": (now + timedelta(minutes=10)).isoformat(),
               "groups": len(groups), "window_seconds": int(FAILURE_WINDOW.total_seconds()),
               "before": {"auto_join_enabled": config.auto_join_enabled,
                          "auto_ads_enabled": config.auto_ads_enabled}}
    if marker is None:
        db.add(SystemSetting(key=key, value=json.dumps(payload)))
    else:
        marker.value = json.dumps(payload)
    await db.commit()
    # The existing worker and session owner perform this diagnostic check.
    from app.modules.account_spam.service import enqueue_automatic_spam_check
    await enqueue_automatic_spam_check(db, account_id=account_id)


async def control_snapshot(db: Any, account_id: int, now: datetime) -> dict:
    from app.core.account.outbound_budget import effective_capacity_limits
    from app.core.automation_settings import (
        get_ad_delivery_execution_settings,
        get_ad_delivery_throttle_settings,
        get_auto_join_scheduler_settings,
    )
    from app.core.database_transaction_boundary import release_read_transaction
    from app.core.operating_time import operating_date, operating_day_start
    from app.core.redis import get_redis
    from app.modules.acquisition.growth_policy import account_block_reason
    from app.modules.acquisition.simple_limits import PREFIX, durable_outbound_used

    account = await db.get(TelegramAccount, account_id)
    config = await db.scalar(select(AccountOperationConfig).where(
        AccountOperationConfig.account_id == account_id))
    if account is None or config is None:
        return {"available": False, "reason": "account_operation_config_missing"}
    limits = await effective_capacity_limits(db, account, config, now)
    throttle = await get_ad_delivery_throttle_settings(db)
    execution = await get_ad_delivery_execution_settings(db)
    join_settings = await get_auto_join_scheduler_settings(db)
    ads_gap = max(limits["ad_interval_seconds"],
                  throttle["growth_min_interval_seconds"] if throttle["enabled"] else 0)
    join_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    ad_start = operating_day_start(now)
    join_used = await db.scalar(select(func.count(AutoJoinAttempt.id)).where(
        AutoJoinAttempt.account_id == account_id, AutoJoinAttempt.telegram_action_attempted.is_(True),
        AutoJoinAttempt.attempted_at >= join_start)) or 0
    ad_counts = dict((await db.execute(select(AdDeliveryLog.status, func.count(AdDeliveryLog.id)).where(
        AdDeliveryLog.account_id == account_id, AdDeliveryLog.created_at >= ad_start,
    ).group_by(AdDeliveryLog.status))).all())
    ad_used = sum(ad_counts.get(k, 0) for k in ("success", "unknown", "sending"))
    other_used = await durable_outbound_used(db, account_id, now)
    ready = await db.scalar(select(func.count(Member.id)).where(
        Member.account_id == account_id, Member.status == "joined", Member.left_at.is_(None),
        Member.review_status == "approved", Member.ad_status == "active",
        Member.verification_status == "complete",
        Member.leave_requested_at.is_(None))) or 0
    pause = await db.get(SystemSetting, f"growth.diagnostic_pause.account.{account_id}")
    data = {"available": True, "configured": {
        "join_daily": config.max_groups_per_day, "ad_daily": config.max_ads_per_day,
        "total_daily": config.max_messages_per_day,
        "join_interval_seconds": config.join_interval_min_seconds,
        "ad_interval_seconds": config.message_interval_seconds},
        "effective": {"join_daily": min(30, limits["join"]), "ad_daily": limits["ad"],
                      "total_daily": limits["total"],
                      "join_interval_seconds": max(7200 if config.business_stage == "cooldown" else 2880,
                                                   limits["join_interval_seconds"]),
                      "ad_interval_seconds": ads_gap},
        "enabled": {"account": config.enabled, "join": config.auto_join_enabled,
                    "ads": config.auto_ads_enabled, "global_join": join_settings["enabled"],
                    "global_ads": execution["enabled"]},
        "usage": {"join_requests": join_used, "ad_success": ad_counts.get("success", 0),
                  "ad_unknown": ad_counts.get("unknown", 0), "ad_in_flight": ad_counts.get("sending", 0), "ad_counted": ad_used,
                  "ad_permission_failed": ad_counts.get("permission_changed", 0),
                  "ad_failed": ad_counts.get("failed", 0)},
        "reset_at": {"join": join_start + timedelta(days=1), "ads": ad_start + timedelta(days=1)},
        "ready_memberships": ready,
        "account_reason": account_block_reason(account, now),
        "diagnostic_pause": json.loads(pause.value) if pause else None,
        "spam_check": {"status": account.spam_check_status, "checked_at": account.spam_checked_at}}
    if not data["account_reason"] and account_id in {33, 34, 35}:
        pilot = await db.get(SystemSetting, f"growth.canary.account.{account_id}")
        if not pilot or not json.loads(pilot.value).get("send_result_started_at"):
            data["account_reason"] = "observation_activation_required"
    diagnostic = data["diagnostic_pause"]
    if diagnostic:
        cleared = account.spam_check_status == "clear" and account.spam_checked_at and account.spam_checked_at > datetime.fromisoformat(diagnostic["at"])
        diagnostic["active"] = bool(not cleared and diagnostic.get("until") and datetime.fromisoformat(diagnostic["until"]) > now)
        if diagnostic["active"]:
            data["account_reason"] = "account_diagnostic_pause"
    data["observation"] = limits.get("observation")
    await release_read_transaction(db, allow_untracked_read_only=True)
    try:
        redis = await get_redis()
        seconds = now.replace(tzinfo=UTC).timestamp()
        redis_time = await redis.time()
        current = int(redis_time[0])
        for kind, expected_day in (("join", current // 86400), ("ads", (current + 28800) // 86400)):
            count = await redis.hgetall(PREFIX + f"{kind}:{account_id}:count")
            durable = join_used if kind == "join" else ad_used
            used = max(durable, int(count.get("used", 0)) if int(count.get("day", -1)) == expected_day else 0)
            cap = data["effective"]["join_daily" if kind == "join" else "ad_daily"]
            due = float(await redis.get(PREFIX + f"{kind}:{account_id}:due") or 0)
            reason = data["account_reason"]
            if not reason and not (data["enabled"]["account"] and
                                   data["enabled"]["join" if kind == "join" else "ads"] and
                                   data["enabled"]["global_join" if kind == "join" else "global_ads"]):
                reason = "automation_paused"
            if not reason and used >= cap:
                reason = "account_daily_cap"
                due = max(due, data["reset_at"][kind].replace(tzinfo=UTC).timestamp())
            elif not reason and due > seconds:
                reason = "account_cooldown"
            data.setdefault("execution", {})[kind] = {
                "used": used, "remaining": max(0, cap - used), "reason": reason,
                "next_allowed_at": datetime.fromtimestamp(due, UTC).replace(tzinfo=None) if due > seconds else None}
        shared = int(await redis.get(f"risk:account:{account_id}:daily:outbound_message:{operating_date(now):%Y%m%d}") or 0)
        total_used = max(shared, data["execution"]["ads"]["used"]) + other_used
        data["usage"]["total_counted"] = total_used
        if not data["execution"]["ads"]["reason"] and total_used >= limits["total"]:
            data["execution"]["ads"]["reason"] = "account_total_cap"
            data["execution"]["ads"]["next_allowed_at"] = min(ad_start + timedelta(days=1), now + timedelta(hours=1))
        elif not data["execution"]["ads"]["reason"] and not ready:
            data["execution"]["ads"]["reason"] = "permission_review_required"
    except Exception:
        data["execution"] = {"available": False, "reason": "redis_unavailable"}
    return data
