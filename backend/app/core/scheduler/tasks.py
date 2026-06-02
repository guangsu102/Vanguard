"""
Scheduled Tasks

Celery scheduled tasks with pragmatic production-safe fallbacks.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.celery import celery_app
from app.core.scheduler.alerts import AlertSeverity, TaskAlertManager


logger = structlog.get_logger()


def get_alert_manager() -> TaskAlertManager:
    """Get singleton alert manager instance."""
    return TaskAlertManager()


async def _ensure_db_initialized():
    from app.core import database as db_module

    if db_module.async_session_factory is None:
        await db_module.init_db(create_tables=False)
    return db_module


async def _run_with_db(handler: Callable[[AsyncSession], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    db_module = await _ensure_db_initialized()
    async with db_module.get_db_session() as db:
        return await handler(db)


def _skipped_result(task: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {"status": "skipped", "task": task, "reason": reason, **extra}


async def _health_check_accounts_async() -> dict[str, Any]:
    from app.core.account.manager import AccountManager
    from app.core.account.models import AccountStatus
    from app.core.account.pool import AccountPool

    async def handler(db: AsyncSession) -> dict[str, Any]:
        manager = AccountManager(db)
        accounts = await manager.list_accounts(limit=10_000)
        runtime_accounts = [
            account for account in accounts if account.phone and account.api_config is not None
        ]
        pool = AccountPool()
        await pool.sync_from_db(runtime_accounts)
        summary = await pool.health_check()
        await pool.close_all()
        unhealthy = sum(
            1 for account in runtime_accounts if account.status in {AccountStatus.ERROR, AccountStatus.BANNED}
        )
        return {
            "checked": len(runtime_accounts),
            "healthy": max(len(runtime_accounts) - unhealthy, 0),
            "unhealthy": unhealthy,
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
    return _skipped_result("broadcast_node_status", "node_status_broadcast_integration_not_implemented")


async def _execute_broadcast_record_async(broadcast_id: int) -> dict[str, Any]:
    from app.api.broadcasts import BroadcastRecord
    from app.core.config import settings
    from app.integrations.telegram.client import TelegramClient, TelegramConfig

    async def handler(db: AsyncSession) -> dict[str, Any]:
        result = await db.execute(select(BroadcastRecord).where(BroadcastRecord.id == broadcast_id))
        broadcast = result.scalar_one_or_none()
        if not broadcast:
            return _skipped_result("execute_broadcast_record", "broadcast_not_found", broadcast_id=broadcast_id)

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

        client = TelegramClient(TelegramConfig(bot_token=settings.BOT_TOKEN))
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
    from app.modules.guardian.campaign_runner import run_managed_group_campaigns_with_db

    return await run_managed_group_campaigns_with_db(now=datetime.utcnow())


async def _send_bulk_messages_async(targets: list, message: str) -> dict[str, Any]:
    from app.core.config import settings
    from app.integrations.telegram.client import TelegramClient, TelegramConfig

    if not settings.BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not configured")

    client = TelegramClient(TelegramConfig(bot_token=settings.BOT_TOKEN))
    results = {"success": 0, "failed": 0, "failed_targets": []}

    try:
        for index, target in enumerate(targets):
            try:
                await client.send_message(target, message)
                results["success"] += 1
                if (index + 1) % 100 == 0:
                    logger.info("send_bulk_messages_progress", processed=index + 1, total=len(targets))
            except Exception as exc:
                logger.warning("send_bulk_message_failed", target=target, error=str(exc))
                results["failed"] += 1
                results["failed_targets"].append(target)
    finally:
        await client.close()

    return results


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


async def _process_user_registration_async(user_id: int, campaign_id: Optional[int]) -> dict[str, Any]:
    from app.core.campaign.engine import CampaignEngine
    from app.core.user.models import User

    async def handler(db: AsyncSession) -> dict[str, Any]:
        user = await db.get(User, user_id)
        if user is None:
            return {"status": "error", "message": "User not found", "user_id": user_id}

        if not campaign_id:
            return {"status": "success", "user_id": user_id, "reward": False}

        engine = CampaignEngine(db)
        campaign = await engine.get_campaign(campaign_id)
        if campaign is None:
            return {"status": "error", "message": "Campaign not found", "campaign_id": campaign_id}

        tracking = await engine.trigger(
            user,
            campaign_name=campaign.name,
            tracking_data={"source": "scheduler:user_registration"},
        )
        if tracking is not None:
            await engine.record_registration(tracking.id)

        return {
            "status": "success",
            "user_id": user_id,
            "campaign_id": campaign_id,
            "reward": tracking is not None,
            "tracking_id": tracking.id if tracking is not None else None,
        }

    return await _run_with_db(handler)


async def _execute_campaign_rewards_async(campaign_id: int) -> dict[str, Any]:
    from app.core.campaign.engine import CampaignEngine
    from app.core.campaign.models import CampaignScope
    from app.modules.guardian.campaign_runner import ManagedGroupCampaignRunner
    from app.modules.guardian.models import GroupCampaignTriggerEvent

    async def handler(db: AsyncSession) -> dict[str, Any]:
        engine = CampaignEngine(db)
        campaign = await engine.get_campaign(campaign_id)
        if campaign is None:
            return {"status": "error", "message": "Campaign not found", "campaign_id": campaign_id}

        if (
            campaign.campaign_scope == CampaignScope.MANAGED_GROUP
            and campaign.trigger_event == GroupCampaignTriggerEvent.MANUAL_BROADCAST.value
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

        return _skipped_result(
            "execute_campaign_rewards",
            "bulk_reward_distribution_not_implemented",
            campaign_id=campaign_id,
        )

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
        client = TelegramClient(TelegramConfig(bot_token=settings.BOT_TOKEN))
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
        result = asyncio.run(_health_check_accounts_async())
        logger.info(
            "health_check_accounts_completed",
            checked=result.get("checked", 0),
            healthy=result.get("healthy", 0),
            unhealthy=result.get("unhealthy", 0),
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
        result = asyncio.run(_health_check_proxies_async())
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
        return asyncio.run(_group_snapshot_async("sync_group_metrics"))
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
        return asyncio.run(_check_proxy_status_async())
    except Exception as exc:
        logger.error("check_proxy_status_failed", error=str(exc))
        return {"error": str(exc)}


@celery_app.task
def cleanup_expired_tokens():
    logger.info("cleanup_expired_tokens", task="cleanup_expired_tokens")
    try:
        return asyncio.run(_cleanup_expired_tokens_async())
    except Exception as exc:
        logger.error("cleanup_expired_tokens_failed", error=str(exc))
        return {"error": str(exc)}


@celery_app.task
def cleanup_old_messages():
    logger.info("cleanup_old_messages", task="cleanup_old_messages")
    try:
        return asyncio.run(_cleanup_old_messages_async())
    except Exception as exc:
        logger.error("cleanup_old_messages_failed", error=str(exc))
        return {"error": str(exc)}


@celery_app.task
def sync_group_info():
    logger.info("sync_group_info", task="sync_group_info")
    try:
        return asyncio.run(_group_snapshot_async("sync_group_info"))
    except Exception as exc:
        logger.error("sync_group_info_failed", error=str(exc))
        return {"error": str(exc)}


@celery_app.task
def check_user_states():
    logger.info("check_user_states", task="check_user_states")
    try:
        result = asyncio.run(_check_user_states_async())
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
        result = asyncio.run(_generate_daily_report_async())
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
        return asyncio.run(_broadcast_node_status_async())
    except Exception as exc:
        logger.error("broadcast_node_status_failed", error=str(exc))
        return {"error": str(exc)}


@celery_app.task(bind=True, max_retries=2, default_retry_delay=30, time_limit=1800, soft_time_limit=1500)
def execute_broadcast_record(self, broadcast_id: int):
    logger.info("execute_broadcast_record", broadcast_id=broadcast_id)
    try:
        result = asyncio.run(_execute_broadcast_record_async(broadcast_id))
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
        result = asyncio.run(_process_pending_campaigns_async())
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

        result = asyncio.run(
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
    keywords_per_account: int = 5,
    max_groups_per_keyword: int = 10,
    dry_run: bool = False,
):
    logger.info("auto_join_groups_task", task="auto_join_groups_task")
    try:
        from app.modules.acquisition.automation import run_auto_join_with_db

        result = asyncio.run(
            run_auto_join_with_db(
                max_accounts=max_accounts,
                keywords_per_account=keywords_per_account,
                max_groups_per_keyword=max_groups_per_keyword,
                dry_run=dry_run,
            )
        )
        logger.info(
            "auto_join_groups_completed",
            succeeded=result.get("succeeded", 0),
            failed=result.get("failed", 0),
        )
        return result
    except Exception as exc:
        logger.error("auto_join_groups_failed", error=str(exc))
        return {"error": str(exc)}


@celery_app.task
def deliver_ads_task(max_deliveries: int = 20, dry_run: bool = False):
    logger.info("deliver_ads_task", task="deliver_ads_task")
    try:
        from app.modules.acquisition.automation import run_ad_delivery_with_db

        result = asyncio.run(run_ad_delivery_with_db(max_deliveries=max_deliveries, dry_run=dry_run))
        logger.info(
            "deliver_ads_completed",
            succeeded=result.get("succeeded", 0),
            failed=result.get("failed", 0),
        )
        return result
    except Exception as exc:
        logger.error("deliver_ads_failed", error=str(exc))
        return {"error": str(exc)}


# =============================================================================
# Second-level Precision Tasks
# =============================================================================


@celery_app.task(bind=True, max_retries=5, default_retry_delay=10)
def campaign_check_task(self):
    logger.debug("campaign_check_task", task="campaign_check_task")
    try:
        return asyncio.run(_process_pending_campaigns_async())
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


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60, time_limit=1800, soft_time_limit=1500)
def send_bulk_messages(self, targets: list, message: str, account_id: Optional[int] = None):
    logger.info("send_bulk_messages", target_count=len(targets), account_id=account_id)
    try:
        results = asyncio.run(_send_bulk_messages_async(targets, message))
        logger.info("send_bulk_messages_completed", success=results["success"], failed=results["failed"])
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
        results = asyncio.run(_import_accounts_batch_async(accounts_data))
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
        result = asyncio.run(_process_user_registration_async(user_id, campaign_id))
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
def execute_campaign_rewards(self, campaign_id: int):
    logger.info("execute_campaign_rewards", campaign_id=campaign_id)
    try:
        result = asyncio.run(_execute_campaign_rewards_async(campaign_id))
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
        result = asyncio.run(_validate_proxy_batch_async(proxy_ids))
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
        return asyncio.run(_send_trial_reminder_async(user_id, hours_before_expiry))
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
