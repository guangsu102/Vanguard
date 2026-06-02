"""
Acquisition automation and advertisement management API.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.models import AccountOperationConfig, AccountType, TelegramAccount
from app.core.database import get_db
from app.core.scheduler.tasks import auto_join_groups_task, deliver_ads_task, replenish_keywords_task
from app.modules.acquisition.models import (
    AccountAdBinding,
    AdCampaign,
    AdCreative,
    AdCreativeType,
    AdDeliveryLog,
    AdSendMode,
    AutoJoinAttempt,
)


router = APIRouter()


# =============================================================================
# Account Operation Config
# =============================================================================

class AccountOperationConfigUpdate(BaseModel):
    auto_join_enabled: Optional[bool] = None
    auto_ads_enabled: Optional[bool] = None
    max_groups_per_day: Optional[int] = Field(None, ge=0, le=200)
    max_groups_total: Optional[int] = Field(None, ge=0, le=10000)
    join_interval_min_seconds: Optional[int] = Field(None, ge=60)
    join_interval_max_seconds: Optional[int] = Field(None, ge=60)
    next_join_after: Optional[datetime] = None
    max_messages_per_day: Optional[int] = Field(None, ge=0, le=1000)
    message_interval_seconds: Optional[int] = Field(None, ge=1)
    quiet_hours_start: Optional[str] = Field(None, max_length=5)
    quiet_hours_end: Optional[str] = Field(None, max_length=5)
    keyword_types: Optional[list[str]] = None
    keyword_auto_replenish_enabled: Optional[bool] = None
    keyword_replenish_requires_review: Optional[bool] = None
    risk_level: Optional[str] = Field(None, max_length=20)
    enabled: Optional[bool] = None


def _operation_config_to_dict(config: AccountOperationConfig) -> dict:
    keyword_types = []
    if config.keyword_types:
        try:
            keyword_types = json.loads(config.keyword_types)
        except (json.JSONDecodeError, TypeError):
            keyword_types = []
    return {
        "id": config.id,
        "account_id": config.account_id,
        "auto_join_enabled": config.auto_join_enabled,
        "auto_ads_enabled": config.auto_ads_enabled,
        "max_groups_per_day": config.max_groups_per_day,
        "max_groups_total": config.max_groups_total,
        "join_interval_min_seconds": config.join_interval_min_seconds,
        "join_interval_max_seconds": config.join_interval_max_seconds,
        "next_join_after": config.next_join_after.isoformat() if config.next_join_after else None,
        "max_messages_per_day": config.max_messages_per_day,
        "message_interval_seconds": config.message_interval_seconds,
        "quiet_hours_start": config.quiet_hours_start,
        "quiet_hours_end": config.quiet_hours_end,
        "keyword_types": keyword_types,
        "keyword_auto_replenish_enabled": config.keyword_auto_replenish_enabled,
        "keyword_replenish_requires_review": config.keyword_replenish_requires_review,
        "risk_level": config.risk_level,
        "enabled": config.enabled,
        "created_at": config.created_at.isoformat() if config.created_at else "",
        "updated_at": config.updated_at.isoformat() if config.updated_at else "",
    }


async def _get_or_create_operation_config(db: AsyncSession, account_id: int) -> AccountOperationConfig:
    account_result = await db.execute(select(TelegramAccount).where(TelegramAccount.id == account_id))
    account = account_result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    if account.account_type != AccountType.PROMOTER:
        raise HTTPException(status_code=400, detail="Only promoter accounts support growth automation config")

    result = await db.execute(
        select(AccountOperationConfig).where(AccountOperationConfig.account_id == account_id)
    )
    config = result.scalar_one_or_none()
    if config:
        return config

    config = AccountOperationConfig(account_id=account_id)
    db.add(config)
    await db.commit()
    await db.refresh(config)
    return config


@router.get("/accounts/{account_id:int}/operation-config")
async def get_account_operation_config(account_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    config = await _get_or_create_operation_config(db, account_id)
    return {"code": 0, "message": "success", "data": _operation_config_to_dict(config)}


@router.put("/accounts/{account_id:int}/operation-config")
async def update_account_operation_config(
    account_id: int,
    request: AccountOperationConfigUpdate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    config = await _get_or_create_operation_config(db, account_id)
    data = request.model_dump(exclude_none=True)
    if "keyword_types" in data:
        data["keyword_types"] = json.dumps(data["keyword_types"], ensure_ascii=False)

    min_interval = data.get("join_interval_min_seconds", config.join_interval_min_seconds)
    max_interval = data.get("join_interval_max_seconds", config.join_interval_max_seconds)
    if max_interval < min_interval:
        raise HTTPException(status_code=400, detail="join_interval_max_seconds must be >= min")

    for field, value in data.items():
        setattr(config, field, value)

    await db.commit()
    await db.refresh(config)
    return {"code": 0, "message": "success", "data": _operation_config_to_dict(config)}


# =============================================================================
# Manual Automation Runs
# =============================================================================

class KeywordReplenishRequest(BaseModel):
    min_per_type: Optional[dict[str, int]] = None
    generate_counts: Optional[dict[str, int]] = None
    auto_approve: bool = False


def _queued_automation_result(task_name: str, async_result: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "queued": True,
        "status": "queued",
        "task_name": task_name,
        "task_id": async_result.id,
        "payload": payload,
        "processed": 0,
        "created": 0,
        "updated": 0,
        "succeeded": 0,
        "skipped": 0,
        "failed": 0,
        "errors": [],
        "details": [
            {
                "action": "task_queued",
                "task_name": task_name,
                "task_id": async_result.id,
            }
        ],
    }


def _enqueue_automation_task(task: Any, task_name: str, **kwargs: Any) -> dict[str, Any]:
    try:
        async_result = task.apply_async(kwargs=kwargs, queue="automation")
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Automation queue unavailable: {exc}") from exc
    return _queued_automation_result(task_name, async_result, kwargs)


@router.post("/keywords/replenish", status_code=status.HTTP_202_ACCEPTED)
async def run_keyword_replenishment(
    request: KeywordReplenishRequest,
) -> dict:
    result = _enqueue_automation_task(
        replenish_keywords_task,
        "replenish_keywords_task",
        min_per_type=request.min_per_type,
        generate_counts=request.generate_counts,
        auto_approve=request.auto_approve,
    )
    return {"code": 0, "message": "success", "data": result}


class AutoJoinRunRequest(BaseModel):
    max_accounts: int = Field(default=10, ge=1, le=100)
    keywords_per_account: int = Field(default=5, ge=1, le=50)
    max_groups_per_keyword: int = Field(default=10, ge=1, le=50)
    dry_run: bool = False


@router.post("/auto-join/run", status_code=status.HTTP_202_ACCEPTED)
async def run_auto_join(request: AutoJoinRunRequest) -> dict:
    result = _enqueue_automation_task(
        auto_join_groups_task,
        "auto_join_groups_task",
        max_accounts=request.max_accounts,
        keywords_per_account=request.keywords_per_account,
        max_groups_per_keyword=request.max_groups_per_keyword,
        dry_run=request.dry_run,
    )
    return {"code": 0, "message": "success", "data": result}


class AdDeliveryRunRequest(BaseModel):
    max_deliveries: int = Field(default=20, ge=1, le=200)
    dry_run: bool = False


@router.post("/ads/run", status_code=status.HTTP_202_ACCEPTED)
async def run_ad_delivery(request: AdDeliveryRunRequest) -> dict:
    result = _enqueue_automation_task(
        deliver_ads_task,
        "deliver_ads_task",
        max_deliveries=request.max_deliveries,
        dry_run=request.dry_run,
    )
    return {"code": 0, "message": "success", "data": result}


# =============================================================================
# Auto-join Logs
# =============================================================================

@router.get("/auto-join/attempts")
async def list_auto_join_attempts(
    account_id: Optional[int] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> dict:
    query = select(AutoJoinAttempt)
    if account_id:
        query = query.where(AutoJoinAttempt.account_id == account_id)
    if status_filter:
        query = query.where(AutoJoinAttempt.status == status_filter)
    query = query.order_by(AutoJoinAttempt.attempted_at.desc()).limit(limit)
    rows = await db.execute(query)
    return {
        "code": 0,
        "message": "success",
        "data": [
            {
                "id": item.id,
                "account_id": item.account_id,
                "group_id": item.group_id,
                "telegram_group_id": item.telegram_group_id,
                "group_username": item.group_username,
                "group_title": item.group_title,
                "source_keyword": item.source_keyword,
                "status": item.status,
                "reason": item.reason,
                "error": item.error,
                "attempted_at": item.attempted_at.isoformat() if item.attempted_at else "",
                "joined_at": item.joined_at.isoformat() if item.joined_at else None,
            }
            for item in rows.scalars().all()
        ],
    }


# =============================================================================
# Advertisement CRUD
# =============================================================================

class AdCreativeCreate(BaseModel):
    name: str = Field(..., max_length=120)
    content: str = Field(..., min_length=1)
    creative_type: str = Field(default=AdCreativeType.TEXT.value)
    media_url: Optional[str] = None
    link_url: Optional[str] = None
    weight: int = Field(default=100, ge=0)
    enabled: bool = True


class AdCreativeUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=120)
    content: Optional[str] = Field(None, min_length=1)
    creative_type: Optional[str] = None
    media_url: Optional[str] = None
    link_url: Optional[str] = None
    weight: Optional[int] = Field(None, ge=0)
    enabled: Optional[bool] = None


def _creative_to_dict(item: AdCreative) -> dict:
    return {
        "id": item.id,
        "name": item.name,
        "content": item.content,
        "creative_type": item.creative_type,
        "media_url": item.media_url,
        "link_url": item.link_url,
        "weight": item.weight,
        "enabled": item.enabled,
        "created_at": item.created_at.isoformat() if item.created_at else "",
        "updated_at": item.updated_at.isoformat() if item.updated_at else "",
    }


@router.get("/ads/creatives")
async def list_ad_creatives(
    enabled: Optional[bool] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> dict:
    query = select(AdCreative)
    count_query = select(func.count(AdCreative.id))
    if enabled is not None:
        query = query.where(AdCreative.enabled == enabled)
        count_query = count_query.where(AdCreative.enabled == enabled)
    total = (await db.execute(count_query)).scalar() or 0
    rows = await db.execute(
        query.order_by(AdCreative.id.desc()).offset((page - 1) * page_size).limit(page_size)
    )
    return {"code": 0, "message": "success", "data": [_creative_to_dict(i) for i in rows.scalars().all()], "total": total}


@router.post("/ads/creatives", status_code=status.HTTP_201_CREATED)
async def create_ad_creative(request: AdCreativeCreate, db: AsyncSession = Depends(get_db)) -> dict:
    if request.creative_type not in {item.value for item in AdCreativeType}:
        raise HTTPException(status_code=400, detail="Invalid creative_type")
    creative = AdCreative(**request.model_dump())
    db.add(creative)
    await db.commit()
    await db.refresh(creative)
    return {"code": 0, "message": "success", "data": _creative_to_dict(creative)}


@router.put("/ads/creatives/{creative_id:int}")
async def update_ad_creative(
    creative_id: int,
    request: AdCreativeUpdate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    creative = (await db.execute(select(AdCreative).where(AdCreative.id == creative_id))).scalar_one_or_none()
    if not creative:
        raise HTTPException(status_code=404, detail="Creative not found")
    data = request.model_dump(exclude_none=True)
    if "creative_type" in data and data["creative_type"] not in {item.value for item in AdCreativeType}:
        raise HTTPException(status_code=400, detail="Invalid creative_type")
    for field, value in data.items():
        setattr(creative, field, value)
    await db.commit()
    await db.refresh(creative)
    return {"code": 0, "message": "success", "data": _creative_to_dict(creative)}


@router.delete("/ads/creatives/{creative_id:int}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_ad_creative(creative_id: int, db: AsyncSession = Depends(get_db)) -> None:
    creative = (await db.execute(select(AdCreative).where(AdCreative.id == creative_id))).scalar_one_or_none()
    if not creative:
        raise HTTPException(status_code=404, detail="Creative not found")
    await db.delete(creative)
    await db.commit()


class AdCampaignCreate(BaseModel):
    name: str = Field(..., max_length=120)
    enabled: bool = False
    status: str = Field(default="draft", max_length=30)
    send_mode: str = Field(default=AdSendMode.AFTER_JOIN.value)
    target_group_levels: list[str] = Field(default_factory=lambda: ["A"])
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None
    min_wait_after_join_minutes: int = Field(default=60, ge=0)
    interval_minutes: int = Field(default=1440, ge=1)
    scheduled_times: Optional[list[str]] = None
    max_sends_per_group_per_day: int = Field(default=1, ge=0)
    max_sends_per_account_per_day: int = Field(default=20, ge=0)


class AdCampaignUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=120)
    enabled: Optional[bool] = None
    status: Optional[str] = Field(None, max_length=30)
    send_mode: Optional[str] = None
    target_group_levels: Optional[list[str]] = None
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None
    min_wait_after_join_minutes: Optional[int] = Field(None, ge=0)
    interval_minutes: Optional[int] = Field(None, ge=1)
    scheduled_times: Optional[list[str]] = None
    max_sends_per_group_per_day: Optional[int] = Field(None, ge=0)
    max_sends_per_account_per_day: Optional[int] = Field(None, ge=0)


def _campaign_to_dict(item: AdCampaign) -> dict:
    return {
        "id": item.id,
        "name": item.name,
        "enabled": item.enabled,
        "status": item.status,
        "send_mode": item.send_mode,
        "target_group_levels": item.get_target_levels(),
        "start_at": item.start_at.isoformat() if item.start_at else None,
        "end_at": item.end_at.isoformat() if item.end_at else None,
        "min_wait_after_join_minutes": item.min_wait_after_join_minutes,
        "interval_minutes": item.interval_minutes,
        "scheduled_times": item.get_scheduled_times(),
        "max_sends_per_group_per_day": item.max_sends_per_group_per_day,
        "max_sends_per_account_per_day": item.max_sends_per_account_per_day,
        "created_at": item.created_at.isoformat() if item.created_at else "",
        "updated_at": item.updated_at.isoformat() if item.updated_at else "",
    }


def _campaign_payload(data: dict) -> dict:
    if "send_mode" in data and data["send_mode"] not in {item.value for item in AdSendMode}:
        raise HTTPException(status_code=400, detail="Invalid send_mode")
    if "target_group_levels" in data:
        data["target_group_levels"] = json.dumps(data["target_group_levels"], ensure_ascii=False)
    if "scheduled_times" in data:
        data["scheduled_times"] = json.dumps(data["scheduled_times"] or [], ensure_ascii=False)
    return data


@router.get("/ads/campaigns")
async def list_ad_campaigns(
    enabled: Optional[bool] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> dict:
    query = select(AdCampaign)
    count_query = select(func.count(AdCampaign.id))
    if enabled is not None:
        query = query.where(AdCampaign.enabled == enabled)
        count_query = count_query.where(AdCampaign.enabled == enabled)
    total = (await db.execute(count_query)).scalar() or 0
    rows = await db.execute(query.order_by(AdCampaign.id.desc()).offset((page - 1) * page_size).limit(page_size))
    return {"code": 0, "message": "success", "data": [_campaign_to_dict(i) for i in rows.scalars().all()], "total": total}


@router.post("/ads/campaigns", status_code=status.HTTP_201_CREATED)
async def create_ad_campaign(request: AdCampaignCreate, db: AsyncSession = Depends(get_db)) -> dict:
    data = _campaign_payload(request.model_dump())
    campaign = AdCampaign(**data)
    db.add(campaign)
    await db.commit()
    await db.refresh(campaign)
    return {"code": 0, "message": "success", "data": _campaign_to_dict(campaign)}


@router.put("/ads/campaigns/{campaign_id:int}")
async def update_ad_campaign(
    campaign_id: int,
    request: AdCampaignUpdate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    campaign = (await db.execute(select(AdCampaign).where(AdCampaign.id == campaign_id))).scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    data = _campaign_payload(request.model_dump(exclude_none=True))
    for field, value in data.items():
        setattr(campaign, field, value)
    await db.commit()
    await db.refresh(campaign)
    return {"code": 0, "message": "success", "data": _campaign_to_dict(campaign)}


@router.delete("/ads/campaigns/{campaign_id:int}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_ad_campaign(campaign_id: int, db: AsyncSession = Depends(get_db)) -> None:
    campaign = (await db.execute(select(AdCampaign).where(AdCampaign.id == campaign_id))).scalar_one_or_none()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    await db.delete(campaign)
    await db.commit()


class AccountAdBindingCreate(BaseModel):
    account_id: int
    ad_campaign_id: int
    creative_id: Optional[int] = None
    enabled: bool = True
    priority: int = 0


class AccountAdBindingUpdate(BaseModel):
    creative_id: Optional[int] = None
    enabled: Optional[bool] = None
    priority: Optional[int] = None


def _binding_to_dict(item: AccountAdBinding) -> dict:
    return {
        "id": item.id,
        "account_id": item.account_id,
        "ad_campaign_id": item.ad_campaign_id,
        "creative_id": item.creative_id,
        "enabled": item.enabled,
        "priority": item.priority,
        "created_at": item.created_at.isoformat() if item.created_at else "",
        "updated_at": item.updated_at.isoformat() if item.updated_at else "",
    }


@router.get("/ads/bindings")
async def list_account_ad_bindings(
    account_id: Optional[int] = None,
    campaign_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
) -> dict:
    query = select(AccountAdBinding)
    if account_id:
        query = query.where(AccountAdBinding.account_id == account_id)
    if campaign_id:
        query = query.where(AccountAdBinding.ad_campaign_id == campaign_id)
    rows = await db.execute(query.order_by(AccountAdBinding.priority.desc(), AccountAdBinding.id.desc()))
    return {"code": 0, "message": "success", "data": [_binding_to_dict(i) for i in rows.scalars().all()]}


@router.post("/ads/bindings", status_code=status.HTTP_201_CREATED)
async def create_account_ad_binding(request: AccountAdBindingCreate, db: AsyncSession = Depends(get_db)) -> dict:
    binding = AccountAdBinding(**request.model_dump())
    db.add(binding)
    await db.commit()
    await db.refresh(binding)
    return {"code": 0, "message": "success", "data": _binding_to_dict(binding)}


@router.put("/ads/bindings/{binding_id:int}")
async def update_account_ad_binding(
    binding_id: int,
    request: AccountAdBindingUpdate,
    db: AsyncSession = Depends(get_db),
) -> dict:
    binding = (await db.execute(select(AccountAdBinding).where(AccountAdBinding.id == binding_id))).scalar_one_or_none()
    if not binding:
        raise HTTPException(status_code=404, detail="Binding not found")
    for field, value in request.model_dump(exclude_none=True).items():
        setattr(binding, field, value)
    await db.commit()
    await db.refresh(binding)
    return {"code": 0, "message": "success", "data": _binding_to_dict(binding)}


@router.delete("/ads/bindings/{binding_id:int}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account_ad_binding(binding_id: int, db: AsyncSession = Depends(get_db)) -> None:
    binding = (await db.execute(select(AccountAdBinding).where(AccountAdBinding.id == binding_id))).scalar_one_or_none()
    if not binding:
        raise HTTPException(status_code=404, detail="Binding not found")
    await db.delete(binding)
    await db.commit()


@router.get("/ads/delivery-logs")
async def list_ad_delivery_logs(
    account_id: Optional[int] = None,
    campaign_id: Optional[int] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> dict:
    query = select(AdDeliveryLog)
    if account_id:
        query = query.where(AdDeliveryLog.account_id == account_id)
    if campaign_id:
        query = query.where(AdDeliveryLog.ad_campaign_id == campaign_id)
    if status_filter:
        query = query.where(AdDeliveryLog.status == status_filter)
    rows = await db.execute(query.order_by(AdDeliveryLog.created_at.desc()).limit(limit))
    return {
        "code": 0,
        "message": "success",
        "data": [
            {
                "id": item.id,
                "account_id": item.account_id,
                "group_id": item.group_id,
                "telegram_group_id": item.telegram_group_id,
                "ad_campaign_id": item.ad_campaign_id,
                "creative_id": item.creative_id,
                "status": item.status,
                "telegram_message_id": item.telegram_message_id,
                "error": item.error,
                "sent_at": item.sent_at.isoformat() if item.sent_at else None,
                "created_at": item.created_at.isoformat() if item.created_at else "",
            }
            for item in rows.scalars().all()
        ],
    }
