"""Operational evidence for authenticated UI and host-local monitoring."""

from datetime import datetime, timedelta

from sqlalchemy import func, select, text


async def dependencies() -> dict:
    import asyncio

    from app.core.database import get_db_session
    from app.core.redis import get_redis

    async with asyncio.timeout(4):
        async with get_db_session() as db:
            await db.execute(text("SELECT 1"))
        await (await get_redis()).ping()
    return {"database": "ready", "redis": "ready"}


async def operational_snapshot(db) -> dict:
    from app.core.account.models import (
        AccountOperationConfig,
        AccountOutboundAttempt,
        TelegramAccount,
    )
    from app.core.account.rpc_governor import snapshot
    from app.core.redis import get_redis
    from app.core.worker_status import TelegramWorkerStatus
    from app.modules.acquisition.capacity import inventory_snapshot
    from app.modules.acquisition.models import AcquisitionTracking, AdDeliveryLog

    now = datetime.utcnow()
    alerts, accounts = [], []
    import json

    redis = await get_redis()
    host_raw = await redis.get("vanguard:ops:host")
    host = json.loads(host_raw) if host_raw else {}
    alerts.extend(host.get("alerts", []))
    if not host_raw:
        alerts.append({"code": "host_monitor_stale", "severity": "warning"})
    heartbeat = await redis.get("vanguard:runtime_recovery:last_run_at")
    if not heartbeat or datetime.fromisoformat(heartbeat) < now - timedelta(minutes=3):
        alerts.append({"code": "recovery_scheduler_stale", "severity": "critical"})
    for role in ("growth_user_worker", "guardian_bot_worker"):
        last = await db.scalar(
            select(func.max(TelegramWorkerStatus.last_heartbeat_at)).where(
                TelegramWorkerStatus.role == role
            )
        )
        if not last or last < now - timedelta(minutes=3):
            alerts.append({"code": "worker_stale", "role": role, "severity": "critical"})
    active = (
        await db.scalars(
            select(TelegramAccount)
            .join(AccountOperationConfig)
            .where(
                AccountOperationConfig.enabled.is_(True),
                AccountOperationConfig.auto_ads_enabled.is_(True),
            )
        )
    ).all()
    for account in active:
        rpc = await snapshot(db, account.id, now)
        inventory = await inventory_snapshot(db, account.id, now)
        last_ad = await db.scalar(
            select(func.max(AdDeliveryLog.created_at)).where(
                AdDeliveryLog.account_id == account.id, AdDeliveryLog.status == "success"
            )
        )
        row = {
            "account_id": account.id,
            "rpc": rpc,
            "inventory": inventory,
            "last_ad_at": last_ad.isoformat() if last_ad else None,
        }
        accounts.append(row)
        if rpc["state"] == "cooldown":
            alerts.append(
                {
                    "code": "telegram_cooldown",
                    "account_id": account.id,
                    "severity": "warning",
                    "resume_at": rpc["pause_until"],
                }
            )
        elif rpc.get("pause_until"):
            end = datetime.fromisoformat(rpc["pause_until"])
            if now > end + timedelta(minutes=30) and (not last_ad or last_ad < end):
                alerts.append(
                    {
                        "code": "resume_without_delivery",
                        "account_id": account.id,
                        "severity": "warning",
                    }
                )
    unknown = await db.scalar(
        select(func.count(AccountOutboundAttempt.id)).where(
            AccountOutboundAttempt.state == "unknown"
        )
    )
    if unknown:
        alerts.append(
            {"code": "outbound_reconciliation_required", "severity": "critical", "count": unknown}
        )
    tracking = await db.scalar(select(func.count(AcquisitionTracking.id)))
    if active and not tracking:
        alerts.append({"code": "attribution_not_connected", "severity": "warning"})
    info = await redis.info("memory")
    maximum = int(info.get("maxmemory") or 0)
    ratio = int(info.get("used_memory") or 0) / maximum if maximum else 0
    if ratio > 0.8:
        alerts.append({"code": "redis_memory_high", "severity": "critical"})
    return {
        "checked_at": now.isoformat(),
        "status": "critical"
        if any(item["severity"] == "critical" for item in alerts)
        else ("warning" if alerts else "ready"),
        "alerts": alerts,
        "accounts": accounts,
        "redis_memory_ratio": round(ratio, 4),
        "recovery_heartbeat": heartbeat,
        "host": host,
        "outbound_unknown": unknown,
        "attribution_records": tracking,
    }
