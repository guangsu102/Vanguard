"""Administrator-only group evidence review endpoints."""
import asyncio
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_admin
from app.core.settings_models import SystemSetting
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
        marker = await db.get(SystemSetting, "qualification.batch." + hashlib.sha256(idempotency_key.encode()).hexdigest())
        work_ids = _payload(marker.value if marker else None).get("work_ids") or []
        work_ids = [value for value in work_ids if type(value) is int and value > 0]
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(409, str(exc)) from exc
    from app.core.config import settings
    if settings.GROWTH_SIMPLIFIED_ENABLED:
        from app.core.scheduler.growth_dispatch import wake_account
        for account_id in sorted(set(request.account_ids)):
            await wake_account(account_id, stages=("review_and_verify",))
    else:
        from app.core.scheduler.tasks import group_qualification_task
        group_qualification_task.apply_async(queue="qualification")
    return {"code": 0, "data": {"batch_id": idempotency_key, "count": len(ids) + len(work_ids),
            "audit_ids": [] if settings.GROWTH_SIMPLIFIED_ENABLED else ids,
            "membership_ids": ids if settings.GROWTH_SIMPLIFIED_ENABLED else [], "work_ids": work_ids}}

def _payload(value: str | None) -> dict:
    try:
        result = json.loads(value or "{}")
        return result if isinstance(result, dict) else {}
    except (TypeError, ValueError):
        return {}


def review_status(row: GroupQualificationAudit, evidence: dict, wait: dict,
                  event: dict | None = None, *, now: datetime | None = None) -> dict:
    """Operational stages never alter the underlying qualification verdict."""
    from app.modules.acquisition.evidence_progress import date
    from app.modules.acquisition.group_qualification import looks_like_ad
    from app.modules.acquisition.review_waits import BUDGET_WAIT_REASONS

    now = now or datetime.utcnow()
    pending = evidence.get("pending_collection")
    material = pending if isinstance(pending, dict) and pending else evidence
    cost = evidence.get("read_cost")
    cost = cost if isinstance(cost, dict) else {}
    progress = material.get("collection_progress")
    progress = progress if isinstance(progress, dict) else {}
    maturity = []
    for item in material.get("evidence", []):
        if not isinstance(item, dict):
            continue
        if (item.get("source") != "recent_promotional_message"
                or item.get("sender_role") != "ordinary" or item.get("system_account")
                or item.get("topic_id") is not None or item.get("accessible") is not True
                or item.get("warning_reply_ids") or not looks_like_ad(str(item.get("text") or ""))):
            continue
        posted, edited = date(item.get("date")), date(item.get("edited_at"))
        if posted is not None and posted <= now:
            maturity.append(max(posted, edited or posted) + timedelta(hours=24))
    mature_at = date(material.get("next_evidence_maturity_at")) or min(maturity, default=None)
    future = row.next_retry_at is not None and row.next_retry_at > now
    current_wait = bool(future and date(wait.get("retry_at")) == row.next_retry_at)
    exhausted = evidence.get("technical_recovery_exhausted") is True
    if (wait.get("reason") in BUDGET_WAIT_REASONS and future
            and date(wait.get("retry_at")) == row.next_retry_at):
        stage = "budget_wait"
    elif row.state in {"waiting_ai", "reviewing_ai"} or material.get("ai_pending"):
        stage = "ai_wait"
    elif any(item.get("outcome") == "unknown" for item in evidence.get("evidence_handoffs", [])
             if isinstance(item, dict)) and evidence.get("collection_needs_refresh"):
        stage = "storage_fallback"
    elif row.decision == "technical_wait" and exhausted and row.next_retry_at is None:
        stage = "technical_evidence_wait"
    elif mature_at is not None and mature_at > now and row.decision in {"observe", "wait", "unknown"}:
        stage = "maturity_wait"
    elif event:
        stage = "event_recheck"
    elif progress.get("pending_message_ids") or material.get("local_rpc_defer_reason"):
        stage = "collection_continuation"
    elif row.decision == "technical_wait":
        stage = "technical_wait"
    elif row.state in {"queued", "running"} or row.checked_at is None:
        stage = "initial_review"
    elif row.decision in {"observe", "wait"}:
        stage = "evidence_wait"
    else:
        stage = "complete"
    return {
        "stage": stage, "retry_at": row.next_retry_at, "mature_at": mature_at,
        "required_reads": wait.get("required_reads") if current_wait else None,
        "actual_reads": cost.get("entered_sdk_reads") if type(cost.get("entered_sdk_reads")) is int else None,
        "budget_attempted_reads": cost.get("reads") if type(cost.get("reads")) is int else None,
        "reserved_reads": cost.get("reserved_reads") if type(cost.get("reserved_reads")) is int else None,
        "unknown_count": len(material.get("unknowns") or []),
        "effective_unknowns": list(material.get("unknowns") or []),
        "pending_message_count": len(progress.get("pending_message_ids") or []),
        "not_evaluated_message_count": len(progress.get("not_evaluated_message_ids") or []),
        "technical_recovery_exhausted": exhausted,
        "event_pending": bool(event),
        "waiting_reason": wait.get("reason") if current_wait else (
            "new_evidence_required" if exhausted else material.get("local_rpc_defer_reason")),
        "rules_evaluation_status": material.get("rules_evaluation_status"),
        "history_evaluation_status": material.get("history_evaluation_status"),
    }


