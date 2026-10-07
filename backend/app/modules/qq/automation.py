"""Durable QQ join and advertising workflow with account/group serialization."""

from __future__ import annotations

import asyncio
import json
import random
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.qq.client import OneBotAPIError, OneBotClient
from app.integrations.qq.join_client import QQJoinClient
from app.modules.acquisition.models import AdCreative
from app.modules.qq.models import (
    QQAdBinding,
    QQAdCampaign,
    QQAdSchedule,
    QQAutomationLog,
    QQBotConnection,
    QQCampaignTarget,
    QQJoinTask,
    QQManagedGroup,
)
from app.modules.qq.runtime import client_for_connection, join_client_for_connection
from app.modules.qq.service import QQEventProcessor

RELEASE_LOCK = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def day_bounds(now: datetime, timezone: str) -> tuple[datetime, datetime]:
    local = now.replace(tzinfo=UTC).astimezone(ZoneInfo(timezone))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        start.astimezone(UTC).replace(tzinfo=None),
        (start + timedelta(days=1)).astimezone(UTC).replace(tzinfo=None),
    )


def next_scheduled_time(campaign: QQAdCampaign, earliest: datetime) -> datetime:
    local = earliest.replace(tzinfo=UTC).astimezone(ZoneInfo(campaign.timezone))
    times = json.loads(campaign.scheduled_times_json)
    if not times:
        raise ValueError("Scheduled QQ campaign has no sending times")
    candidates = []
    for value in times:
        hour, minute = map(int, value.split(":"))
        candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate < local:
            candidate += timedelta(days=1)
        candidates.append(candidate.astimezone(UTC).replace(tzinfo=None))
    return min(candidates)


def initial_due(campaign: QQAdCampaign, joined_at: datetime, now: datetime) -> datetime:
    earliest = max(
        now,
        joined_at + timedelta(minutes=campaign.min_wait_after_join_minutes),
        campaign.start_at or now,
    )
    return (
        next_scheduled_time(campaign, earliest) if campaign.send_mode == "scheduled" else earliest
    )


def render_creative(creative: AdCreative) -> list[dict[str, Any]]:
    content = (creative.content or "").strip()
    if creative.link_url and creative.link_url not in content:
        content += f"\n{creative.link_url}"
    segments: list[dict[str, Any]] = []
    if creative.creative_type in {"image", "mixed"}:
        media = creative.media_url or ""
        if urlsplit(media).scheme not in {"https", "http"} or not urlsplit(media).hostname:
            raise ValueError("QQ 图片素材需要 HTTP/HTTPS 图片地址，不能使用 Telegram file_id")
        segments.append({"type": "image", "data": {"file": media}})
    elif creative.creative_type != "text":
        raise ValueError("QQ 暂不支持此素材类型")
    if content:
        segments.append({"type": "text", "data": {"text": content}})
    if not segments:
        raise ValueError("广告素材为空")
    return segments


@asynccontextmanager
async def operation_lock(redis: Redis, key: str) -> AsyncIterator[bool]:
    token = uuid.uuid4().hex
    acquired = bool(await redis.set(key, token, nx=True, ex=120))
    try:
        yield acquired
    finally:
        if acquired:
            await redis.eval(RELEASE_LOCK, 1, key, token)


