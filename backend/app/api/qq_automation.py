"""QQ growth accounts, target groups, shared creative bindings and execution history."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.ephemeral_secret import encrypt_ephemeral_secret
from app.core.security import require_admin
from app.modules.acquisition.models import AdCreative
from app.modules.qq.automation import initial_due, render_creative, utcnow
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
from app.modules.qq.runtime import client_for_connection
from app.modules.qq.service import QQEventProcessor, ensure_qq_connection

router = APIRouter(prefix="/automation")


def response(data: Any, **extra: Any) -> dict[str, Any]:
    return {"code": 0, "message": "success", "data": data, **extra}


def dump_model(model: Any) -> dict[str, Any]:
    return {
        column.name: value.isoformat() if isinstance(value, datetime) else value
        for column in model.__table__.columns
        if "token" not in column.name
        for value in [getattr(model, column.name)]
    }


def account_dict(account: QQBotConnection) -> dict[str, Any]:
    data = dump_model(account)
    configured = bool(account.http_url and account.access_token_encrypted)
    is_default = account.app_id == (settings.QQ_ONEBOT_ACCOUNT_ID or "").strip()
    data.update(
        configured=configured or (is_default and settings.QQ_ONEBOT_ENABLED),
        http_url=account.http_url or (settings.QQ_ONEBOT_HTTP_URL if is_default else None),
        join_configured=bool(account.join_api_url and account.join_api_token_encrypted),
    )
    return data


def check_url(value: str | None) -> None:
    if not value:
        return
    parts = urlsplit(value)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise HTTPException(422, "接口地址必须为无用户名、密码和查询参数的 HTTP/HTTPS 地址")


async def get_account(db: AsyncSession, account_id: int) -> QQBotConnection:
    account = await db.get(QQBotConnection, account_id)
    if account is None:
        raise HTTPException(404, "QQ 账号不存在")
    return account


class QQAccountCreate(BaseModel):
    account_number: str = Field(pattern=r"^\d{5,20}$")
    display_name: str | None = Field(None, max_length=120)
    http_url: str = Field(min_length=1, max_length=500)
    access_token: str = Field(min_length=32, max_length=2000)


class QQAccountUpdate(BaseModel):
    display_name: str | None = Field(None, max_length=120)
    enabled: bool | None = None
    automation_enabled: bool | None = None
    http_url: str | None = Field(None, max_length=500)
    access_token: str | None = Field(None, min_length=32, max_length=2000)
    join_api_url: str | None = Field(None, max_length=500)
    join_api_token: str | None = Field(None, min_length=32, max_length=2000)
    join_interval_seconds: int | None = Field(None, ge=1, le=86400)
    send_interval_seconds: int | None = Field(None, ge=1, le=86400)
    max_joins_per_day: int | None = Field(None, ge=0, le=10000)
    max_sends_per_day: int | None = Field(None, ge=0, le=10000)


@router.get("/accounts")
async def list_accounts(db: AsyncSession = Depends(get_db)) -> dict:
    default_account = (settings.QQ_ONEBOT_ACCOUNT_ID or "").strip()
    if settings.QQ_ONEBOT_ENABLED and default_account:
        await ensure_qq_connection(db, default_account)
        await db.commit()
    accounts = await db.scalars(select(QQBotConnection).order_by(QQBotConnection.id))
    return response([account_dict(a) for a in accounts])


@router.post("/accounts", status_code=201)
async def create_account(
    request: QQAccountCreate,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(require_admin),
) -> dict:
    check_url(request.http_url)
    if await db.scalar(
        select(QQBotConnection.id).where(QQBotConnection.app_id == request.account_number)
    ):
        raise HTTPException(409, "QQ 账号已经存在")
    account = QQBotConnection(
        app_id=request.account_number,
        display_name=request.display_name,
        http_url=request.http_url.rstrip("/"),
        access_token_encrypted=encrypt_ephemeral_secret(request.access_token),
    )
    db.add(account)
    await db.commit()
    return response(account_dict(account))


@router.patch("/accounts/{account_id}")
async def update_account(
    account_id: int,
    request: QQAccountUpdate,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(require_admin),
) -> dict:
    account = await get_account(db, account_id)
    changes = request.model_dump(exclude_unset=True)
    nullable = {"display_name", "http_url", "join_api_url"}
    if any(value is None and name not in nullable for name, value in changes.items()):
        raise HTTPException(422, "账号开关、凭证和额度不能设为 null")
    check_url(changes.get("http_url"))
    check_url(changes.get("join_api_url"))
    for name in ("access_token", "join_api_token"):
        if name in changes:
            setattr(account, f"{name}_encrypted", encrypt_ephemeral_secret(changes.pop(name)))
    for name, value in changes.items():
        setattr(account, name, value)
    await db.commit()
    return response(account_dict(account))


@router.post("/accounts/{account_id}/sync")
async def sync_account(
    account_id: int,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(require_admin),
) -> dict:
    account = await get_account(db, account_id)
    client = client_for_connection(account)
    try:
        login = await client.get_login_info()
        if str(login.get("user_id") or "") != account.app_id:
            raise HTTPException(409, "NapCat 登录 QQ 号与账号配置不一致")
        groups = await QQEventProcessor(db).sync_groups(account, await client.get_group_list())
        account.status = "online"
        account.last_error = None
        account.last_heartbeat_at = utcnow()
        await db.commit()
        return response({"total": len(groups)})
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc
    finally:
        await client.close()


class QQTargetInput(BaseModel):
    group_number: str = Field(pattern=r"^\d{5,20}$")
    local_name: str | None = Field(None, max_length=255)
    verify_message: str = Field("", max_length=500)
    enabled: bool = True


class QQCampaignInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = False
    auto_join_enabled: bool = True
    send_mode: Literal["after_join", "interval", "scheduled"] = "interval"
    min_wait_after_join_minutes: int = Field(60, ge=0, le=10080)
    interval_minutes: int = Field(1440, ge=1, le=525600)
    scheduled_times: list[str] = Field(default_factory=list, max_length=48)
    timezone: str = "Asia/Shanghai"
    max_sends_per_group_per_day: int = Field(1, ge=0, le=10000)
    max_sends_per_account_per_day: int = Field(30, ge=0, le=10000)
    start_at: datetime | None = None
    end_at: datetime | None = None
    targets: list[QQTargetInput] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def validate_schedule(self) -> QQCampaignInput:
        if any(not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", t) for t in self.scheduled_times):
            raise ValueError("定时时间应为 HH:MM")
        if self.send_mode == "scheduled" and not self.scheduled_times:
            raise ValueError("定时投放至少需要一个时间点")
        try:
            ZoneInfo(self.timezone)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("无效时区") from exc
        for name in ("start_at", "end_at"):
            value = getattr(self, name)
            if value is not None and value.tzinfo is not None:
                setattr(self, name, value.astimezone(UTC).replace(tzinfo=None))
        if self.start_at and self.end_at and self.end_at <= self.start_at:
            raise ValueError("结束时间必须晚于开始时间")
        if len({t.group_number for t in self.targets}) != len(self.targets):
            raise ValueError("目标群号不能重复")
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("广告计划名称不能为空")
        return self


async def campaign_dict(db: AsyncSession, campaign: QQAdCampaign) -> dict:
    data = dump_model(campaign)
    data["scheduled_times"] = json.loads(data.pop("scheduled_times_json"))
    targets = await db.scalars(
        select(QQCampaignTarget).where(QQCampaignTarget.campaign_id == campaign.id)
    )
    data["targets"] = [dump_model(t) for t in targets]
    return data


async def save_campaign(db: AsyncSession, campaign: QQAdCampaign, request: QQCampaignInput) -> dict:
    duplicate = await db.scalar(
        select(QQAdCampaign.id).where(
            QQAdCampaign.name == request.name,
            QQAdCampaign.id != (campaign.id or 0),
        )
    )
    if duplicate:
        raise HTTPException(409, "广告计划名称已经存在")
    values = request.model_dump(exclude={"targets", "scheduled_times"})
    values["scheduled_times_json"] = json.dumps(sorted(set(request.scheduled_times)))
    for name, value in values.items():
        setattr(campaign, name, value)
    db.add(campaign)
    await db.flush()
    await db.execute(delete(QQCampaignTarget).where(QQCampaignTarget.campaign_id == campaign.id))
    for target in request.targets:
        db.add(QQCampaignTarget(campaign_id=campaign.id, **target.model_dump()))
    schedules = await db.scalars(
        select(QQAdSchedule).where(
            QQAdSchedule.campaign_id == campaign.id,
            QQAdSchedule.status == "active",
        )
    )
    for schedule in schedules:
        if campaign.send_mode == "after_join" and schedule.last_sent_at:
            schedule.status = "completed"
            schedule.next_due_at = None
        else:
            member = await db.scalar(
                select(QQManagedGroup).where(
                    QQManagedGroup.connection_id == schedule.connection_id,
                    QQManagedGroup.group_openid == schedule.group_number,
                )
            )
            base = initial_due(
                campaign,
                member.bot_added_at if member and member.bot_added_at else utcnow(),
                utcnow(),
            )
            if schedule.last_sent_at and campaign.send_mode == "interval":
                from datetime import timedelta

                base = max(
                    base, schedule.last_sent_at + timedelta(minutes=campaign.interval_minutes)
                )
            schedule.next_due_at = base
    await db.commit()
    return response(await campaign_dict(db, campaign))


@router.get("/campaigns")
async def list_campaigns(db: AsyncSession = Depends(get_db)) -> dict:
    rows = await db.scalars(select(QQAdCampaign).order_by(QQAdCampaign.id.desc()))
    return response([await campaign_dict(db, c) for c in rows])


@router.post("/campaigns", status_code=201)
async def create_campaign(
    request: QQCampaignInput,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(require_admin),
) -> dict:
    return await save_campaign(db, QQAdCampaign(), request)


@router.put("/campaigns/{campaign_id}")
async def update_campaign(
    campaign_id: int,
    request: QQCampaignInput,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(require_admin),
) -> dict:
    campaign = await db.get(QQAdCampaign, campaign_id)
    if campaign is None:
        raise HTTPException(404, "QQ 广告计划不存在")
    return await save_campaign(db, campaign, request)


class QQBindingInput(BaseModel):
    connection_id: int
    campaign_id: int
    creative_ids: list[int] = Field(min_length=1, max_length=100)
    priority: int = Field(100, ge=1, le=10000)


@router.get("/bindings")
async def list_bindings(db: AsyncSession = Depends(get_db)) -> dict:
    rows = await db.scalars(select(QQAdBinding).order_by(QQAdBinding.id))
    return response([dump_model(b) for b in rows])


@router.post("/bindings", status_code=201)
async def create_bindings(
    request: QQBindingInput,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(require_admin),
) -> dict:
    await get_account(db, request.connection_id)
    if await db.get(QQAdCampaign, request.campaign_id) is None:
        raise HTTPException(404, "QQ 广告计划不存在")
    ids = set(request.creative_ids)
    creatives = list(await db.scalars(select(AdCreative).where(AdCreative.id.in_(ids))))
    if len(creatives) != len(ids) or any(not c.enabled for c in creatives):
        raise HTTPException(422, "素材不存在或已停用")
    for creative in creatives:
        try:
            render_creative(creative)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    existing = set(
        await db.scalars(
            select(QQAdBinding.creative_id).where(
                QQAdBinding.connection_id == request.connection_id,
                QQAdBinding.campaign_id == request.campaign_id,
            )
        )
    )
    for creative_id in ids - existing:
        db.add(
            QQAdBinding(
                connection_id=request.connection_id,
                campaign_id=request.campaign_id,
                creative_id=creative_id,
                priority=request.priority,
            )
        )
    await db.commit()
    return response({"created": len(ids - existing)})


@router.delete("/bindings/{binding_id}", status_code=204)
async def delete_binding(
    binding_id: int,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(require_admin),
) -> None:
    binding = await db.get(QQAdBinding, binding_id)
    if binding is None:
        raise HTTPException(404, "绑定不存在")
    await db.delete(binding)
    await db.commit()


@router.get("/join-tasks")
async def list_join_tasks(
    connection_id: int | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> dict:
    filters = [QQJoinTask.connection_id == connection_id] if connection_id is not None else []
    query = (
        select(QQJoinTask)
        .where(*filters)
        .order_by(QQJoinTask.id.desc())
        .offset(offset)
        .limit(limit)
    )
    total = await db.scalar(select(func.count(QQJoinTask.id)).where(*filters))
    return response([dump_model(t) for t in await db.scalars(query)], total=total)


@router.post("/join-tasks/{task_id}/retry")
async def retry_join_task(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(require_admin),
) -> dict:
    task = await db.get(QQJoinTask, task_id)
    if not task:
        raise HTTPException(404, "加群任务不存在")
    if task.status not in {"failed", "rejected", "unsupported", "action_required", "cancelled"}:
        raise HTTPException(409, "待审核或结果未知的请求应先核对，不能重复申请")
    task.status = "queued"
    task.error_message = None
    await db.commit()
    return response(dump_model(task))


@router.post("/join-tasks/{task_id}/cancel")
async def cancel_join_task(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(require_admin),
) -> dict:
    task = await db.get(QQJoinTask, task_id)
    if not task or task.status not in {
        "queued",
        "failed",
        "unsupported",
        "rejected",
        "action_required",
    }:
        raise HTTPException(409, "当前任务不能取消已提交的远端请求")
    task.status = "cancelled"
    await db.commit()
    return response(dump_model(task))


@router.get("/schedules")
async def list_schedules(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = await db.scalars(
        select(QQAdSchedule).order_by(QQAdSchedule.id.desc()).offset(offset).limit(limit)
    )
    total = await db.scalar(select(func.count(QQAdSchedule.id)))
    return response([dump_model(s) for s in rows], total=total)


@router.get("/logs")
async def list_logs(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = await db.scalars(
        select(QQAutomationLog)
        .order_by(QQAutomationLog.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    total = await db.scalar(select(func.count(QQAutomationLog.id)))
    return response([dump_model(log) for log in rows], total=total)


class QQScheduleResume(BaseModel):
    confirmed_not_sent: bool = False


@router.post("/schedules/{schedule_id}/resume")
async def resume_schedule(
    schedule_id: int,
    request: QQScheduleResume,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(require_admin),
) -> dict:
    schedule = await db.get(QQAdSchedule, schedule_id)
    if not schedule or schedule.status != "paused":
        raise HTTPException(409, "投放任务当前没有暂停")
    unresolved = await db.scalar(
        select(QQAutomationLog.id)
        .where(
            QQAutomationLog.connection_id == schedule.connection_id,
            QQAutomationLog.campaign_id == schedule.campaign_id,
            QQAutomationLog.group_number == schedule.group_number,
            QQAutomationLog.operation_type == "ad",
            QQAutomationLog.status.in_(["unknown", "sending"]),
        )
        .limit(1)
    )
    if unresolved and not request.confirmed_not_sent:
        raise HTTPException(409, "请先核对远端群消息，确认未发送后再恢复")
    if unresolved:
        log = await db.get(QQAutomationLog, unresolved)
        if log.status == "sending":
            raise HTTPException(409, "发送仍在执行，不能恢复")
        log.status = "confirmed_not_sent"
    schedule.status = "active"
    schedule.error_message = None
    schedule.next_due_at = utcnow()
    await db.commit()
    return response(dump_model(schedule))