async def sample_summary(account_id: int | None, now: datetime) -> dict:
    """Read two exact, existing bounded lists; these samples are not totals."""
    from app.core.account.read_work import RETENTION_SECONDS, sample_key
    from app.core.redis import get_redis

    result = {"available": False, "source": "read_work_samples", "partial": True,
              "per_workload_limit": 200, "retention_seconds": RETENTION_SECONDS,
              "actual_reads": None, "budget_charged_reads": None, "known_entered_sdk_reads": 0,
              "sdk_unknown_work_units": 0, "work_units": 0, "by_workload": [], "methods": [],
              "methods_measurement": "budget_charged_attempts"}
    if account_id is None:
        result["reason"] = "select_account"
        return result
    try:
        async with asyncio.timeout(3):
            cache = await get_redis()
            groups = await asyncio.gather(*(
                cache.lrange(sample_key(account_id, kind), 0, 199)
                for kind in ("review", "renewal")
            ))
        current = now.replace(tzinfo=UTC).timestamp()
        methods = Counter()
        result.update(available=True, budget_charged_reads=0)
        for kind, raw_items in zip(("review", "renewal"), groups, strict=True):
            reads, count, entered, unknown = 0, 0, 0, 0
            for raw in raw_items[:200]:
                item = _payload(raw)
                amount, at = item.get("reads"), item.get("at")
                if (type(amount) is not int or amount < 0 or type(at) not in (int, float)
                        or not current - RETENTION_SECONDS <= at <= current):
                    continue
                reads += amount
                count += 1
                sdk = item.get("entered_sdk_reads")
                if type(sdk) is int and sdk >= 0:
                    entered += sdk
                else:
                    unknown += 1
                values = item.get("methods")
                if isinstance(values, dict):
                    for method, value in values.items():
                        if len(method) <= 96 and type(value) is int and value >= 0:
                            methods[method] += value
            result["budget_charged_reads"] += reads
            result["known_entered_sdk_reads"] += entered
            result["sdk_unknown_work_units"] += unknown
            result["work_units"] += count
            result["by_workload"].append({"workload": kind, "work_units": count,
                                           "budget_charged_reads": reads,
                                           "actual_reads": entered if count and not unknown else None})
        if result["work_units"] and not result["sdk_unknown_work_units"]:
            result["actual_reads"] = result["known_entered_sdk_reads"]
        result["methods"] = [{"method": name, "reads": count}
                             for name, count in methods.most_common(20)]
    except Exception:
        result["reason"] = "read_work_samples_unavailable"
    return result


