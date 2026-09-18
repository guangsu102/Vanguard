"""API for manual, read-only Telegram group resource searches."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.account.models import (
    AccountOperationMode,
    AccountStatus,
    AccountType,
    TelegramAccount,
)
from app.core.config import settings
from app.core.database import get_db
from app.core.security import require_admin
from app.modules.acquisition.models import (
    ResourceReviewStatus,
    ResourceSearchAccountTask,
    ResourceSearchResult,
    ResourceSearchRun,
    ResourceSearchStatus,
)

router = APIRouter()


class ResourceSearchCreateRequest(BaseModel):
    keywords: list[str] = Field(..., min_length=1, max_length=20)
    account_ids: list[int] = Field(..., min_length=1, max_length=20)
    max_results_per_keyword: int = Field(default=20, ge=5, le=50)

    @field_validator("keywords")
    @classmethod
    def normalize_keywords(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            keyword = value.strip()
            if not keyword:
                continue
            if len(keyword) > 255:
                raise ValueError("关键词长度不能超过 255 个字符")
            signature = keyword.casefold()
            if signature not in seen:
                seen.add(signature)
                normalized.append(keyword)
        if not normalized:
            raise ValueError("至少需要一个有效关键词")
        return normalized

    @field_validator("account_ids")
    @classmethod
    def normalize_account_ids(cls, values: list[int]) -> list[int]:
        normalized = list(dict.fromkeys(int(value) for value in values if int(value) > 0))
        if not normalized:
            raise ValueError("至少需要一个有效账号")
        return normalized


class ResourceSearchReviewRequest(BaseModel):
    review_status: str | None = None
    note: str | None = Field(default=None, max_length=4000)

    @field_validator("review_status")
    @classmethod
    def validate_review_status(cls, value: str | None) -> str | None:
        if value is None:
            return None
        allowed = {item.value for item in ResourceReviewStatus}
        if value not in allowed:
            raise ValueError(f"review_status 必须是 {', '.join(sorted(allowed))}")
        return value

    @model_validator(mode="after")
    def validate_changes(self):
        if self.review_status is None and self.note is None:
            raise ValueError("至少需要修改分析状态或备注")
        return self


def _json_list(raw: str | None, item_type: type = str) -> list:
    try:
        values = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(values, list):
        return []
    result = []
    for value in values:
        try:
            result.append(item_type(value))
        except (TypeError, ValueError):
            continue
    return result


def _session_available(account: TelegramAccount) -> bool:
    if account.session_string:
        return True
    session_dir = Path(settings.TELEGRAM_SESSION_DIR)
    return (session_dir / f"{account.session_name}.session").exists()


def _utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return aware.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _operation_mode(account: TelegramAccount) -> str:
    config = account.__dict__.get("operation_config")
    return (
        config.operation_mode
        if config is not None
        else AccountOperationMode.GROWTH.value
    )


def _locked_resource_search_accounts_query(account_ids: list[int]):
    """Lock only selected account rows while validating a new search run."""
    return (
        select(TelegramAccount)
        .options(selectinload(TelegramAccount.operation_config))
        .where(TelegramAccount.id.in_(account_ids))
        .order_by(TelegramAccount.id)
        .with_for_update(of=TelegramAccount)
    )


def _serialize_account_task(task: ResourceSearchAccountTask) -> dict:
    return {
        "id": task.id,
        "account_id": task.account_id,
        "account_identifier": task.account_identifier,
        "status": task.status,
        "keywords_completed": task.keywords_completed,
        "successful_keywords": task.successful_keywords,
        "result_count": task.result_count,
        "error": task.error,
        "flood_wait_seconds": task.flood_wait_seconds,
        "started_at": _utc_iso(task.started_at),
        "completed_at": _utc_iso(task.completed_at),
    }


def _serialize_run(run: ResourceSearchRun, *, include_accounts: bool = False) -> dict:
    data = {
        "id": run.id,
        "keywords": _json_list(run.keywords_json),
        "account_ids": _json_list(run.account_ids_json, int),
        "max_results_per_keyword": run.max_results_per_keyword,
        "status": run.status,
        "total_accounts": run.total_accounts,
        "completed_accounts": run.completed_accounts,
        "successful_accounts": run.successful_accounts,
        "failed_accounts": run.failed_accounts,
        "raw_result_count": run.raw_result_count,
        "unique_result_count": run.unique_result_count,
        "error_summary": run.error_summary,
        "created_by_id": run.created_by_id,
        "celery_task_id": run.celery_task_id,
        "heartbeat_at": _utc_iso(run.heartbeat_at),
        "cancel_requested_at": _utc_iso(run.cancel_requested_at),
        "started_at": _utc_iso(run.started_at),
        "completed_at": _utc_iso(run.completed_at),
        "created_at": _utc_iso(run.created_at),
        "updated_at": _utc_iso(run.updated_at),
    }
    if include_accounts:
        data["accounts"] = [
            _serialize_account_task(task)
            for task in sorted(run.account_tasks, key=lambda item: item.id)
        ]
    return data


def _serialize_result(
    result: ResourceSearchResult,
    account_labels: dict[int, str],
) -> dict:
    account_ids = _json_list(result.discovered_by_account_ids_json, int)
    return {
        "id": result.id,
        "run_id": result.run_id,
        "telegram_group_id": result.telegram_group_id,
        "title": result.title,
        "username": result.username,
        "invite_link": result.invite_link,
        "member_count": result.member_count,
        "is_private": result.is_private,
        "matched_keywords": _json_list(result.matched_keywords_json),
        "discovered_by_accounts": [
            {
                "id": account_id,
                "identifier": account_labels.get(account_id, f"账号 {account_id}"),
            }
            for account_id in account_ids
        ],
        "discovery_count": result.discovery_count,
        "review_status": result.review_status,
        "note": result.note,
        "first_found_at": _utc_iso(result.first_found_at),
        "last_found_at": _utc_iso(result.last_found_at),
        "reviewed_at": _utc_iso(result.reviewed_at),
        "reviewed_by_id": result.reviewed_by_id,
    }


def enqueue_resource_search(run_id: int, task_id: str) -> None:
    from app.core.scheduler.tasks import resource_search_task

    resource_search_task.apply_async(
        args=[run_id],
        queue="resource_search",
        task_id=task_id,
    )


@router.get("/accounts")
async def list_resource_search_accounts(
    _current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = await db.execute(
        select(TelegramAccount)
        .options(selectinload(TelegramAccount.operation_config))
        .where(
            TelegramAccount.account_type == AccountType.PROMOTER,
            TelegramAccount.is_active,
            TelegramAccount.status.in_([AccountStatus.ONLINE, AccountStatus.IDLE]),
        )
        .order_by(TelegramAccount.display_name, TelegramAccount.identifier)
    )
    accounts = rows.scalars().unique().all()
    return {
        "code": 0,
        "message": "success",
        "data": [
            {
                "id": account.id,
                "identifier": account.identifier,
                "display_name": account.display_name,
                "status": account.status.value,
                "operation_mode": _operation_mode(account),
                "session_available": _session_available(account),
            }
            for account in accounts
        ],
    }


@router.post("/runs", status_code=status.HTTP_202_ACCEPTED)
async def create_resource_search_run(
    request: ResourceSearchCreateRequest,
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = await db.execute(_locked_resource_search_accounts_query(request.account_ids))
    accounts = rows.scalars().unique().all()
    account_by_id = {account.id: account for account in accounts}
    invalid_accounts = []
    for account_id in request.account_ids:
        account = account_by_id.get(account_id)
        if account is None:
            invalid_accounts.append({"id": account_id, "reason": "账号不存在"})
        elif account.account_type != AccountType.PROMOTER:
            invalid_accounts.append({"id": account_id, "reason": "不是推广账号"})
        elif not account.is_active:
            invalid_accounts.append({"id": account_id, "reason": "账号未启用"})
        elif account.status not in {AccountStatus.ONLINE, AccountStatus.IDLE}:
            invalid_accounts.append(
                {"id": account_id, "reason": f"账号状态为 {account.status.value}"}
            )
        elif not _session_available(account):
            invalid_accounts.append({"id": account_id, "reason": "缺少登录会话"})
    if invalid_accounts:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"message": "部分账号当前不可用于资源搜索", "accounts": invalid_accounts},
        )

    conflict_rows = await db.execute(
        select(ResourceSearchAccountTask.account_id, ResourceSearchRun.id)
        .join(ResourceSearchRun, ResourceSearchRun.id == ResourceSearchAccountTask.run_id)
        .where(
            ResourceSearchAccountTask.account_id.in_(request.account_ids),
            ResourceSearchRun.status.in_(
                [
                    ResourceSearchStatus.QUEUED.value,
                    ResourceSearchStatus.RUNNING.value,
                ]
            ),
        )
    )
    conflicts = [
        {"account_id": int(account_id), "run_id": int(run_id)}
        for account_id, run_id in conflict_rows.all()
        if account_id is not None
    ]
    if conflicts:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "部分账号已在其他资源搜索批次中",
                "conflicts": conflicts,
            },
        )

    task_id = uuid.uuid4().hex
    run = ResourceSearchRun(
        keywords_json=json.dumps(request.keywords, ensure_ascii=False),
        account_ids_json=json.dumps(request.account_ids),
        max_results_per_keyword=request.max_results_per_keyword,
        status=ResourceSearchStatus.QUEUED.value,
        total_accounts=len(request.account_ids),
        created_by_id=int(current_user["id"]),
        celery_task_id=task_id,
    )
    db.add(run)
    await db.flush()
    for account_id in request.account_ids:
        account = account_by_id[account_id]
        db.add(
            ResourceSearchAccountTask(
                run_id=run.id,
                account_id=account.id,
                account_identifier=account.display_name or account.identifier,
                status=ResourceSearchStatus.QUEUED.value,
            )
        )
    await db.commit()

    try:
        enqueue_resource_search(run.id, task_id)
    except Exception as exc:
        now = datetime.utcnow()
        run.status = ResourceSearchStatus.FAILED.value
        run.error_summary = f"搜索任务入队失败: {str(exc) or exc.__class__.__name__}"
        run.completed_at = now
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"message": "搜索任务入队失败", "run_id": run.id},
        ) from exc

    await db.refresh(run)
    return {"code": 0, "message": "资源搜索已排队", "data": _serialize_run(run)}


@router.get("/runs")
async def list_resource_search_runs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=10, le=100),
    _current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    total = (
        await db.execute(select(func.count(ResourceSearchRun.id)))
    ).scalar_one()
    rows = await db.execute(
        select(ResourceSearchRun)
        .order_by(ResourceSearchRun.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "code": 0,
        "message": "success",
        "data": [_serialize_run(run) for run in rows.scalars().all()],
        "total": int(total or 0),
        "page": page,
        "page_size": page_size,
    }


@router.get("/runs/{run_id}")
async def get_resource_search_run(
    run_id: int,
    _current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    run = (
        await db.execute(
            select(ResourceSearchRun)
            .options(selectinload(ResourceSearchRun.account_tasks))
            .where(ResourceSearchRun.id == run_id)
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="资源搜索批次不存在")
    return {
        "code": 0,
        "message": "success",
        "data": _serialize_run(run, include_accounts=True),
    }


@router.post("/runs/{run_id}/cancel")
async def cancel_resource_search_run(
    run_id: int,
    _current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    run = (
        await db.execute(
            select(ResourceSearchRun)
            .where(ResourceSearchRun.id == run_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="资源搜索批次不存在")
    terminal = {
        ResourceSearchStatus.COMPLETED.value,
        ResourceSearchStatus.PARTIAL.value,
        ResourceSearchStatus.FAILED.value,
        ResourceSearchStatus.CANCELLED.value,
    }
    if run.status not in terminal:
        now = datetime.utcnow()
        run.cancel_requested_at = now
        run.updated_at = now
        if run.status == ResourceSearchStatus.QUEUED.value:
            run.status = ResourceSearchStatus.CANCELLED.value
            run.completed_at = now
            task_rows = await db.execute(
                select(ResourceSearchAccountTask).where(
                    ResourceSearchAccountTask.run_id == run.id
                )
            )
            for task in task_rows.scalars().all():
                if task.status in {
                    ResourceSearchStatus.QUEUED.value,
                    ResourceSearchStatus.RUNNING.value,
                }:
                    task.status = ResourceSearchStatus.CANCELLED.value
                    task.completed_at = now
                    task.updated_at = now
        await db.commit()
        if run.celery_task_id:
            from app.celery import celery_app

            try:
                celery_app.control.revoke(run.celery_task_id, terminate=False)
            except Exception:
                # The persisted cancellation flag remains authoritative and is
                # checked by both the worker and stale-run reconciler.
                pass
    await db.refresh(run)
    return {
        "code": 0,
        "message": (
            "已取消资源搜索"
            if run.status == ResourceSearchStatus.CANCELLED.value
            else "已提交取消请求"
        ),
        "data": _serialize_run(run),
    }


@router.delete("/runs/{run_id}")
async def delete_resource_search_run(
    run_id: int,
    _current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    run = await db.get(ResourceSearchRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="资源搜索批次不存在")
    if run.status in {
        ResourceSearchStatus.QUEUED.value,
        ResourceSearchStatus.RUNNING.value,
    }:
        raise HTTPException(status_code=409, detail="请先取消正在执行的搜索批次")
    await db.delete(run)
    await db.commit()
    return {"code": 0, "message": "资源搜索批次已删除", "data": {"id": run_id}}


@router.get("/runs/{run_id}/results")
async def list_resource_search_results(
    run_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=10, le=100),
    search: str | None = Query(default=None, max_length=255),
    keyword: str | None = Query(default=None, max_length=255),
    review_status: str | None = Query(default=None),
    min_members: int | None = Query(default=None, ge=0),
    _current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if await db.get(ResourceSearchRun, run_id) is None:
        raise HTTPException(status_code=404, detail="资源搜索批次不存在")
    query = select(ResourceSearchResult).where(ResourceSearchResult.run_id == run_id)
    if search and search.strip():
        pattern = f"%{search.strip()}%"
        query = query.where(
            or_(
                ResourceSearchResult.title.ilike(pattern),
                ResourceSearchResult.username.ilike(pattern),
            )
        )
    if keyword and keyword.strip():
        query = query.where(
            ResourceSearchResult.matched_keywords_json.ilike(
                f'%"{keyword.strip()}"%'
            )
        )
    if review_status:
        if review_status not in {item.value for item in ResourceReviewStatus}:
            raise HTTPException(status_code=422, detail="无效的分析状态")
        query = query.where(ResourceSearchResult.review_status == review_status)
    if min_members is not None:
        query = query.where(ResourceSearchResult.member_count >= min_members)

    count_query = select(func.count()).select_from(query.order_by(None).subquery())
    total = (await db.execute(count_query)).scalar_one()
    rows = await db.execute(
        query.order_by(
            ResourceSearchResult.member_count.desc(),
            ResourceSearchResult.id.asc(),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    results = rows.scalars().all()
    account_ids = {
        account_id
        for result in results
        for account_id in _json_list(result.discovered_by_account_ids_json, int)
    }
    account_labels: dict[int, str] = {}
    if account_ids:
        account_rows = await db.execute(
            select(
                TelegramAccount.id,
                TelegramAccount.display_name,
                TelegramAccount.identifier,
            ).where(TelegramAccount.id.in_(account_ids))
        )
        account_labels = {
            int(account_id): display_name or identifier
            for account_id, display_name, identifier in account_rows.all()
        }
    return {
        "code": 0,
        "message": "success",
        "data": [
            _serialize_result(result, account_labels)
            for result in results
        ],
        "total": int(total or 0),
        "page": page,
        "page_size": page_size,
    }


@router.patch("/results/{result_id}")
async def update_resource_search_result(
    result_id: int,
    request: ResourceSearchReviewRequest,
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.get(ResourceSearchResult, result_id)
    if result is None:
        raise HTTPException(status_code=404, detail="资源搜索结果不存在")
    if request.review_status is not None:
        result.review_status = request.review_status
    if request.note is not None:
        result.note = request.note.strip() or None
    result.reviewed_at = datetime.utcnow()
    result.reviewed_by_id = int(current_user["id"])
    await db.commit()
    await db.refresh(result)
    return {
        "code": 0,
        "message": "分析结果已保存",
        "data": _serialize_result(result, {}),
    }
