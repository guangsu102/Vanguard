"""Local readiness and separate business-maintenance projection; no Telegram I/O."""
from datetime import datetime
from typing import Any
from sqlalchemy import select

async def _maintenance(db: Any, account_id: int) -> dict:
    from app.modules.acquisition.models import QualificationWork
    try:
        async with db.begin_nested():
            rows=(await db.scalars(select(QualificationWork).where(
                QualificationWork.account_id==account_id,QualificationWork.family=="business_maintenance",
            ).order_by(QualificationWork.id.desc()).limit(101))).all()
        return {"available":True,"truncated":len(rows)>100,"limit":100,
            "items":[{"id":row.id,"group_id":row.group_id,"state":row.state,"reason":row.reason,
                      "next_step_reads":row.next_step_reads,"ready_at":row.ready_at,"updated_at":row.updated_at}
                     for row in rows[:100]],"blocks_qualification":False,"retry_is_prediction":True}
    except Exception:
        return {"available":False,"reason":"maintenance_snapshot_unavailable","items":[],
                "blocks_qualification":False,"retry_is_prediction":True}

async def overview(db: Any, account_id: int, now: datetime) -> dict:
    from app.core.account.models import AccountOperationConfig, TelegramAccount
    from app.modules.acquisition.capacity import executable_inventory
    empty={"available":False,"reason":"readiness_snapshot_unavailable","ad_targets":None,
           "join_candidates":None,"blocker_counts":None,"maintenance":{"available":False,"items":[]}}
    try:
        async with db.begin_nested():
            account=await db.get(TelegramAccount,account_id)
            config=await db.scalar(select(AccountOperationConfig).where(AccountOperationConfig.account_id==account_id))
            if account is None or config is None:
                return {**empty,"reason":"account_configuration_missing"}
            # Reuse exactly the local dry-run gates already used by scheduling.
            # This makes no reservation and constructs no Telegram client.
            from sqlalchemy.ext.asyncio import AsyncSession
            from app.modules.acquisition.capacity_reads import CapacityReads
            reader=CapacityReads(db) if isinstance(db,AsyncSession) else db
            inventory=await executable_inventory(reader,account_id,config,now,include_candidates=True)
            from app.modules.acquisition.qualification_service import account_block_reason
            account_reason=account_block_reason(account,now)
            blockers=dict(inventory.get("blocker_counts") or {})
            if account_reason:blockers[account_reason]=max(1,blockers.get(account_reason,0))
            if not config.enabled:blockers["account_operation_disabled"]=1
            if not config.auto_join_enabled:blockers["account_auto_join_disabled"]=1
            result={"available":True,"observed_at":now,"source":"existing_local_execution_admission",
                "ad_targets":0 if account_reason or not config.enabled else inventory.get("ad_targets"),
                "join_candidates":inventory.get("join_candidates"),
                "join_candidates_total":inventory.get("join_candidates_total"),
                "join_candidates_preview_pending":inventory.get("join_candidates_preview_pending"),
                "join_candidates_pending_identity":inventory.get("join_candidates_pending_identity"),
                "shared_candidate_pool_available":inventory.get("shared_candidate_pool_available"),
                "sample_complete":inventory.get("sample_complete") is True,
                "blocker_counts":blockers,"live_boundary_recheck_required":True,
                "does_not_reserve_or_read_telegram":True}
    except Exception:
        return empty
    result["maintenance"]=await _maintenance(db,account_id)
    return result