def serialize(row: GroupQualificationAudit, *, wait: dict | None = None,
              event: dict | None = None, operational: dict | None = None) -> dict:
    evidence, wait = _payload(row.evidence_json), wait or {}
    return {"id": row.id, "batch_id": row.batch_id, "account_id": row.account_id,
            "group_id": row.group_id, "membership_id": row.membership_id,
            "state": row.state, "decision": row.decision, "reason": row.reason,
            "policy_version": row.policy_version, "content_scope": row.content_scope,
            "evidence_hash": row.evidence_hash,
            "checked_at": row.checked_at, "expires_at": row.expires_at,
            "next_retry_at": row.next_retry_at, "attempts": row.attempts,
            "evidence": evidence, "wait": wait,
            "read_status": review_status(row, evidence, wait, event),
            "authorization": (operational or {}).get("authorization"),
            "job": (operational or {}).get("job"),
            "blocker": (operational or {}).get("blocker")}

def efficiency_enabled(account_id: int | None) -> bool:
    if account_id is None:
        return False
    from app.modules.acquisition.qualification_engine import enabled_for
    return enabled_for(account_id)


async def operational_states(db: AsyncSession, rows: list) -> dict:
    """Read work/grant state without adopting grants, enqueuing work or touching Telegram."""
    from app.modules.acquisition.qualification_engine import enabled_for, runtime_for_memberships
    selected = [row for row in rows if enabled_for(row.account_id)]
    if not selected:
        return {}
    try:
        async with db.begin_nested():
            return await runtime_for_memberships(db, selected)
    except Exception:
        return {(getattr(row, "membership_id", None) or row.id): {"authorization": {"state": "unavailable"}, "job": None,
                "blocker": {"code": "runtime_unavailable", "observed_at": datetime.utcnow()}}
                for row in selected}


async def flow_summary(db: AsyncSession, account_id: int | None, now: datetime) -> dict:
    empty = {"authorization": None, "job": None, "blocker": None,
             "current_work": {"available": False, "items": [], "reason": "select_enabled_account"},
             "business": {"available": False, "reason": "select_enabled_account"}}
    if not efficiency_enabled(account_id):
        return empty
    from app.core.account.models import AccountOutboundAttempt as Outbound
    from app.core.group.models import GroupAccountMembership as Membership
    from app.modules.acquisition.models import AutoJoinAttempt as Join

    start = now - timedelta(hours=24)
    try:
        async with db.begin_nested():
            rows = (await db.scalars(select(Membership).where(
                Membership.account_id == account_id, Membership.status == "joined",
                Membership.left_at.is_(None),
            ).order_by(Membership.id).limit(501))).all()
            state = await operational_states(db, rows[:500])
            values = list(state.values())
            summary = {key: {"available": True, "scope": "joined_memberships", "truncated": len(rows) > 500,
                             "by_state" if key != "blocker" else "by_code": dict(Counter(
                                 value[key].get("state" if key != "blocker" else "code", "unknown")
                                 for value in values if isinstance(value.get(key), dict)))}
                       for key in ("authorization", "job", "blocker")}
            summary["current_work"] = {"available": True, "truncated": len(rows) > 500, "limit": 500,
                "items": [{"membership_id": member.id, "account_id": member.account_id,
                           "group_id": member.group_id, **state[member.id]}
                          for member in rows[:500] if member.id in state]}
            ads = (await db.execute(select(
                func.count(Outbound.id).label("attempted"),
                func.count(Outbound.id).filter(Outbound.state == "succeeded", Outbound.message_id > 0).label("succeeded"),
                func.count(Outbound.id).filter(Outbound.state == "unknown").label("unknown"),
            ).where(Outbound.account_id == account_id, Outbound.category == "ad",
                    Outbound.attempted_at >= start, Outbound.attempted_at < now))).mappings().one()
            join_values, join_columns, join_windows = {}, [], []
            for name, column_name, condition in (
                ("intent", "request_sent_at", Join.telegram_action_attempted.is_(True)),
                ("sdk_entered", "sdk_entered_at", None),
                ("platform_responded", "platform_responded_at", None),
                ("membership_confirmed", "joined_at", Join.status == "success"),
            ):
                column = getattr(Join, column_name, None)
                if column is None:
                    join_values[name] = None
                    continue
                window = and_(column >= start, column < now)
                join_windows.append(window)
                if condition is not None:
                    window = and_(window, condition)
                join_columns.append(func.count(Join.id).filter(window).label(name))
            intended = and_(Join.request_sent_at >= start, Join.request_sent_at < now,
                            Join.telegram_action_attempted.is_(True))
            for name, column_name in (("sdk_entry_unrecorded", "sdk_entered_at"),
                                      ("platform_response_unrecorded", "platform_responded_at")):
                column = getattr(Join, column_name, None)
                if column is None:
                    join_values[name] = None
                else:
                    join_columns.append(func.count(Join.id).filter(intended, column.is_(None)).label(name))
            join_columns.append(func.count(Join.id).filter(
                Join.request_state == "outcome_unknown", intended).label("unknown"))
            joins = (await db.execute(select(*join_columns).where(
                Join.account_id == account_id, or_(*join_windows),
            ))).mappings().one()
            join_values.update(dict(joins))
            summary["business"] = {"available": True, "window_start": start, "window_end": now,
                "ads": dict(ads), "joins": join_values,
                "join_stage_coverage": "recorded_facts_only;historical_null_not_inferred"}
            from app.modules.acquisition.ad_opportunities import summary as opportunity_summary
            summary["business"]["ad_opportunities"] = await opportunity_summary(db, account_id, now)
            summary["business"]["ads_measurement"] = "outbound_receipts;not_due_opportunity_completion"
            from app.modules.acquisition.growth_efficiency_overview import overview
            summary["business"]["readiness"] = await overview(db, account_id, now)
            return summary
    except Exception:
        return {**empty, "business": {"available": False, "reason": "business_snapshot_unavailable"}}


