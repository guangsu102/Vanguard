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


async def outbound_reconciliation_counts(db, now: datetime) -> tuple[int, int, int]:
    """Return total unknown sends, blocking unknown sends, and parked receipts.

    A parked receipt has already been moved to the local evidence workflow.  It
    still protects its original group from another send, but it does not
    represent an account-wide reconciliation task that can be cleared by a
    generic health check.
    """
    from app.core.account.models import AccountOutboundAttempt
    from app.modules.acquisition.models import AdDeliveryLog

    rows = list(
        (
            await db.scalars(
                select(AccountOutboundAttempt).where(
                    AccountOutboundAttempt.state == "unknown"
                )
            )
        ).all()
    )
    if not rows:
        return 0, 0, 0

    tokens = {
        row.attempt_key[3:]
        for row in rows
        if row.category == "ad"
        and isinstance(row.attempt_key, str)
        and row.attempt_key.startswith("ad:")
        and len(row.attempt_key) > 3
    }
    logs = (
        list(
            (
                await db.scalars(
                    select(AdDeliveryLog).where(
                        AdDeliveryLog.reservation_token.in_(tokens)
                    )
                )
            ).all()
        )
        if tokens
        else []
    )
    by_attempt = {
        (log.account_id, log.reservation_token): log
        for log in logs
        if log.reservation_token
    }
    from app.core.account.send_receipts import parked_receipt

    parked = 0
    for row in rows:
        if (
            row.category == "ad"
            and isinstance(row.attempt_key, str)
            and row.attempt_key.startswith("ad:")
        ):
            log = by_attempt.get((row.account_id, row.attempt_key[3:]))
            if log is not None and parked_receipt(log, now):
                parked += 1
    return len(rows), len(rows) - parked, parked


async def operational_snapshot(db) -> dict:
    from app.core.account.models import (
        AccountOperationConfig,
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
    from app.core.scheduler.growth_dispatch import snapshot as dispatch_snapshot
    dispatch_state = await dispatch_snapshot(redis)
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
                memory = runtime.get("process_memory", {})
                workers[role]["process_memory"] = memory
                inbox = runtime.get("event_inbox", {})
                workers[role]["event_inbox"] = inbox
                if inbox.get("states", {}).get("reconciliation_required", 0):
                    alerts.append({"code": "listener_event_reconciliation_required", "severity": "warning"})
                if inbox.get("oldest_pending_seconds", 0) >= 120:
                    alerts.append({"code": "listener_business_backlog", "severity": "warning"})
                if (memory.get("rss_bytes") or 0) >= 2 * 1024**3:
                    alerts.append({"code": "growth_memory_high", "severity": "warning"})
                for item in listener_rows.values():
                    journal = item.get("raw_journal") or {}
                    if journal.get("storage_error"):
                        alerts.append({"code":"listener_raw_storage_failed","severity":"critical","account_id":item["account_id"]})
                    if journal.get("states",{}).get("reconciliation_required",0):
                        alerts.append({"code":"listener_event_reconciliation_required","severity":"warning","account_id":item["account_id"]})
                    disk_pending = journal.get("states", {}).get("pending", 0)
                    if disk_pending >= 1000 or (
                        disk_pending > 0 and journal.get("oldest_pending_seconds", 0) >= 120
                    ) or (item.get("update_queue_size") or 0) >= 1000 or (
                        (item.get("update_queue_size") or 0) > 0
                        and (item.get("queue_nonempty_seconds") or 0) >= 120
                    ):
                        alerts.append({"code": "listener_update_backlog", "severity": "warning",
                                       "account_id": item["account_id"]})
                    if item.get("sync_wait_reason") and (item.get("read_wait_seconds") or 0) >= 120:
                        alerts.append({"code": "listener_sync_delayed", "severity": "warning",
                                       "account_id": item["account_id"]})
                    if item.get("pause_error"):
                        alerts.append({"code": "listener_pause_failed", "severity": "critical",
                                       "account_id": item["account_id"]})
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
        elif listener.get("state") != "pausing" and rpc.get("lanes", {}).get("sync", {}).get("retry_after_seconds"):
            listener = {**listener, "state": "connected_wait", "reason": "telegram_sync_read_wait",
                        "resume_at": rpc["lanes"]["sync"].get("resume_at")}
        row = {
            "account_id": account.id,
            "listener": listener,
            "rpc": rpc,
            "inventory": inventory,
            "last_ad_at": last_ad.isoformat() if last_ad else None,
        }
        from app.modules.acquisition.survival_schedule import backlog
        row["survival"] = await backlog(db, account.id, now)
        if row["survival"]["survival_oldest_overdue_seconds"] >= 120:
            alerts.append({"code":"ad_survival_overdue","severity":"warning","account_id":account.id,
                           "count":row["survival"]["survival_overdue"]})
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
    unknown, unknown_blocking, receipt_evidence_required = (
        await outbound_reconciliation_counts(db, now)
    )
    if unknown_blocking:
        alerts.append(
            {
                "code": "outbound_reconciliation_required",
                "severity": "critical",
                "count": unknown_blocking,
            }
        )
    if receipt_evidence_required:
        alerts.append(
            {
                "code": "outbound_receipt_evidence_required",
                "severity": "warning",
                "count": receipt_evidence_required,
            }
        )
    tracking = await db.scalar(select(func.count(AcquisitionTracking.id)))
    # Campaign operations are measured by confirmed Telegram delivery. Missing
    # optional downstream attribution is not an advertising runtime failure.
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
        "growth_scheduler": dispatch_state,
        "redis_memory_ratio": round(ratio, 4),
        "recovery_heartbeat": heartbeat,
        "host": host,
        "outbound_unknown": unknown,
        "outbound_unknown_blocking": unknown_blocking,
        "outbound_receipt_evidence_required": receipt_evidence_required,
        "attribution_records": tracking,
    }
