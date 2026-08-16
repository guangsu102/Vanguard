"""
Scheduled Tasks

Celery scheduled tasks with pragmatic production-safe fallbacks.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.celery import celery_app
from app.core.account.risk_guard import AccountRiskGuard
from app.core.account.system_identity import bot_risk_identity
from app.core.scheduler.alerts import AlertSeverity, TaskAlertManager

logger = structlog.get_logger()

AUTO_JOIN_SCHEDULER_LAST_RUN_KEY = "vanguard:auto_join_scheduler:last_run_at"
AUTO_JOIN_SCHEDULER_LOCK_KEY = "vanguard:auto_join_scheduler:lock"
AUTO_JOIN_SCHEDULER_LOCK_TTL_SECONDS = 60 * 60
AUTO_JOIN_SCHEDULER_LOCK_MIN_STALE_SECONDS = 10 * 60
AUTO_JOIN_SCHEDULER_LOCK_MAX_STALE_SECONDS = 45 * 60
AD_DELIVERY_LOCK_KEY = "vanguard:ad_delivery:lock"
AD_DELIVERY_LOCK_TTL_SECONDS = 60 * 60
AD_DELIVERY_LAST_RUN_KEY = "vanguard:ad_delivery:last_run_at"
GROUP_AI_WARMUP_LAST_RUN_KEY = "vanguard:group_ai_warmup:last_run_at"
GROUP_AI_WARMUP_LOCK_KEY = "vanguard:group_ai_warmup:lock"
GROUP_AI_WARMUP_LOCK_TTL_SECONDS = 30 * 60


def get_alert_manager() -> TaskAlertManager:
    """Get singleton alert manager instance."""
    return TaskAlertManager()


async def _run_with_worker_cleanup(awaitable: Awaitable[Any]) -> Any:
    """Run a Celery coroutine and dispose async DB connections before loop close."""
    try:
        return await awaitable
    finally:
        from app.core import database as db_module

        if db_module.engine is not None:
            await db_module.close_db()


def _run_async(awaitable: Awaitable[Any]) -> Any:
    return asyncio.run(_run_with_worker_cleanup(awaitable))


async def _ensure_db_initialized():
    from app.core import database as db_module

    if db_module.async_session_factory is None:
        await db_module.init_db(create_tables=False)
    return db_module


async def _run_with_db(
    handler: Callable[[AsyncSession], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    db_module = await _ensure_db_initialized()
    async with db_module.get_db_session() as db:
        return await handler(db)


def _skipped_result(task: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {"status": "skipped", "task": task, "reason": reason, **extra}


def _timestamp_to_iso(value: float) -> str:
    return datetime.utcfromtimestamp(value).isoformat()


def _auto_join_lock_stale_seconds(config: dict[str, Any]) -> int:
    try:
        interval_seconds = int(config.get("scan_interval_minutes", 30)) * 60
    except (TypeError, ValueError):
        interval_seconds = 30 * 60
    return min(
        max(AUTO_JOIN_SCHEDULER_LOCK_MIN_STALE_SECONDS, interval_seconds * 4),
        AUTO_JOIN_SCHEDULER_LOCK_MAX_STALE_SECONDS,
    )


def _auto_join_lock_value(now: float, source: str) -> str:
    return json.dumps({"started_at": now, "source": source})


def _parse_auto_join_lock(raw: Optional[str]) -> tuple[Optional[float], Optional[str]]:
    if not raw:
        return None, None

    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            started_at = payload.get("started_at")
            source = payload.get("source")
            return float(started_at), str(source) if source else None
    except (TypeError, ValueError, json.JSONDecodeError):
        pass

    try:
        return float(raw), None
    except (TypeError, ValueError):
        return None, None


async def _acquire_auto_join_execution_lock(
    client,
    *,
    now: float,
    config: dict[str, Any],
    source: str,
    locked_reason: str,
) -> dict[str, Any]:
    lock_stale_seconds = _auto_join_lock_stale_seconds(config)
    lock_value = _auto_join_lock_value(now, source)
    lock_acquired = await client.set(
        AUTO_JOIN_SCHEDULER_LOCK_KEY,
        lock_value,
        nx=True,
        ex=AUTO_JOIN_SCHEDULER_LOCK_TTL_SECONDS,
    )
    if not lock_acquired:
        lock_raw = await client.get(AUTO_JOIN_SCHEDULER_LOCK_KEY)
        lock_started_at, lock_source = _parse_auto_join_lock(lock_raw)

        if lock_started_at is not None and now - lock_started_at > lock_stale_seconds:
            await client.delete(AUTO_JOIN_SCHEDULER_LOCK_KEY)
            lock_acquired = await client.set(
                AUTO_JOIN_SCHEDULER_LOCK_KEY,
                lock_value,
                nx=True,
                ex=AUTO_JOIN_SCHEDULER_LOCK_TTL_SECONDS,
            )
            if lock_acquired:
                logger.warning(
                    "auto_join_stale_lock_reclaimed",
                    lock_started_at=_timestamp_to_iso(lock_started_at),
                    lock_age_seconds=int(now - lock_started_at),
                    lock_stale_seconds=lock_stale_seconds,
                    lock_source=lock_source,
                    source=source,
                )

    if not lock_acquired:
        lock_raw = await client.get(AUTO_JOIN_SCHEDULER_LOCK_KEY)
        lock_started_at, lock_source = _parse_auto_join_lock(lock_raw)
        lock_age = int(now - lock_started_at) if lock_started_at is not None else None
        return {
            "should_run": False,
            "reason": locked_reason,
            "config": config,
            "lock_started_at": _timestamp_to_iso(lock_started_at) if lock_started_at else None,
            "lock_age_seconds": lock_age,
            "lock_stale_seconds": lock_stale_seconds,
            "lock_source": lock_source,
        }

    return {
        "should_run": True,
        "reason": "due",
        "config": config,
        "started_at": now,
        "lock_stale_seconds": lock_stale_seconds,
        "lock_source": source,
    }


async def _new_scheduler_redis_client():
    import redis.asyncio as redis

    from app.core.config import settings

    return redis.from_url(
        settings.REDIS_URL,
        password=settings.REDIS_PASSWORD,
        encoding="utf-8",
        decode_responses=True,
    )


async def _close_scheduler_redis_client(client) -> None:
    close = getattr(client, "aclose", None) or getattr(client, "close", None)
    if close:
        result = close()
        if hasattr(result, "__await__"):
            await result


async def _reserve_scheduled_auto_join() -> dict[str, Any]:
    from app.modules.acquisition.models import AutoJoinAttempt, DeliveryStatus

    db_module = await _ensure_db_initialized()
    from app.core.automation_settings import get_auto_join_scheduler_settings

    async with db_module.get_db_session() as db:
        config = await get_auto_join_scheduler_settings(db)
    interval_seconds = int(config["scan_interval_minutes"]) * 60
    lock_stale_seconds = _auto_join_lock_stale_seconds(config)
    now = time.time()

    if not config["enabled"]:
        return {
            "should_run": False,
            "reason": "scheduler_disabled",
            "config": config,
        }

    client = None
    try:
        client = await _new_scheduler_redis_client()
        last_raw = await client.get(AUTO_JOIN_SCHEDULER_LAST_RUN_KEY)
        last_run_at = float(last_raw) if last_raw else None
        if last_run_at is None:
            async with db_module.get_db_session() as db:
                recent_success = await db.execute(
                    select(AutoJoinAttempt.joined_at)
                    .where(
                        AutoJoinAttempt.status == DeliveryStatus.SUCCESS.value,
                        AutoJoinAttempt.joined_at.isnot(None),
                    )
                    .order_by(AutoJoinAttempt.joined_at.desc())
                    .limit(1)
                )
                recent_joined_at = recent_success.scalar_one_or_none()
                if recent_joined_at is not None:
                    last_run_at = recent_joined_at.timestamp()
                    await client.set(AUTO_JOIN_SCHEDULER_LAST_RUN_KEY, str(last_run_at))
        if last_run_at is not None and now - last_run_at < interval_seconds:
            next_run_at = last_run_at + interval_seconds
            return {
                "should_run": False,
                "reason": "scheduler_interval",
                "config": config,
                "last_run_at": _timestamp_to_iso(last_run_at),
                "next_run_after": _timestamp_to_iso(next_run_at),
                "remaining_seconds": max(0, int(next_run_at - now)),
            }

        lock_result = await _acquire_auto_join_execution_lock(
            client,
            now=now,
            config=config,
            source="scheduled",
            locked_reason="scheduler_locked",
        )
        if not lock_result.get("should_run"):
            return lock_result

        last_raw = await client.get(AUTO_JOIN_SCHEDULER_LAST_RUN_KEY)
        last_run_at = float(last_raw) if last_raw else None
        if last_run_at is not None and now - last_run_at < interval_seconds:
            await client.delete(AUTO_JOIN_SCHEDULER_LOCK_KEY)
            next_run_at = last_run_at + interval_seconds
            return {
                "should_run": False,
                "reason": "scheduler_interval",
                "config": config,
                "last_run_at": _timestamp_to_iso(last_run_at),
                "next_run_after": _timestamp_to_iso(next_run_at),
                "remaining_seconds": max(0, int(next_run_at - now)),
            }

        return {
            "should_run": True,
            "reason": "due",
            "config": config,
            "started_at": now,
            "lock_stale_seconds": lock_stale_seconds,
            "lock_source": "scheduled",
        }
    except Exception as exc:
        logger.warning("auto_join_scheduler_state_unavailable", error=str(exc))
        return {
            "should_run": False,
            "reason": "scheduler_state_unavailable",
            "config": config,
            "started_at": now,
        }
    finally:
        if client is not None:
            await _close_scheduler_redis_client(client)


async def _reserve_manual_auto_join() -> dict[str, Any]:
    db_module = await _ensure_db_initialized()
    from app.core.automation_settings import get_auto_join_scheduler_settings

    async with db_module.get_db_session() as db:
        config = await get_auto_join_scheduler_settings(db)
    now = time.time()
    client = None
    try:
        client = await _new_scheduler_redis_client()
        result = await _acquire_auto_join_execution_lock(
            client,
            now=now,
            config=config,
            source="manual",
            locked_reason="auto_join_locked",
        )
        if result.get("should_run"):
            result["reason"] = "manual"
        return result
    except Exception as exc:
        logger.warning("auto_join_execution_lock_unavailable", error=str(exc), source="manual")
        return {
            "should_run": False,
            "reason": "auto_join_lock_unavailable",
            "config": config,
            "started_at": now,
        }
    finally:
        if client is not None:
            await _close_scheduler_redis_client(client)


async def _finish_auto_join_execution() -> None:
    client = None
    try:
        client = await _new_scheduler_redis_client()
        await client.set(AUTO_JOIN_SCHEDULER_LAST_RUN_KEY, str(time.time()))
        await client.delete(AUTO_JOIN_SCHEDULER_LOCK_KEY)
    except Exception as exc:
        logger.warning("auto_join_execution_finish_failed", error=str(exc))
    finally:
        if client is not None:
            await _close_scheduler_redis_client(client)


async def _reserve_ad_delivery_execution() -> dict[str, Any]:
    now = time.time()
    client = None
    try:
        await _ensure_db_initialized()
        from app.core import database as db_module
        from app.core.automation_settings import (
            get_ad_capacity_settings,
            get_ad_delivery_execution_settings,
        )

        async with db_module.get_db_session() as db:
            execution = await get_ad_delivery_execution_settings(db)
            capacity = await get_ad_capacity_settings(db)
        if not execution["enabled"]:
            return {
                "should_run": False,
                "reason": "ad_delivery_disabled",
                "execution": execution,
            }

        client = await _new_scheduler_redis_client()
        last_run_raw = await client.get(AD_DELIVERY_LAST_RUN_KEY)
        try:
            last_run_at = float(last_run_raw) if last_run_raw else None
        except (TypeError, ValueError):
            last_run_at = None
        interval_seconds = int(execution["dispatcher_interval_seconds"])
        if capacity.get("enabled", True):
            interval_seconds = min(interval_seconds, 60)
        if last_run_at is not None and now - last_run_at < interval_seconds:
            return {
                "should_run": False,
                "reason": "ad_delivery_interval",
                "next_run_at": _timestamp_to_iso(last_run_at + interval_seconds),
                "execution": execution,
            }

        lock_value = json.dumps({"started_at": now, "source": "deliver_ads_task"})
        lock_acquired = await client.set(
            AD_DELIVERY_LOCK_KEY,
            lock_value,
            nx=True,
            ex=AD_DELIVERY_LOCK_TTL_SECONDS,
        )
        if lock_acquired:
            return {
                "should_run": True,
                "reason": "due",
                "started_at": now,
                "lock_ttl_seconds": AD_DELIVERY_LOCK_TTL_SECONDS,
                "execution": execution,
            }

        lock_raw = await client.get(AD_DELIVERY_LOCK_KEY)
        lock_started_at, lock_source = _parse_auto_join_lock(lock_raw)
        lock_age = int(now - lock_started_at) if lock_started_at is not None else None
        return {
            "should_run": False,
            "reason": "ad_delivery_locked",
            "lock_started_at": _timestamp_to_iso(lock_started_at) if lock_started_at else None,
            "lock_age_seconds": lock_age,
            "lock_source": lock_source,
            "lock_ttl_seconds": AD_DELIVERY_LOCK_TTL_SECONDS,
            "execution": execution,
        }
    except Exception as exc:
        logger.warning("ad_delivery_execution_lock_unavailable", error=str(exc))
        return {
            "should_run": False,
            "reason": "ad_delivery_lock_unavailable",
            "started_at": now,
        }
    finally:
        if client is not None:
            await _close_scheduler_redis_client(client)


async def _finish_ad_delivery_execution() -> None:
    client = None
    try:
        client = await _new_scheduler_redis_client()
        await client.set(AD_DELIVERY_LAST_RUN_KEY, str(time.time()))
        await client.delete(AD_DELIVERY_LOCK_KEY)
    except Exception as exc:
        logger.warning("ad_delivery_execution_finish_failed", error=str(exc))
    finally:
        if client is not None:
            await _close_scheduler_redis_client(client)


async def _reserve_group_ai_warmup_execution() -> dict[str, Any]:
    now = time.time()
    client = None
    try:
        await _ensure_db_initialized()
        from app.core import database as db_module
        from app.core.automation_settings import get_group_ai_interaction_settings

        async with db_module.get_db_session() as db:
            config = await get_group_ai_interaction_settings(db)
        if not (config.get("enabled") and config.get("allowProactiveWarmup")):
            return {"should_run": False, "reason": "group_ai_warmup_disabled", "config": config}

        interval_seconds = max(60, int(config.get("proactiveWarmupIntervalMinutes") or 30) * 60)
        client = await _new_scheduler_redis_client()
        last_run_raw = await client.get(GROUP_AI_WARMUP_LAST_RUN_KEY)
        try:
            last_run_at = float(last_run_raw) if last_run_raw else None
        except (TypeError, ValueError):
            last_run_at = None
        if last_run_at is not None and now - last_run_at < interval_seconds:
            return {
                "should_run": False,
                "reason": "group_ai_warmup_interval",
                "next_run_at": _timestamp_to_iso(last_run_at + interval_seconds),
                "config": config,
            }

        lock_value = json.dumps({"started_at": now, "source": "group_ai_warmup_task"})
        lock_acquired = await client.set(
            GROUP_AI_WARMUP_LOCK_KEY,
            lock_value,
            nx=True,
            ex=max(GROUP_AI_WARMUP_LOCK_TTL_SECONDS, min(interval_seconds, 3600)),
        )
        if lock_acquired:
            return {"should_run": True, "reason": "due", "started_at": now, "config": config}

        lock_raw = await client.get(GROUP_AI_WARMUP_LOCK_KEY)
        lock_started_at, lock_source = _parse_auto_join_lock(lock_raw)
        return {
            "should_run": False,
            "reason": "group_ai_warmup_locked",
            "lock_started_at": _timestamp_to_iso(lock_started_at) if lock_started_at else None,
            "lock_source": lock_source,
            "config": config,
        }
    except Exception as exc:
        logger.warning("group_ai_warmup_execution_lock_unavailable", error=str(exc))
        return {
            "should_run": False,
            "reason": "group_ai_warmup_lock_unavailable",
            "started_at": now,
        }
    finally:
        if client is not None:
            await _close_scheduler_redis_client(client)


async def _finish_group_ai_warmup_execution() -> None:
    client = None
    try:
        client = await _new_scheduler_redis_client()
        await client.set(GROUP_AI_WARMUP_LAST_RUN_KEY, str(time.time()))
        await client.delete(GROUP_AI_WARMUP_LOCK_KEY)
    except Exception as exc:
        logger.warning("group_ai_warmup_execution_finish_failed", error=str(exc))
    finally:
        if client is not None:
            await _close_scheduler_redis_client(client)


async def _health_check_accounts_async() -> dict[str, Any]:
    from app.core.account.manager import AccountManager
    from app.core.account.models import AccountStatus
    from app.core.account.pool import AccountPool, _resolve_account_api_credentials

    async def handler(db: AsyncSession) -> dict[str, Any]:
        manager = AccountManager(db)
        accounts = await manager.list_accounts(limit=10_000)
        runtime_accounts = []
        skipped_inactive = 0
        for account in accounts:
            if not account.is_active:
                skipped_inactive += 1
                continue
            api_id, api_hash = _resolve_account_api_credentials(account)
            if account.phone and api_id and api_hash:
                runtime_accounts.append(account)
        pool = AccountPool()
        await pool.sync_from_db(runtime_accounts)
        summary = await pool.health_check()
        await pool.close_all()
        unhealthy = sum(
            1
            for account in runtime_accounts
            if account.status in {AccountStatus.ERROR, AccountStatus.BANNED}
        )
        return {
            "checked": len(runtime_accounts),
            "healthy": max(len(runtime_accounts) - unhealthy, 0),
            "unhealthy": unhealthy,
            "skipped_inactive": skipped_inactive,
            "summary": summary,
        }

    return await _run_with_db(handler)


async def _health_check_proxies_async() -> dict[str, Any]:
    from app.core.network.proxy_pool import ProxyPool

    async def handler(db: AsyncSession) -> dict[str, Any]:
        pool = ProxyPool(db)
        return await pool.health_check_all()

    return await _run_with_db(handler)


async def _group_snapshot_async(task_name: str) -> dict[str, Any]:
    from app.core.group.manager import GroupManager

    async def handler(db: AsyncSession) -> dict[str, Any]:
        manager = GroupManager(db)
        stats = await manager.get_group_stats()
        return _skipped_result(task_name, "telegram_group_sync_not_implemented", stats=stats)

    return await _run_with_db(handler)


async def _check_proxy_status_async() -> dict[str, Any]:
    from app.core.network.proxy_pool import ProxyPool

    async def handler(db: AsyncSession) -> dict[str, Any]:
        pool = ProxyPool(db)
        return await pool.check_and_cleanup()

    return await _run_with_db(handler)


async def _cleanup_expired_tokens_async() -> dict[str, Any]:
    return _skipped_result("cleanup_expired_tokens", "persistent_auth_token_store_not_implemented")


async def _cleanup_old_messages_async() -> dict[str, Any]:
    return _skipped_result("cleanup_old_messages", "message_retention_cleanup_not_implemented")


async def _maintain_account_risk_async() -> dict[str, Any]:
    async def handler(db: AsyncSession) -> dict[str, Any]:
        guard = AccountRiskGuard(db)
        decay = await guard.decay_risk_scores()
        cleanup = await guard.cleanup_risk_events()
        return {"decay": decay, "cleanup": cleanup}

    return await _run_with_db(handler)


async def _check_user_states_async() -> dict[str, Any]:
    from app.core.user.models import User, UserState

    async def handler(db: AsyncSession) -> dict[str, Any]:
        now = datetime.utcnow()
        rows = await db.execute(
            select(User).where(
                User.trial_expires_at.is_not(None),
                User.trial_expires_at <= now,
                User.state.in_([UserState.PENDING, UserState.ACTIVE]),
            )
        )
        users = list(rows.scalars().all())
        updated = 0
        for user in users:
            if user.state != UserState.SILENT:
                user.state = UserState.SILENT
                updated += 1
        return {"checked": len(users), "updated": updated}

    return await _run_with_db(handler)


async def _generate_daily_report_async() -> dict[str, Any]:
    from app.core.campaign.models import Campaign, CampaignTracking
    from app.core.group.models import Group
    from app.core.user.models import User

    async def handler(db: AsyncSession) -> dict[str, Any]:
        campaign_count = (await db.execute(select(func.count(Campaign.id)))).scalar() or 0
        tracking_count = (await db.execute(select(func.count(CampaignTracking.id)))).scalar() or 0
        user_count = (await db.execute(select(func.count(User.id)))).scalar() or 0
        group_count = (await db.execute(select(func.count(Group.id)))).scalar() or 0
        return {
            "generated": True,
            "generated_at": datetime.utcnow().isoformat(),
            "report": {
                "campaigns": campaign_count,
                "trackings": tracking_count,
                "users": user_count,
                "groups": group_count,
            },
        }

    return await _run_with_db(handler)


async def _broadcast_node_status_async() -> dict[str, Any]:
    return _skipped_result(
        "broadcast_node_status", "node_status_broadcast_integration_not_implemented"
    )


async def _execute_broadcast_record_async(broadcast_id: int) -> dict[str, Any]:
    from app.api.broadcasts import BroadcastRecord
    from app.core.config import settings
    from app.integrations.telegram.client import TelegramClient, TelegramConfig

    async def handler(db: AsyncSession) -> dict[str, Any]:
        result = await db.execute(select(BroadcastRecord).where(BroadcastRecord.id == broadcast_id))
        broadcast = result.scalar_one_or_none()
        if not broadcast:
            return _skipped_result(
                "execute_broadcast_record", "broadcast_not_found", broadcast_id=broadcast_id
            )

        try:
            target_groups = json.loads(broadcast.target_groups or "[]")
        except Exception:
            target_groups = []

        if not broadcast.content or not target_groups:
            broadcast.status = "failed"
            broadcast.failed_count = len(target_groups)
            broadcast.completed_at = datetime.utcnow()
            await db.commit()
            return {
                "status": "failed",
                "broadcast_id": broadcast_id,
                "reason": "empty_content_or_targets",
                "success": 0,
                "failed": len(target_groups),
            }

        if not settings.BOT_TOKEN:
            broadcast.status = "failed"
            broadcast.failed_count = len(target_groups)
            broadcast.completed_at = datetime.utcnow()
            await db.commit()
            return {
                "status": "failed",
                "broadcast_id": broadcast_id,
                "reason": "bot_token_not_configured",
                "success": 0,
                "failed": len(target_groups),
            }

        broadcast.status = "sending"
        await db.commit()

        client = TelegramClient(
            TelegramConfig(bot_token=settings.BOT_TOKEN),
            risk_guard=AccountRiskGuard(db),
            risk_account=bot_risk_identity("scheduler"),
        )
        success = 0
        failed_groups: list[int] = []

        try:
            for group_id in target_groups:
                try:
                    await client.send_message(group_id, broadcast.content, parse_mode="Markdown")
                    success += 1
                except Exception as exc:
                    logger.warning(
                        "execute_broadcast_record_send_failed",
                        broadcast_id=broadcast_id,
                        group_id=group_id,
                        error=str(exc),
                    )
                    failed_groups.append(group_id)
        finally:
            await client.close()

        broadcast.success_count = success
        broadcast.failed_count = len(failed_groups)
        broadcast.status = "completed" if success > 0 else "failed"
        broadcast.completed_at = datetime.utcnow()
        await db.commit()

        return {
            "status": broadcast.status,
            "broadcast_id": broadcast_id,
            "success": success,
            "failed": len(failed_groups),
            "failed_groups": failed_groups,
        }

    return await _run_with_db(handler)


async def _process_pending_campaigns_async() -> dict[str, Any]:
    from app.core.campaign.runner import run_global_campaigns_with_db
    from app.modules.guardian.campaign_runner import run_managed_group_campaigns_with_db

    now = datetime.utcnow()
    global_result = await run_global_campaigns_with_db(now=now)
    managed_group_result = await run_managed_group_campaigns_with_db(now=now)
    keys = set(global_result) | set(managed_group_result)
    combined = {
        key: int(global_result.get(key, 0) or 0) + int(managed_group_result.get(key, 0) or 0)
        for key in keys
        if isinstance(global_result.get(key, 0), int)
        or isinstance(managed_group_result.get(key, 0), int)
    }
    return {
        **combined,
        "global": global_result,
        "managed_group": managed_group_result,
    }


async def _send_bulk_messages_async(targets: list, message: str) -> dict[str, Any]:
    from app.core.config import settings
    from app.integrations.telegram.client import TelegramClient, TelegramConfig

    if not settings.BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not configured")

    async def handler(db: AsyncSession) -> dict[str, Any]:
        client = TelegramClient(
            TelegramConfig(bot_token=settings.BOT_TOKEN),
            risk_guard=AccountRiskGuard(db),
            risk_account=bot_risk_identity("scheduler_bulk_messages"),
        )
        results = {"success": 0, "failed": 0, "failed_targets": []}

        try:
            for index, target in enumerate(targets):
                try:
                    await client.send_message(target, message)
                    results["success"] += 1
                    if (index + 1) % 100 == 0:
                        logger.info(
                            "send_bulk_messages_progress", processed=index + 1, total=len(targets)
                        )
                except Exception as exc:
                    logger.warning("send_bulk_message_failed", target=target, error=str(exc))
                    results["failed"] += 1
                    results["failed_targets"].append(target)
        finally:
            await client.close()

        return results

    return await _run_with_db(handler)


async def _import_accounts_batch_async(accounts_data: list[dict[str, Any]]) -> dict[str, Any]:
    from app.core.account.manager import AccountManager
    from app.core.account.models import AccountType

    async def handler(db: AsyncSession) -> dict[str, Any]:
        manager = AccountManager(db)
        results = {"total": len(accounts_data), "imported": 0, "failed": 0, "errors": []}

        for data in accounts_data:
            try:
                api_id = str(data["api_id"])
                api_hash = str(data["api_hash"])
                api_config_name = str(data.get("api_config_name") or f"imported_{api_id}")
                if await manager.get_api_config(api_config_name) is None:
                    await manager.create_api_config(
                        name=api_config_name,
                        api_id=api_id,
                        api_hash=api_hash,
                        description="Imported by scheduler batch task",
                    )

                raw_type = str(data.get("account_type") or AccountType.PROMOTER.value)
                account_type = AccountType(raw_type)
                await manager.create_account(
                    phone=data.get("phone"),
                    api_config_name=api_config_name,
                    country_code=str(data.get("country_code") or "US"),
                    country_name=data.get("country_name"),
                    session_name=data.get("session_name"),
                    identifier=data.get("identifier"),
                    display_name=data.get("display_name"),
                    account_type=account_type,
                )
                results["imported"] += 1
            except Exception as exc:
                logger.error("import_account_failed", phone=data.get("phone"), error=str(exc))
                results["failed"] += 1
                results["errors"].append({"phone": data.get("phone"), "error": str(exc)})

        return results

    return await _run_with_db(handler)


async def _process_user_registration_async(
    user_id: int, campaign_id: Optional[int]
) -> dict[str, Any]:
    from app.core.campaign.engine import CampaignEngine
    from app.core.campaign.runner import CampaignRunner
    from app.core.user.models import User

    async def handler(db: AsyncSession) -> dict[str, Any]:
        user = await db.get(User, user_id)
        if user is None:
            return {"status": "error", "message": "User not found", "user_id": user_id}

        runner = CampaignRunner(db)
        if not campaign_id:
            executions = await runner.trigger_for_registration(
                user,
                metadata={"source": "scheduler:user_registration"},
            )
            return {
                "status": "success",
                "user_id": user_id,
                "executed": len(executions),
                "rewarded": sum(1 for item in executions if item.reward_granted),
                "details": [asdict(item) for item in executions],
            }

        engine = CampaignEngine(db)
        campaign = await engine.get_campaign(campaign_id)
        if campaign is None:
            return {"status": "error", "message": "Campaign not found", "campaign_id": campaign_id}

        execution = await runner.trigger_campaign(
            campaign=campaign,
            user=user,
            metadata={"source": "scheduler:user_registration"},
        )

        return {
            "status": "success",
            "user_id": user_id,
            "campaign_id": campaign_id,
            "reward": getattr(execution, "reward_granted", False),
            "details": [asdict(item) for item in execution]
            if isinstance(execution, list)
            else asdict(execution),
        }

    return await _run_with_db(handler)


async def _execute_campaign_rewards_async(
    campaign_id: int, user_id: Optional[int] = None
) -> dict[str, Any]:
    from app.core.campaign.engine import CampaignEngine
    from app.core.campaign.models import CampaignDistributionMode, CampaignScope
    from app.core.campaign.runner import CampaignRunner
    from app.modules.guardian.campaign_runner import ManagedGroupCampaignRunner
    from app.modules.guardian.models import GroupCampaignTriggerEvent

    async def handler(db: AsyncSession) -> dict[str, Any]:
        engine = CampaignEngine(db)
        campaign = await engine.get_campaign(campaign_id)
        if campaign is None:
            return {"status": "error", "message": "Campaign not found", "campaign_id": campaign_id}

        if campaign.campaign_scope == CampaignScope.MANAGED_GROUP and (
            campaign.trigger_event == GroupCampaignTriggerEvent.MANUAL_BROADCAST.value
            or (
                not campaign.trigger_event
                and campaign.distribution_mode == CampaignDistributionMode.MANUAL
            )
        ):
            runner = ManagedGroupCampaignRunner(db)
            execution = await runner.trigger_manual_broadcast(campaign)
            return {
                "status": "success",
                "campaign_id": campaign_id,
                "distributed": 1 if execution.delivered else 0,
                "details": {
                    "triggered": execution.triggered,
                    "delivered": execution.delivered,
                    "reward_granted": execution.reward_granted,
                    "reason": execution.reason,
                },
            }

        runner = CampaignRunner(db)
        execution = await runner.trigger_campaign(campaign=campaign, user_id=user_id, manual=True)
        executions = execution if isinstance(execution, list) else [execution]
        return {
            "status": "success",
            "campaign_id": campaign_id,
            "distributed": sum(1 for item in executions if item.reward_granted),
            "delivered": sum(1 for item in executions if item.delivered),
            "executed": sum(1 for item in executions if item.triggered),
            "details": [asdict(item) for item in executions],
        }

    return await _run_with_db(handler)


async def _validate_proxy_batch_async(proxy_ids: list[int]) -> dict[str, Any]:
    from app.core.network.proxy_pool import ProxyPool

    async def handler(db: AsyncSession) -> dict[str, Any]:
        pool = ProxyPool(db)
        return await pool.validate_batch(proxy_ids)

    return await _run_with_db(handler)


async def _send_trial_reminder_async(user_id: int, hours_before_expiry: int) -> dict[str, Any]:
    from app.core.config import settings
    from app.core.user.models import User
    from app.integrations.telegram.client import TelegramClient, TelegramConfig

    async def handler(db: AsyncSession) -> dict[str, Any]:
        user = await db.get(User, user_id)
        if user is None:
            return {"sent": False, "reason": "user_not_found", "user_id": user_id}
        if user.trial_expires_at is None:
            return {"sent": False, "reason": "trial_not_active", "user_id": user_id}
        if not settings.BOT_TOKEN:
            return {"sent": False, "reason": "bot_token_not_configured", "user_id": user_id}

        message = (
            f"提醒：您的试用将在约 {hours_before_expiry} 小时后到期。\n"
            f"到期时间：{user.trial_expires_at.isoformat()}"
        )
        client = TelegramClient(
            TelegramConfig(bot_token=settings.BOT_TOKEN),
            risk_guard=AccountRiskGuard(db),
            risk_account=bot_risk_identity("scheduler"),
        )
        try:
            await client.send_message(user.telegram_id, message)
        finally:
            await client.close()

        return {
            "sent": True,
            "user_id": user_id,
            "hours_before_expiry": hours_before_expiry,
            "trial_expires_at": user.trial_expires_at.isoformat(),
        }

    return await _run_with_db(handler)


# =============================================================================
# Periodic Tasks (Triggered by Celery Beat)
# =============================================================================


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def health_check_accounts(self):
    logger.info("health_check_accounts", task="health_check_accounts")
    try:
        result = _run_async(_health_check_accounts_async())
        logger.info(
            "health_check_accounts_completed",
            checked=result.get("checked", 0),
            healthy=result.get("healthy", 0),
            unhealthy=result.get("unhealthy", 0),
            skipped_inactive=result.get("skipped_inactive", 0),
        )
        return result
    except Exception as exc:
        logger.error("health_check_accounts_failed", error=str(exc))
        get_alert_manager().send_task_alert(
            task_name="health_check_accounts",
            error=str(exc),
            severity=AlertSeverity.WARNING,
        )
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def health_check_proxies(self):
    logger.info("health_check_proxies", task="health_check_proxies")
    try:
        result = _run_async(_health_check_proxies_async())
        logger.info(
            "health_check_proxies_completed",
            checked=result.get("checked", 0),
            healthy=result.get("healthy", 0),
            unhealthy=result.get("unhealthy", 0),
        )
        return result
    except Exception as exc:
        logger.error("health_check_proxies_failed", error=str(exc))
        get_alert_manager().send_task_alert(
            task_name="health_check_proxies",
            error=str(exc),
            severity=AlertSeverity.WARNING,
        )
        raise self.retry(exc=exc)


@celery_app.task
def sync_group_metrics():
    logger.info("sync_group_metrics", task="sync_group_metrics")
    try:
        return _run_async(_group_snapshot_async("sync_group_metrics"))
    except Exception as exc:
        logger.error("sync_group_metrics_failed", error=str(exc))
        get_alert_manager().send_task_alert(
            task_name="sync_group_metrics",
            error=str(exc),
            severity=AlertSeverity.WARNING,
        )
        return {"error": str(exc)}


@celery_app.task
def check_proxy_status():
    logger.info("check_proxy_status", task="check_proxy_status")
    try:
        return _run_async(_check_proxy_status_async())
    except Exception as exc:
        logger.error("check_proxy_status_failed", error=str(exc))
        return {"error": str(exc)}


@celery_app.task
def cleanup_expired_tokens():
    logger.info("cleanup_expired_tokens", task="cleanup_expired_tokens")
    try:
        return _run_async(_cleanup_expired_tokens_async())
    except Exception as exc:
        logger.error("cleanup_expired_tokens_failed", error=str(exc))
        return {"error": str(exc)}


@celery_app.task
def cleanup_old_messages():
    logger.info("cleanup_old_messages", task="cleanup_old_messages")
    try:
        return _run_async(_cleanup_old_messages_async())
    except Exception as exc:
        logger.error("cleanup_old_messages_failed", error=str(exc))
        return {"error": str(exc)}


@celery_app.task
def maintain_account_risk():
    logger.info("maintain_account_risk", task="maintain_account_risk")
    try:
        return _run_async(_maintain_account_risk_async())
    except Exception as exc:
        logger.error("maintain_account_risk_failed", error=str(exc))
        return {"error": str(exc)}


@celery_app.task
def sync_group_info():
    logger.info("sync_group_info", task="sync_group_info")
    try:
        return _run_async(_group_snapshot_async("sync_group_info"))
    except Exception as exc:
        logger.error("sync_group_info_failed", error=str(exc))
        return {"error": str(exc)}


@celery_app.task
def check_user_states():
    logger.info("check_user_states", task="check_user_states")
    try:
        result = _run_async(_check_user_states_async())
        logger.info(
            "check_user_states_completed",
            checked=result.get("checked", 0),
            updated=result.get("updated", 0),
        )
        return result
    except Exception as exc:
        logger.error("check_user_states_failed", error=str(exc))
        return {"error": str(exc)}


@celery_app.task
def generate_daily_report():
    logger.info("generate_daily_report", task="generate_daily_report")
    try:
        result = _run_async(_generate_daily_report_async())
        logger.info("generate_daily_report_completed", generated=result.get("generated", False))
        return result
    except Exception as exc:
        logger.error("generate_daily_report_failed", error=str(exc))
        get_alert_manager().send_task_alert(
            task_name="generate_daily_report",
            error=str(exc),
            severity=AlertSeverity.WARNING,
        )
        return {"error": str(exc)}


@celery_app.task
def broadcast_node_status():
    logger.info("broadcast_node_status", task="broadcast_node_status")
    try:
        return _run_async(_broadcast_node_status_async())
    except Exception as exc:
        logger.error("broadcast_node_status_failed", error=str(exc))
        return {"error": str(exc)}


@celery_app.task(
    bind=True, max_retries=2, default_retry_delay=30, time_limit=1800, soft_time_limit=1500
)
def execute_broadcast_record(self, broadcast_id: int):
    logger.info("execute_broadcast_record", broadcast_id=broadcast_id)
    try:
        result = _run_async(_execute_broadcast_record_async(broadcast_id))
        logger.info("execute_broadcast_record_completed", broadcast_id=broadcast_id, result=result)
        return result
    except Exception as exc:
        logger.error("execute_broadcast_record_failed", broadcast_id=broadcast_id, error=str(exc))
        get_alert_manager().send_task_alert(
            task_name="execute_broadcast_record",
            error=str(exc),
            severity=AlertSeverity.ERROR,
            details=f"Broadcast ID: {broadcast_id}",
        )
        raise self.retry(exc=exc)


@celery_app.task
def process_pending_campaigns():
    logger.info("process_pending_campaigns", task="process_pending_campaigns")
    try:
        result = _run_async(_process_pending_campaigns_async())
        logger.info("process_pending_campaigns_completed", processed=result.get("processed", 0))
        return result
    except Exception as exc:
        logger.error("process_pending_campaigns_failed", error=str(exc))
        return {"error": str(exc)}


@celery_app.task
def replenish_keywords_task(
    min_per_type: Optional[dict[str, int]] = None,
    generate_counts: Optional[dict[str, int]] = None,
    auto_approve: bool = False,
):
    logger.info("replenish_keywords_task", task="replenish_keywords_task")
    try:
        from app.modules.acquisition.automation import run_keyword_replenishment_with_db

        result = _run_async(
            run_keyword_replenishment_with_db(
                min_per_type=min_per_type,
                generate_counts=generate_counts,
                auto_approve=auto_approve,
            )
        )
        logger.info("replenish_keywords_completed", created=result.get("created", 0))
        return result
    except Exception as exc:
        logger.error("replenish_keywords_failed", error=str(exc))
        return {"error": str(exc)}


@celery_app.task
def auto_join_groups_task(
    max_accounts: int = 10,
    keywords_per_account: int = 10,
    max_groups_per_keyword: int = 20,
    dry_run: bool = False,
    scheduled: bool = False,
):
    logger.info("auto_join_groups_task", task="auto_join_groups_task", scheduled=scheduled)
    reservation = _run_async(
        _reserve_scheduled_auto_join() if scheduled else _reserve_manual_auto_join()
    )
    if not reservation.get("should_run"):
        logger.info(
            "auto_join_groups_execution_skipped",
            reason=reservation.get("reason"),
            config=reservation.get("config"),
            next_run_after=reservation.get("next_run_after"),
            scheduled=scheduled,
        )
        extra_key = "scheduler" if scheduled else "execution_lock"
        return _skipped_result(
            "auto_join_groups_task",
            reservation.get("reason", "auto_join_skipped"),
            **{extra_key: reservation},
        )

    try:
        from app.modules.acquisition.automation import run_auto_join_with_db

        result = _run_async(
            run_auto_join_with_db(
                max_accounts=max_accounts,
                keywords_per_account=keywords_per_account,
                max_groups_per_keyword=max_groups_per_keyword,
                dry_run=dry_run,
            )
        )
        if scheduled:
            result["scheduler"] = reservation
        else:
            result["execution_lock"] = reservation
        logger.info(
            "auto_join_groups_completed",
            succeeded=result.get("succeeded", 0),
            failed=result.get("failed", 0),
        )
        return result
    except Exception as exc:
        logger.error("auto_join_groups_failed", error=str(exc))
        return {"error": str(exc)}
    finally:
        if reservation and reservation.get("should_run"):
            _run_async(_finish_auto_join_execution())


@celery_app.task
def recover_orphaned_groups_task(
    max_tasks: int = 20,
    dry_run: bool = False,
    target_account_ids: list[int] | None = None,
):
    logger.info("recover_orphaned_groups_task", task="recover_orphaned_groups_task")
    try:
        from app.modules.acquisition.failover import run_group_failover_with_db

        result = _run_async(
            run_group_failover_with_db(
                max_tasks=max_tasks,
                dry_run=dry_run,
                target_account_ids=target_account_ids,
            )
        )
        logger.info(
            "recover_orphaned_groups_completed",
            created=result.get("created", 0),
            succeeded=result.get("succeeded", 0),
            failed=result.get("failed", 0),
        )
        return result
    except Exception as exc:
        logger.error("recover_orphaned_groups_failed", error=str(exc))
        return {"error": str(exc)}


@celery_app.task
def group_ai_warmup_task(max_groups: Optional[int] = None, dry_run: bool = False):
    logger.info("group_ai_warmup_task", task="group_ai_warmup_task")
    reservation = _run_async(_reserve_group_ai_warmup_execution())
    if not reservation.get("should_run"):
        logger.info("group_ai_warmup_execution_skipped", reason=reservation.get("reason"))
        return _skipped_result(
            "group_ai_warmup_task",
            reservation.get("reason", "group_ai_warmup_skipped"),
            execution_lock=reservation,
        )

    try:
        from app.modules.acquisition.automation import run_group_ai_warmup_with_db

        config = reservation.get("config") or {}
        resolved_max_groups = max_groups or int(config.get("proactiveWarmupMaxGroupsPerRun") or 5)
        result = _run_async(
            run_group_ai_warmup_with_db(max_groups=resolved_max_groups, dry_run=dry_run)
        )
        result["execution_lock"] = reservation
        logger.info(
            "group_ai_warmup_completed",
            succeeded=result.get("succeeded", 0),
            failed=result.get("failed", 0),
            skipped=result.get("skipped", 0),
        )
        return result
    except Exception as exc:
        logger.error("group_ai_warmup_failed", error=str(exc))
        return {"error": str(exc)}
    finally:
        if reservation and reservation.get("should_run"):
            _run_async(_finish_group_ai_warmup_execution())


@celery_app.task
def deliver_ads_task(max_deliveries: Optional[int] = None, dry_run: bool = False):
    logger.info("deliver_ads_task", task="deliver_ads_task")
    reservation = _run_async(_reserve_ad_delivery_execution())
    if not reservation.get("should_run"):
        logger.info(
            "deliver_ads_execution_skipped",
            reason=reservation.get("reason"),
            lock_started_at=reservation.get("lock_started_at"),
            lock_age_seconds=reservation.get("lock_age_seconds"),
        )
        return _skipped_result(
            "deliver_ads_task",
            reservation.get("reason", "ad_delivery_skipped"),
            execution_lock=reservation,
        )

    try:
        from app.modules.acquisition.automation import run_ad_delivery_with_db

        execution = reservation.get("execution") or {}
        resolved_max_deliveries = max_deliveries or int(
            execution.get("max_deliveries_per_run") or 20
        )
        result = _run_async(
            run_ad_delivery_with_db(max_deliveries=resolved_max_deliveries, dry_run=dry_run)
        )
        result["execution_lock"] = reservation
        logger.info(
            "deliver_ads_completed",
            succeeded=result.get("succeeded", 0),
            failed=result.get("failed", 0),
        )
        return result
    except Exception as exc:
        logger.error("deliver_ads_failed", error=str(exc))
        return {"error": str(exc)}
    finally:
        if reservation and reservation.get("should_run"):
            _run_async(_finish_ad_delivery_execution())


@celery_app.task
def check_ad_survival_task(limit: Optional[int] = None):
    logger.info("check_ad_survival_task", task="check_ad_survival_task")
    try:
        from app.modules.acquisition.automation import run_ad_survival_check_with_db

        result = _run_async(run_ad_survival_check_with_db(limit=limit))
        logger.info(
            "check_ad_survival_completed",
            processed=result.get("processed", 0),
            survived=result.get("survived", 0),
            deleted=result.get("deleted", 0),
            check_failed=result.get("check_failed", 0),
        )
        return result
    except Exception as exc:
        logger.error("check_ad_survival_failed", error=str(exc))
        return {"error": str(exc)}


@celery_app.task
def audit_group_ad_policies_task(limit: int = 5, dry_run: bool = False):
    logger.info("audit_group_ad_policies_task", task="audit_group_ad_policies_task", limit=limit)
    try:
        from app.modules.acquisition.automation import run_group_ad_policy_audit_with_db

        result = _run_async(run_group_ad_policy_audit_with_db(limit=limit, dry_run=dry_run))
        logger.info(
            "audit_group_ad_policies_completed",
            processed=result.get("processed", 0),
            updated=result.get("updated", 0),
            failed=result.get("failed", 0),
        )
        return result
    except Exception as exc:
        logger.error("audit_group_ad_policies_failed", error=str(exc))
        return {"error": str(exc)}


# =============================================================================
# Second-level Precision Tasks
# =============================================================================


@celery_app.task(bind=True, max_retries=5, default_retry_delay=10)
def campaign_check_task(self):
    logger.debug("campaign_check_task", task="campaign_check_task")
    try:
        return _run_async(_process_pending_campaigns_async())
    except Exception as exc:
        logger.error("campaign_check_task_failed", error=str(exc))
        get_alert_manager().send_task_alert(
            task_name="campaign_check_task",
            error=str(exc),
            severity=AlertSeverity.WARNING,
        )
        raise self.retry(exc=exc)


# =============================================================================
# Async Tasks (Triggered by API or other modules)
# =============================================================================


@celery_app.task(
    bind=True, max_retries=3, default_retry_delay=60, time_limit=1800, soft_time_limit=1500
)
def send_bulk_messages(self, targets: list, message: str, account_id: Optional[int] = None):
    logger.info("send_bulk_messages", target_count=len(targets), account_id=account_id)
    try:
        results = _run_async(_send_bulk_messages_async(targets, message))
        logger.info(
            "send_bulk_messages_completed", success=results["success"], failed=results["failed"]
        )
        return results
    except Exception as exc:
        logger.error("send_bulk_messages_failed", error=str(exc))
        get_alert_manager().send_task_alert(
            task_name="send_bulk_messages",
            error=str(exc),
            severity=AlertSeverity.ERROR,
            details=f"Failed to send to {len(targets)} targets",
        )
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=2, time_limit=3600)
def import_accounts_batch(self, accounts_data: list):
    logger.info("import_accounts_batch", count=len(accounts_data))
    try:
        results = _run_async(_import_accounts_batch_async(accounts_data))
        logger.info(
            "import_accounts_batch_completed",
            imported=results["imported"],
            failed=results["failed"],
        )
        return results
    except Exception as exc:
        logger.error("import_accounts_batch_failed", error=str(exc))
        get_alert_manager().send_task_alert(
            task_name="import_accounts_batch",
            error=str(exc),
            severity=AlertSeverity.ERROR,
        )
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def process_user_registration(self, user_id: int, campaign_id: Optional[int] = None):
    logger.info("process_user_registration", user_id=user_id, campaign_id=campaign_id)
    try:
        result = _run_async(_process_user_registration_async(user_id, campaign_id))
        logger.info("process_user_registration_completed", user_id=user_id, result=result)
        return result
    except Exception as exc:
        logger.error("process_user_registration_failed", user_id=user_id, error=str(exc))
        get_alert_manager().send_task_alert(
            task_name="process_user_registration",
            error=str(exc),
            severity=AlertSeverity.WARNING,
            details=f"Failed for user_id={user_id}",
        )
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=3, time_limit=1800)
def execute_campaign_rewards(self, campaign_id: int, user_id: Optional[int] = None):
    logger.info("execute_campaign_rewards", campaign_id=campaign_id, user_id=user_id)
    try:
        result = _run_async(_execute_campaign_rewards_async(campaign_id, user_id=user_id))
        logger.info("execute_campaign_rewards_completed", campaign_id=campaign_id, result=result)
        return result
    except Exception as exc:
        logger.error("execute_campaign_rewards_failed", campaign_id=campaign_id, error=str(exc))
        get_alert_manager().send_task_alert(
            task_name="execute_campaign_rewards",
            error=str(exc),
            severity=AlertSeverity.ERROR,
            details=f"Campaign ID: {campaign_id}",
        )
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=2, time_limit=3600)
def validate_proxy_batch(self, proxy_ids: list):
    logger.info("validate_proxy_batch", count=len(proxy_ids))
    try:
        result = _run_async(_validate_proxy_batch_async(proxy_ids))
        logger.info(
            "validate_proxy_batch_completed",
            total=result.get("total", 0),
            valid=result.get("valid", 0),
            invalid=result.get("invalid", 0),
        )
        return result
    except Exception as exc:
        logger.error("validate_proxy_batch_failed", error=str(exc))
        return {"error": str(exc)}


@celery_app.task
def send_trial_reminder(user_id: int, hours_before_expiry: int):
    logger.info("send_trial_reminder", user_id=user_id, hours_before_expiry=hours_before_expiry)
    try:
        return _run_async(_send_trial_reminder_async(user_id, hours_before_expiry))
    except Exception as exc:
        logger.error("send_trial_reminder_failed", user_id=user_id, error=str(exc))
        return {"sent": False, "error": str(exc)}


# =============================================================================
# Task Utilities
# =============================================================================


@celery_app.task
def get_task_status(task_id: str):
    from celery.result import AsyncResult

    result = AsyncResult(task_id)
    return {
        "task_id": task_id,
        "status": result.state,
        "result": result.result if result.ready() else None,
    }


@celery_app.task
def cleanup_completed_tasks():
    logger.info("cleanup_completed_tasks", task="cleanup_completed_tasks")
    return {"status": "ok"}