@router.get("")
async def list_reviews(account_id: int | None = None, batch_id: str | None = None,
                       limit: int = Query(200, ge=1, le=1000),
                       db: AsyncSession = Depends(get_db)) -> dict:
    from app.core.config import settings
    if settings.GROWTH_SIMPLIFIED_ENABLED:
        from app.modules.acquisition.simple_runtime import list_members
        return await list_members(db, account_id, limit=limit)
    statement = select(GroupQualificationAudit)
    if account_id is not None:
        statement = statement.where(GroupQualificationAudit.account_id == account_id)
    if batch_id:
        statement = statement.where(GroupQualificationAudit.batch_id == batch_id)
    rows = (await db.scalars(statement.order_by(desc(GroupQualificationAudit.id)).limit(limit))).all()
    keys = [f"qualification.wait.{row.id}" for row in rows]
    keys += [f"qualification.event.{row.membership_id}" for row in rows]
    keys += [f"qualification.continuity.{row.account_id}" for row in rows]
    observations = dict((await db.execute(select(SystemSetting.key, SystemSetting.value).where(
        SystemSetting.key.in_(keys)
    ))).all()) if rows else {}
    from app.modules.acquisition.qualification_events import pending_value

    operational = await operational_states(db, rows)
    return {"code": 0, "data": [serialize(
        row, operational=operational.get(row.membership_id), wait=_payload(observations.get(f"qualification.wait.{row.id}")),
        event=pending_value(observations.get(f"qualification.event.{row.membership_id}"),
                            row.membership_joined_at,
                            observations.get(f"qualification.continuity.{row.account_id}")),
    ) for row in rows]}