class QQAutomationService:
    def __init__(
        self,
        db: AsyncSession,
        redis: Redis,
        *,
        client_factory: Callable[[QQBotConnection], OneBotClient] = client_for_connection,
        join_factory: Callable[[QQBotConnection], QQJoinClient | None] = join_client_for_connection,
    ) -> None:
        self.db = db
        self.redis = redis
        self.client_factory = client_factory
        self.join_factory = join_factory

    async def tick(self, now: datetime | None = None) -> dict[str, int]:
        fixed_now = now
        summary = {"accounts": 0, "joins": 0, "ads": 0, "errors": 0}
        account_ids = list(
            await self.db.scalars(
                select(QQBotConnection.id)
                .where(
                    QQBotConnection.enabled.is_(True), QQBotConnection.automation_enabled.is_(True)
                )
                .order_by(QQBotConnection.id)
            )
        )
        await self.db.commit()
        for account_id in account_ids:
            now = fixed_now or utcnow()
            async with operation_lock(self.redis, f"vanguard:qq:account:{account_id}") as acquired:
                if not acquired:
                    continue
                account = await self.db.get(QQBotConnection, account_id, populate_existing=True)
                if account is None or not account.enabled or not account.automation_enabled:
                    await self.db.commit()
                    continue
                await self.db.commit()
                client = None
                try:
                    # Finish or cancel before the 120-second account lease expires.
                    async with asyncio.timeout(90):
                        client = self.client_factory(account)
                        await self.recover_interrupted(account, now)
                        await self.sync_membership(account, client, now)
                        campaigns = await self.prepare_work(account, now)
                        summary["accounts"] += 1
                        if await self.deliver_one(account, client, campaigns, now):
                            summary["ads"] += 1
                        if await self.join_one(account, campaigns, now):
                            summary["joins"] += 1
                except Exception as exc:
                    await self.db.rollback()
                    account = await self.db.get(QQBotConnection, account_id)
                    if account is not None:
                        account.status = "error"
                        account.last_error = str(exc)[:2000]
                        await self.db.commit()
                    summary["errors"] += 1
                finally:
                    if client is not None:
                        await client.close()
        return summary

    async def recover_interrupted(self, account: QQBotConnection, now: datetime) -> None:
        rows = list(
            await self.db.scalars(
                select(QQAutomationLog).where(
                    QQAutomationLog.connection_id == account.id,
                    QQAutomationLog.status == "sending",
                    QQAutomationLog.created_at < now - timedelta(minutes=2),
                )
            )
        )
        for log in rows:
            log.status = "unknown"
            log.error_message = "执行进程中断，需核对远端结果"
            log.completed_at = now
            payload = json.loads(log.payload_json)
            if log.operation_type == "ad":
                schedule = await self.db.get(QQAdSchedule, payload.get("schedule_id"))
                if schedule:
                    schedule.status = "paused"
                    schedule.error_message = log.error_message
            else:
                task = await self.db.get(QQJoinTask, payload.get("join_task_id"))
                if task:
                    task.status = "unknown"
                    task.error_message = log.error_message
        await self.db.commit()

    async def sync_membership(
        self, account: QQBotConnection, client: OneBotClient, now: datetime
    ) -> None:
        login = await client.get_login_info()
        if str(login.get("user_id") or "") != account.app_id:
            raise OneBotAPIError("NapCat 登录账号与所选 QQ 账号不一致")
        groups = await client.get_group_list()
        members = await QQEventProcessor(self.db).sync_groups(account, groups, verified_at=now)
        for group in members:
            group.membership_verified_at = now
        member_numbers = {group.group_openid for group in members}
        tasks = await self.db.scalars(
            select(QQJoinTask).where(QQJoinTask.connection_id == account.id)
        )
        for task in tasks:
            if task.group_number in member_numbers:
                task.status = "joined"
                task.joined_at = task.joined_at or now
                task.error_message = None
        account.display_name = str(login.get("nickname") or "") or account.display_name
        account.status = "online"
        account.last_error = None
        account.last_heartbeat_at = now
        await self.db.commit()

    async def prepare_work(
        self, account: QQBotConnection, now: datetime
    ) -> dict[int, QQAdCampaign]:
        campaigns = {
            c.id: c
            for c in await self.db.scalars(
                select(QQAdCampaign)
                .join(QQAdBinding, QQAdBinding.campaign_id == QQAdCampaign.id)
                .join(AdCreative, QQAdBinding.creative_id == AdCreative.id)
                .where(
                    QQAdBinding.connection_id == account.id,
                    QQAdBinding.enabled.is_(True),
                    AdCreative.enabled.is_(True),
                    QQAdBinding.priority > 0,
                    AdCreative.weight > 0,
                    QQAdCampaign.enabled.is_(True),
                )
                .distinct()
            )
            if (c.start_at is None or c.start_at <= now) and (c.end_at is None or c.end_at > now)
        }
        members = {
            g.group_openid: g
            for g in await self.db.scalars(
                select(QQManagedGroup).where(
                    QQManagedGroup.connection_id == account.id,
                    QQManagedGroup.membership_status == "member",
                )
            )
        }
        targets = list(
            await self.db.scalars(
                select(QQCampaignTarget).where(
                    QQCampaignTarget.campaign_id.in_(list(campaigns)),
                    QQCampaignTarget.enabled.is_(True),
                )
            )
        )
        for target in targets:
            campaign = campaigns[target.campaign_id]
            member = members.get(target.group_number)
            if member is None:
                if not campaign.auto_join_enabled:
                    continue
                task = await self.db.scalar(
                    select(QQJoinTask).where(
                        QQJoinTask.connection_id == account.id,
                        QQJoinTask.group_number == target.group_number,
                    )
                )
                if task is None:
                    self.db.add(
                        QQJoinTask(
                            connection_id=account.id,
                            group_number=target.group_number,
                            verify_message=target.verify_message,
                        )
                    )
                    await self.db.flush()
                elif task.status == "queued":
                    task.verify_message = target.verify_message
                continue
            if member.status != "active":
                continue
            schedule = await self.db.scalar(
                select(QQAdSchedule).where(
                    QQAdSchedule.connection_id == account.id,
                    QQAdSchedule.campaign_id == campaign.id,
                    QQAdSchedule.group_number == target.group_number,
                )
            )
            if schedule is None:
                self.db.add(
                    QQAdSchedule(
                        connection_id=account.id,
                        campaign_id=campaign.id,
                        group_number=target.group_number,
                        next_due_at=initial_due(campaign, member.bot_added_at or now, now),
                    )
                )
        await self.db.commit()
        return campaigns

    async def count_attempts(
        self, operation_type: str, now: datetime, timezone: str = "Asia/Shanghai", **filters: Any
    ) -> int:
        start, end = day_bounds(now, timezone)
        query = select(func.count(QQAutomationLog.id)).where(
            QQAutomationLog.operation_type == operation_type,
            QQAutomationLog.created_at >= start,
            QQAutomationLog.created_at < end,
        )
        for name, value in filters.items():
            query = query.where(getattr(QQAutomationLog, name) == value)
        return int(await self.db.scalar(query) or 0)

    async def join_one(
        self, account: QQBotConnection, campaigns: dict[int, QQAdCampaign], now: datetime
    ) -> bool:
        if account.next_join_at and account.next_join_at > now:
            return False
        if (
            await self.count_attempts("join", now, connection_id=account.id)
            >= account.max_joins_per_day
        ):
            return False
        numbers = set(
            await self.db.scalars(
                select(QQCampaignTarget.group_number).where(
                    QQCampaignTarget.campaign_id.in_(
                        [c.id for c in campaigns.values() if c.auto_join_enabled]
                    ),
                    QQCampaignTarget.enabled.is_(True),
                )
            )
        )
        task = await self.db.scalar(
            select(QQJoinTask)
            .where(
                QQJoinTask.connection_id == account.id,
                QQJoinTask.status == "queued",
                QQJoinTask.group_number.in_(numbers),
            )
            .order_by(QQJoinTask.id)
            .limit(1)
        )
        if task is None:
            return False
        bridge = self.join_factory(account)
        if bridge is None:
            task.status = "unsupported"
            task.error_message = "当前执行端未配置主动申请加群接口；NapCat 标准 OneBot 不提供此能力"
            await self.db.commit()
            return False
        try:
            await self.db.refresh(account)
            await self.db.refresh(task)
            if not account.enabled or not account.automation_enabled:
                return False
            if task.status != "queued":
                return False
            current_campaign = await self.db.scalar(
                select(QQAdCampaign)
                .join(QQCampaignTarget, QQCampaignTarget.campaign_id == QQAdCampaign.id)
                .join(QQAdBinding, QQAdBinding.campaign_id == QQAdCampaign.id)
                .join(AdCreative, AdCreative.id == QQAdBinding.creative_id)
                .where(
                    QQAdCampaign.id.in_(list(campaigns)),
                    QQAdCampaign.enabled.is_(True),
                    QQAdCampaign.auto_join_enabled.is_(True),
                    QQCampaignTarget.enabled.is_(True),
                    QQCampaignTarget.group_number == task.group_number,
                    QQAdBinding.connection_id == account.id,
                    QQAdBinding.enabled.is_(True),
                    AdCreative.enabled.is_(True),
                )
                .limit(1)
            )
            if current_campaign is None:
                return False
            task.attempt_count += 1
            task.status = "requesting"
            task.attempted_at = now
            log = QQAutomationLog(
                connection_id=account.id,
                group_number=task.group_number,
                operation_type="join",
                operation_key=f"join:{task.id}:{task.attempt_count}",
                payload_json=json.dumps(
                    {"join_task_id": task.id, "verify_message": task.verify_message}
                ),
                created_at=now,
            )
            self.db.add(log)
            account.next_join_at = now + timedelta(seconds=account.join_interval_seconds)
            await self.db.commit()
            try:
                result = await bridge.request_join(
                    account_id=account.app_id,
                    group_number=task.group_number,
                    verify_message=task.verify_message,
                    operation_id=log.id,
                )
                # A bridge's 'joined' response is not membership evidence.
                task.status = (
                    "pending_approval" if result["status"] == "joined" else result["status"]
                )
                task.error_message = str(result.get("message") or "")[:2000] or None
                log.status = "succeeded" if task.status == "pending_approval" else "failed"
                log.error_message = task.error_message
            except OneBotAPIError as exc:
                task.status = log.status = "unknown" if exc.uncertain else "failed"
                task.error_message = log.error_message = exc.message[:2000]
            except Exception:
                task.status = log.status = "unknown"
                task.error_message = log.error_message = "加群执行结果未知，需核对"
            log.completed_at = now
            await self.db.commit()
            return True
        finally:
            await bridge.close()

    async def deliver_one(
        self,
        account: QQBotConnection,
        client: OneBotClient,
        campaigns: dict[int, QQAdCampaign],
        now: datetime,
    ) -> bool:
        if account.next_send_at and account.next_send_at > now:
            return False
        if (
            await self.count_attempts("ad", now, connection_id=account.id)
            >= account.max_sends_per_day
        ):
            return False
        schedules = list(
            await self.db.scalars(
                select(QQAdSchedule)
                .join(
                    QQCampaignTarget,
                    (QQCampaignTarget.campaign_id == QQAdSchedule.campaign_id)
                    & (QQCampaignTarget.group_number == QQAdSchedule.group_number),
                )
                .join(
                    QQManagedGroup,
                    (QQManagedGroup.connection_id == QQAdSchedule.connection_id)
                    & (QQManagedGroup.group_openid == QQAdSchedule.group_number),
                )
                .where(
                    QQAdSchedule.connection_id == account.id,
                    QQAdSchedule.campaign_id.in_(list(campaigns)),
                    QQAdSchedule.status == "active",
                    QQAdSchedule.next_due_at <= now,
                    QQCampaignTarget.enabled.is_(True),
                    QQManagedGroup.status == "active",
                    QQManagedGroup.membership_status == "member",
                )
                .order_by(QQAdSchedule.next_due_at, QQAdSchedule.id)
                .limit(5)
            )
        )
        for schedule in schedules:
            async with operation_lock(
                self.redis, f"vanguard:qq:group:{schedule.group_number}"
            ) as acquired:
                if acquired and await self.deliver_schedule(account, client, schedule, now):
                    return True
        return False

    async def deliver_schedule(
        self, account: QQBotConnection, client: OneBotClient, schedule: QQAdSchedule, now: datetime
    ) -> bool:
        await self.db.refresh(account)
        await self.db.refresh(schedule)
        campaign = await self.db.get(QQAdCampaign, schedule.campaign_id, populate_existing=True)
        if (
            not account.enabled
            or not account.automation_enabled
            or not campaign
            or not campaign.enabled
            or schedule.status != "active"
            or (campaign.end_at and campaign.end_at <= now)
            or (campaign.start_at and campaign.start_at > now)
        ):
            return False
        target = await self.db.scalar(
            select(QQCampaignTarget).where(
                QQCampaignTarget.campaign_id == campaign.id,
                QQCampaignTarget.group_number == schedule.group_number,
                QQCampaignTarget.enabled.is_(True),
            )
        )
        member = await self.db.scalar(
            select(QQManagedGroup).where(
                QQManagedGroup.connection_id == account.id,
                QQManagedGroup.group_openid == schedule.group_number,
                QQManagedGroup.membership_status == "member",
                QQManagedGroup.status == "active",
            )
        )
        if not target or not member:
            return False
        earliest = (member.bot_added_at or now) + timedelta(
            minutes=campaign.min_wait_after_join_minutes
        )
        if earliest > now:
            schedule.next_due_at = initial_due(campaign, member.bot_added_at or now, now)
            await self.db.commit()
            return False
        account_count = await self.count_attempts(
            "ad", now, campaign.timezone, connection_id=account.id, campaign_id=campaign.id
        )
        group_count = await self.count_attempts(
            "ad", now, campaign.timezone, group_number=schedule.group_number
        )
        if (
            account_count >= campaign.max_sends_per_account_per_day
            or group_count >= campaign.max_sends_per_group_per_day
        ):
            schedule.next_due_at = day_bounds(now, campaign.timezone)[1]
            if campaign.send_mode == "scheduled":
                schedule.next_due_at = next_scheduled_time(campaign, schedule.next_due_at)
            await self.db.commit()
            return False
        rows = (
            await self.db.execute(
                select(QQAdBinding, AdCreative)
                .join(AdCreative, QQAdBinding.creative_id == AdCreative.id)
                .where(
                    QQAdBinding.connection_id == account.id,
                    QQAdBinding.campaign_id == campaign.id,
                    QQAdBinding.enabled.is_(True),
                    AdCreative.enabled.is_(True),
                    QQAdBinding.priority > 0,
                    AdCreative.weight > 0,
                )
            )
        ).all()
        if not rows:
            return False
        binding, creative = random.choices(
            rows, weights=[b.priority * c.weight for b, c in rows], k=1
        )[0]
        try:
            segments = render_creative(creative)
        except ValueError as exc:
            schedule.status = "paused"
            schedule.error_message = str(exc)
            await self.db.commit()
            return False
        await self.db.commit()
        info = await client.get_group_member_info(schedule.group_number)
        if int(info.get("shut_up_timestamp") or 0) > int(now.replace(tzinfo=UTC).timestamp()):
            schedule.next_due_at = datetime.fromtimestamp(
                int(info["shut_up_timestamp"]), UTC
            ).replace(tzinfo=None)
            await self.db.commit()
            return False
        # Recheck mutable controls after the permission read and before the write.
        await self.db.refresh(account)
        await self.db.refresh(campaign)
        await self.db.refresh(binding)
        await self.db.refresh(creative)
        await self.db.refresh(target)
        await self.db.refresh(member)
        await self.db.refresh(schedule)
        if not (
            account.enabled
            and account.automation_enabled
            and campaign.enabled
            and binding.enabled
            and creative.enabled
            and target.enabled
            and member.status == "active"
            and member.membership_status == "member"
            and schedule.status == "active"
            and schedule.next_due_at
            and schedule.next_due_at <= now
        ):
            return False
        if account.next_send_at and account.next_send_at > now:
            return False
        if (campaign.start_at and campaign.start_at > now) or (
            campaign.end_at and campaign.end_at <= now
        ):
            return False
        if (
            await self.count_attempts("ad", now, connection_id=account.id)
            >= account.max_sends_per_day
            or await self.count_attempts(
                "ad", now, campaign.timezone, connection_id=account.id, campaign_id=campaign.id
            )
            >= campaign.max_sends_per_account_per_day
            or await self.count_attempts(
                "ad", now, campaign.timezone, group_number=schedule.group_number
            )
            >= campaign.max_sends_per_group_per_day
        ):
            return False
        segments = render_creative(creative)
        operation_key = f"ad:{schedule.id}:{schedule.next_due_at.isoformat()}"
        existing = await self.db.scalar(
            select(QQAutomationLog).where(QQAutomationLog.operation_key == operation_key)
        )
        if existing:
            return False
        log = QQAutomationLog(
            connection_id=account.id,
            campaign_id=campaign.id,
            creative_id=creative.id,
            group_number=schedule.group_number,
            operation_type="ad",
            operation_key=operation_key,
            payload_json=json.dumps(
                {"schedule_id": schedule.id, "segments": segments}, ensure_ascii=False
            ),
            created_at=now,
        )
        self.db.add(log)
        account.next_send_at = now + timedelta(seconds=account.send_interval_seconds)
        await self.db.commit()
        try:
            result = await client.send_group_segments(schedule.group_number, segments)
            if not result.get("message_id"):
                raise OneBotAPIError("发送未返回消息回执", uncertain=True)
            log.provider_message_id = str(result["message_id"])
            log.status = "succeeded"
            schedule.last_sent_at = now
            schedule.error_message = None
            if campaign.send_mode == "after_join":
                schedule.status = "completed"
                schedule.next_due_at = None
            elif campaign.send_mode == "scheduled":
                schedule.next_due_at = next_scheduled_time(campaign, now + timedelta(minutes=1))
            else:
                schedule.next_due_at = now + timedelta(minutes=campaign.interval_minutes)
        except OneBotAPIError as exc:
            log.status = "unknown" if exc.uncertain else "failed"
            log.error_message = schedule.error_message = exc.message[:2000]
            schedule.status = "paused"
        except Exception:
            log.status = "unknown"
            log.error_message = schedule.error_message = "发送结果未知，需核对远端消息"
            schedule.status = "paused"
        log.completed_at = now
        await self.db.commit()
        return True
