"""Administrator-only group evidence review endpoints."""
import json

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_admin
from app.modules.acquisition.models import GroupQualificationAudit
from app.modules.acquisition.qualification_service import queue_reviews

router = APIRouter(prefix="/group-reviews", dependencies=[Depends(require_admin)])

class ReviewRequest(BaseModel):
    account_ids: list[int] = Field(min_length=1, max_length=100)

@router.post("", status_code=202)
async def create_reviews(request: ReviewRequest, idempotency_key: str = Header(alias="Idempotency-Key"),
                         db: AsyncSession = Depends(get_db)) -> dict:
    if not 8 <= len(idempotency_key) <= 128 or any(value <= 0 for value in request.account_ids):
        raise HTTPException(422, "Invalid account scope or idempotency key")
    try:
        ids = await queue_reviews(db, sorted(set(request.account_ids)), idempotency_key)
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(409, str(exc)) from exc
    from app.core.scheduler.tasks import group_qualification_task
    group_qualification_task.apply_async(queue="qualification")
    return {"code": 0, "data": {"batch_id": idempotency_key, "count": len(ids), "audit_ids": ids}}

def serialize(row: GroupQualificationAudit) -> dict:
    return {"id": row.id, "batch_id": row.batch_id, "account_id": row.account_id,
            "group_id": row.group_id, "membership_id": row.membership_id,
            "state": row.state, "decision": row.decision, "reason": row.reason,
            "policy_version": row.policy_version, "content_scope": row.content_scope,
            "evidence_hash": row.evidence_hash,
            "checked_at": row.checked_at, "expires_at": row.expires_at,
            "next_retry_at": row.next_retry_at, "attempts": row.attempts,
            "evidence": json.loads(row.evidence_json or "{}")}

@router.get("")
async def list_reviews(account_id: int | None = None, batch_id: str | None = None,
                       limit: int = Query(200, ge=1, le=1000),
                       db: AsyncSession = Depends(get_db)) -> dict:
    statement = select(GroupQualificationAudit)
    if account_id is not None:
        statement = statement.where(GroupQualificationAudit.account_id == account_id)
    if batch_id:
        statement = statement.where(GroupQualificationAudit.batch_id == batch_id)
    rows = (await db.scalars(statement.order_by(desc(GroupQualificationAudit.id)).limit(limit))).all()
    return {"code": 0, "data": [serialize(row) for row in rows]}

@router.get("/{audit_id:int}")
async def get_review(audit_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    row = await db.get(GroupQualificationAudit, audit_id)
    if row is None:
        raise HTTPException(404, "Review not found")
    return {"code": 0, "data": serialize(row)}