def project_work_budget(flow: dict, budget: dict | None, now: datetime) -> None:
    """Project the already collected purpose snapshot with no per-row I/O."""
    for item in flow.get("current_work", {}).get("items", []):
        job = item.get("job") or {}
        if job.get("state") != "WAIT_BUDGET":
            continue
        renewal = job.get("kind") in {"renewal", "ad_qualification_refresh"}
        kind, purpose = ("renewal", "ad_qualification_refresh") if renewal else ("review", "group_qualification")
        current = (budget or {}).get(kind) or {}
        remaining = current.get("remaining")
        known = (budget or {}).get("state") != "unavailable" and type(remaining) is int and remaining >= 0
        available = (0 if (budget or {}).get("state") == "cooldown" else remaining) if known else None
        blocker = item.setdefault("blocker", {})
        if blocker is None:
            blocker = item["blocker"] = {}
        names = ["minute", "hour", "day", *(f"ad_{window}" if renewal else f"critical_{window}" for window in ("hour", "day"))]
        if not renewal:
            names += ["critical_review_hour", "critical_review_day"]
        windows = (budget or {}).get("windows") or {}
        blocker.update(available_reads=available, observed_at=now, budget_purpose=purpose,
            budget_windows={name: windows[name] for name in names if name in windows},
            retry_is_prediction=True)
        delay = current.get("retry_after_seconds")
        if known and type(delay) is int and delay >= 0:
            predicted = now+timedelta(seconds=delay)
            existing = blocker.get("retry_at")
            blocker["retry_at"] = max(predicted, existing) if isinstance(existing, datetime) else predicted


