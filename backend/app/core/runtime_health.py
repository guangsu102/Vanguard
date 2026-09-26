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
    workers = {}
    listener_rows = {}
    for role in ("growth_user_worker", "guardian_bot_worker"):
        worker = await db.scalar(select(TelegramWorkerStatus).where(
            TelegramWorkerStatus.role == role
        ).order_by(TelegramWorkerStatus.last_heartbeat_at.desc()).limit(1))
        last = worker.last_heartbeat_at if worker else None
        fresh = bool(last and last >= now - timedelta(minutes=3))
        workers[role] = {"heartbeat_at": last.isoformat() if last else None, "fresh": fresh}
        if not fresh:
            alerts.append({"code": "worker_stale", "role": role, "severity": "critical"})
        if role == "growth_user_worker" and fresh:
            try:
                runtime = json.loads(worker.metadata_json or "{}").get("runtime", {})
                listener_rows = {item["account_id"]: item for item in runtime.get("listeners", [])}
            except (ValueError, TypeError, KeyError):
                listener_rows = {}
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
        listener = listener_rows.get(account.id, {"account_id": account.id,
                                                "state": "unknown", "connected": False})
        if not listener.get("connected"):
            alerts.append({"code": "listener_status_unknown" if listener["state"] == "unknown" else "listener_not_connected",
                           "severity": "warning", "account_id": account.id,
                           "reason": listener.get("reason"), "resume_at": listener.get("resume_at")})
        elif rpc.get("lanes", {}).get("sync", {}).get("retry_after_seconds"):
            listener = {**listener, "state": "connected_wait", "reason": "telegram_sync_read_wait",
                        "resume_at": rpc["lanes"]["sync"].get("resume_at")}
        row = {
            "account_id": account.id,
            "listener": listener,
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
        "workers": workers,
        "redis_memory_ratio": round(ratio, 4),
        "recovery_heartbeat": heartbeat,
        "host": host,
        "outbound_unknown": unknown,
        "attribution_records": tracking,
    }