@router.get("/runtime")
async def read_runtime(account_id: int | None = Query(None, ge=1),
                       db: AsyncSession = Depends(get_db)) -> dict:
    """Read existing finite ledgers; do not create another metrics/event store."""
    from app.core.config import settings
    if settings.GROWTH_SIMPLIFIED_ENABLED:
        from app.modules.acquisition.simple_runtime import runtime
        return await runtime(db, account_id)
    from app.core.account.read_metrics import snapshot as measurement_snapshot
    from app.core.config import settings
    from app.modules.acquisition.review_read_runtime import enabled_for, evidence_status

    now = datetime.utcnow()
    start = now - timedelta(hours=24)
    metrics = {"enabled": settings.BUDGET_LEDGER_ENABLED,
               "source": "read_work_ledger", "window_start": start, "window_end": now,
               "selection": "created_in_window", "row_limit": 1000, "truncated": False,
               "actual_reads": None, "budget_charged_reads": None, "known_budget_charged_reads": 0,
               "coverage": "unavailable", "reason": "ledger_disabled",
               "settled_work_units": 0, "inflight_work_units": 0,
               "unknown_consumption_work_units": 0, "by_workload": []}
    if settings.BUDGET_LEDGER_ENABLED:
        from app.core.account.control_models import AccountBudgetReservation as Ledger

        query = select(Ledger.workload, Ledger.state, Ledger.consumed_reads, Ledger.reason).where(
            Ledger.created_at >= start, Ledger.created_at <= now,
            Ledger.workload.in_(["review", "renewal"]),
        )
        if account_id is not None:
            query = query.where(Ledger.account_id == account_id)
        rows = (await db.execute(query.order_by(desc(Ledger.created_at), Ledger.reservation_id).limit(1001))).all()
        metrics["truncated"] = len(rows) > 1000
        grouped = {}
        for kind, state, consumed, reason in rows[:1000]:
            if state == "reserved":
                metrics["inflight_work_units"] += 1
                continue
            unknown = reason == "consumption_evidence_missing"
            actual = 0 if unknown else consumed
            metrics["known_budget_charged_reads"] += actual
            metrics["settled_work_units"] += 1
            metrics["unknown_consumption_work_units"] += int(unknown)
            item = grouped.setdefault((kind, state), {"workload": kind, "state": state,
                                                     "work_units": 0, "actual_reads": 0})
            item["work_units"] += 1
            item["actual_reads"] += actual
        metrics["by_workload"] = list(grouped.values())
        if not rows:
            metrics["reason"] = "no_matching_ledger_records"
        elif metrics["unknown_consumption_work_units"] or metrics["truncated"]:
            metrics.update(coverage="partial", reason="ledger_consumption_incomplete")
        elif metrics["settled_work_units"]:
            metrics.update(coverage="settled_selection", reason=None,
                           budget_charged_reads=metrics["known_budget_charged_reads"])
        else:
            metrics.update(coverage="inflight_only", reason="no_settled_ledger_records")
    budget = None
    if account_id is not None:
        from app.core.account.critical_fairness import purpose_budget
        from app.core.account.rpc_governor import snapshot

        try:
            async with asyncio.timeout(3):
                current = await snapshot(db, account_id, now)
            budget = {"state": current["state"], "reason": current.get("reason"),
                      "review": purpose_budget(current, "group_qualification"),
                      "renewal": purpose_budget(current, "ad_qualification_refresh"),
                      "minute": current.get("usage", {}).get("minute"),
                      "windows": current.get("usage", {}), "observed_at": now}
            if current.get("budget_enabled") is False:
                budget = {"state": "disabled", "enabled": False, "reason": "rpc_pacing_enabled",
                          "review": None, "renewal": None, "minute": None, "windows": None,
                          "observed_at": now}
        except Exception:
            budget = {"state": "unavailable", "reason": "budget_snapshot_unavailable"}
    from app.modules.acquisition.review_storage_budget import verify as verify_component_storage
    component_storage = await asyncio.to_thread(verify_component_storage, {
        "evidence": settings.REVIEW_EVIDENCE_STORE_PATH,
        "metrics": settings.REVIEW_READ_METRICS_PATH,
        "diagnostics": settings.REVIEW_DIAGNOSTICS_PATH,
    })
    measurement = await measurement_snapshot(account_id)
    flow = await flow_summary(db, account_id, now)
    project_work_budget(flow, budget, now)
    from app.modules.acquisition.qualification_telemetry import snapshot as workflow_telemetry
    telemetry = await workflow_telemetry(account_id, now, sdk_coverage=measurement) if account_id is not None else {}
    return {"code": 0, "data": {
        **flow,
        "efficiency": {**telemetry, "available": measurement.get("available", False),
                       "complete": measurement.get("complete", False),
                       "reason": measurement.get("reason"),
                       "entered_sdk": measurement.get("actual_reads"),
                       "known_entered_sdk": measurement.get("known_entered_sdk"),
                       "gap_hours": measurement.get("gap_hours", []),
                       "diagnostics": measurement.get("diagnostics"),
                       "net_savings": None},
        "account_id": account_id, "optimization_enabled": enabled_for(account_id) if account_id is not None else None,
        "metrics": metrics, "samples": await sample_summary(account_id, now),
        "measurement": measurement,
        "budget": budget, "storage": await evidence_status(account_id),
        "component_storage": component_storage,
    }}

@router.get("/{audit_id:int}")
async def get_review(audit_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    from app.core.config import settings
    if settings.GROWTH_SIMPLIFIED_ENABLED:
        from app.core.group.models import Group, GroupAccountMembership
        from app.modules.acquisition.simple_runtime import member_payload
        member = await db.get(GroupAccountMembership, audit_id)
        group = await db.get(Group, member.group_id) if member else None
        if member is None or group is None:
            raise HTTPException(404, "Membership not found")
        return {"code": 0, "mode": "writable_v2", "data": member_payload(member, group)}
    row = await db.get(GroupQualificationAudit, audit_id)
    if row is None:
        raise HTTPException(404, "Review not found")
    wait = await db.get(SystemSetting, f"qualification.wait.{row.id}")
    event = await db.get(SystemSetting, f"qualification.event.{row.membership_id}")
    marker = await db.get(SystemSetting, f"qualification.continuity.{row.account_id}")
    from app.modules.acquisition.qualification_events import pending_value

    operational = await operational_states(db, [row])
    return {"code": 0, "data": serialize(
        row, operational=operational.get(row.membership_id), wait=_payload(wait.value if wait else None),
        event=pending_value(event.value if event else None, row.membership_joined_at,
                            marker.value if marker else None),
    )}
