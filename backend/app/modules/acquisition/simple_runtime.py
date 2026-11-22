"""Compatibility adapters for the four-stage growth dispatcher and API."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy import func, select, update

from app.core.account.models import AccountOperationConfig, AccountType, TelegramAccount
from app.core.group.models import Group
from app.core.group.models import GroupAccountMembership as Member
from app.core.settings_models import SystemSetting
from app.modules.acquisition.simple_growth import STAGES, GrowthService, release, result


async def recover_result_journals(db: Any, redis: Any, *, limit: int = 2) -> dict:
    """Commit already observed results even if the account is now paused/offline.

    This bounded dispatcher step never obtains a Telegram session or issues an
    RPC. Account and target locks exclude a still-running result submission.
    """
    from app.modules.acquisition.simple_limits import PREFIX, Deferred, lease
    cursor_key = PREFIX + "result_scan_cursor"
    page_key = PREFIX + "result_scan_pending"
    await release(db)
    keys = json.loads(await redis.get(page_key) or "[]")
    if not keys:
        cursor, keys = await redis.scan(int(await redis.get(cursor_key) or 0),
                                        match=PREFIX + "results:*", count=limit)
        # COUNT is a hint: retain the whole page so a compact hash table cannot
        # repeatedly hide its tail. The dispatcher owns the scan cursor.
        async with redis.pipeline(transaction=True) as pipeline:
            pipeline.set(page_key, json.dumps(keys))
            pipeline.set(cursor_key, cursor)
            await pipeline.execute()
    recovered = 0
    service = GrowthService(SimpleNamespace(db=db, account_pool=None))
    for key in keys[:limit]:
        account_id = int(key.rsplit(":", 1)[1])
        try:
            async with lease(redis, f"account:{account_id}"):
                await service.recover_results(account_id, limit=2)
                recovered += 1
        except Deferred:
            continue
    await redis.set(page_key, json.dumps(keys[limit:]))
    # An interrupted pre-RPC row may have no Redis journal. Visit it even when
    # the account is paused; leases exclude live owners and recovery never sends.
    from app.modules.acquisition.models import AdDeliveryLog
    from app.modules.acquisition.send_result_policy import POLICY
    interrupted = list((await db.scalars(select(AdDeliveryLog.account_id).where(
        AdDeliveryLog.status == "sending", AdDeliveryLog.created_at < datetime.utcnow() - timedelta(minutes=2),
        AdDeliveryLog.qualification_context_json.contains(POLICY),
    ).group_by(AdDeliveryLog.account_id).order_by(func.min(AdDeliveryLog.created_at)).limit(limit))).all())
    await release(db)
    interrupted_recovered = 0
    for account_id in interrupted:
        try:
            async with lease(redis, f"account:{account_id}"):
                await service.recover_results(account_id, limit=2)
                interrupted_recovered += await service.recover_interrupted_ads(account_id)
        except Deferred:
            continue
        finally:
            await release(db)
    return {"result_journals": recovered, "interrupted_attempts": interrupted_recovered}


async def account_sets(db: Any, now: datetime) -> dict:
    from app.core.scheduler.growth_dispatch import eligible_accounts
    configs = list((await db.scalars(select(AccountOperationConfig).join(
        TelegramAccount, TelegramAccount.id == AccountOperationConfig.account_id).where(
        TelegramAccount.account_type == AccountType.PROMOTER,
        AccountOperationConfig.account_id.in_(eligible_accounts(now)), AccountOperationConfig.enabled.is_(True),
        AccountOperationConfig.operation_mode != "ad_only"
    ))).all())
    ids = [c.account_id for c in configs]
    return {"ads": [c.account_id for c in configs if c.auto_ads_enabled],
            "join": [c.account_id for c in configs if c.auto_join_enabled and c.operation_mode != "ad_only"],
            "review_and_verify": ids, "exit": ids}


async def run(service: Any, stage: str, account_id: int | None, *, limit: int = 10, dry_run: bool = False) -> dict:
    from app.core.account.session_exec import PROCESS_ID, forward, owner_id
    from app.core.redis import get_redis
    redis = await get_redis()
    ids = [account_id] if account_id else (await account_sets(service.db, datetime.utcnow()))[stage][:limit]
    combined = {"processed": 0, "succeeded": 0, "failed": 0, "skipped": 0, "details": [], "errors": []}
    for target in ids:
        await release(service.db)
        owner = await owner_id(redis, target)
        if dry_run:
            outcome = result("dry_run", account_id=target)
        elif owner == PROCESS_ID:
            outcome = await GrowthService(service).execute(stage, target)
        elif owner:
            reply = await forward(target, stage)
            outcome = (reply or {}).get("result") or result("session_owner_unreachable")
        else:
            outcome = result("session_owner_required")
        for key in ("processed", "succeeded", "failed", "skipped"):
            combined[key] += outcome.get(key, 0)
        combined["details"].append({"account_id": target, **outcome})
    return combined


def member_payload(member: Member, group: Group) -> dict:
    return {"id": member.id, "membership_id": member.id, "account_id": member.account_id,
            "group_id": group.id, "group_title": group.title, "telegram_group_id": group.group_id,
            "status": member.status, "review_status": member.review_status,
            "can_send_text": member.can_send_text, "verification_status": member.verification_status,
            "reason": member.permission_reason, "checked_at": member.permission_checked_at,
            "next_review_at": member.review_next_at, "next_ad_allowed_at": member.next_ad_allowed_at,
            "ad_failure_streak": member.ad_failure_streak,
            "leave_attempts": member.leave_attempts, "leave_reason": member.leave_error,
            "leave_state": "left" if member.status == "left" else "needs_attention" if member.leave_requested_at and member.leave_attempts >= 3 else "pending" if member.leave_requested_at else "none",
            "ad_status": member.ad_status, "authorization": {"source": "membership_permission", "grant_audit_id": None}}


async def list_members(db: Any, account_id: int | None, *, limit: int = 100, offset: int = 0) -> dict:
    query = select(Member, Group).join(Group, Group.id == Member.group_id)
    if account_id:
        query = query.where(Member.account_id == account_id)
    rows = (await db.execute(query.order_by(Member.id).offset(offset).limit(limit))).all()
    from app.modules.acquisition.models import AdDeliveryLog
    payloads = []
    for member, group in rows:
        payload = member_payload(member, group)
        last = (await db.execute(select(AdDeliveryLog.status, AdDeliveryLog.error).where(AdDeliveryLog.account_id == member.account_id,
            AdDeliveryLog.group_id == member.group_id).order_by(AdDeliveryLog.created_at.desc(), AdDeliveryLog.id.desc()).limit(1))).first()
        payload.update(last_send_outcome=last.status if last else None, last_send_error=last.error if last else None)
        payloads.append(payload)
    return {"code": 0, "mode": "writable_v2", "data": payloads}


async def runtime(db: Any, account_id: int | None) -> dict:
    from app.core.account.rpc_governor import snapshot
    now = datetime.utcnow()
    from app.modules.acquisition.growth_controls import control_snapshot
    controls = await control_snapshot(db, account_id, now) if account_id else None
    readiness = await snapshot(db, account_id, now) if account_id else None
    query = select(Member.review_status, func.count(Member.id)).group_by(Member.review_status)
    if account_id:
        query = query.where(Member.account_id == account_id)
    counts = dict((await db.execute(query)).all())
    due_query = select(func.count(Member.id).filter(Member.next_ad_allowed_at <= now),
                       func.min(Member.next_ad_allowed_at)).where(
        Member.status == "joined", Member.review_status == "approved", Member.ad_status == "active",
        Member.verification_status == "complete",
        Member.leave_requested_at.is_(None))
    if account_id:
        due_query = due_query.where(Member.account_id == account_id)
    due_count, next_ad = (await db.execute(due_query)).one()
    await release(db)
    from app.core.redis import get_redis
    from app.core.scheduler.growth_dispatch import PREFIX as DISPATCH_PREFIX
    stages = []
    schedule_error = None
    try:
        redis = await get_redis()
        for name, seconds in STAGES.items():
            due = await redis.zscore(DISPATCH_PREFIX + name + ":due", str(account_id)) if account_id else None
            stages.append({"name": name, "interval_seconds": seconds,
                           "next_execution_at": datetime.fromtimestamp(due, UTC).replace(tzinfo=None) if due else None})
    except Exception:
        schedule_error = "redis_unavailable"
        stages = [{"name": name, "interval_seconds": seconds, "next_execution_at": None}
                  for name, seconds in STAGES.items()]
    next_execution = min((s["next_execution_at"] for s in stages if s["next_execution_at"]), default=None)
    return {"code": 0, "data": {"mode": "writable_v2", "send_policy": "send_result_v1", "survival_review_enabled": False, "stages": stages,
        "budget": None, "budget_enabled": False, "current_work": None, "controls": controls,
        "readiness": readiness, "checked_at": now,
        "permissions": {"counts": counts, "due": due_count, "next_ad_allowed_at": next_ad},
        "authorization": {"state": "disabled", "grant_audit_id": None},
        "cooldown_reason": schedule_error or (readiness or {}).get("reason"),
        "next_execution_at": next_execution}}


async def enqueue(db: Any, account_ids: list[int], key: str) -> list[int]:
    marker_key = "growth.v2.review_batch." + hashlib.sha256(key.encode()).hexdigest()
    marker = await db.get(SystemSetting, marker_key)
    scope = sorted(set(account_ids))
    if marker:
        stored = json.loads(marker.value)
        if stored["account_ids"] != scope:
            raise ValueError("idempotency_scope_mismatch")
        return stored["membership_ids"]
    members = list((await db.scalars(select(Member).where(Member.account_id.in_(scope),
        Member.status == "joined", Member.ad_status != "paused", Member.leave_requested_at.is_(None)))).all())
    now = datetime.utcnow()
    for member in members:
        member.review_status, member.review_next_at = "pending", now
    ids = [m.id for m in members]
    db.add(SystemSetting(key=marker_key, value=json.dumps({"account_ids": scope, "membership_ids": ids})))
    return ids


async def apply_fact(db: Any, account_id: int, fact: dict) -> None:
    from app.modules.acquisition.qualification_identity import identity_aliases, peer_identity
    kind = fact.get("kind")
    # Text rules and ad-evidence changes no longer revoke text permission.
    if kind == "gap":
        return  # Session protocol recovery handles gaps; normal memberships stay eligible.
    if kind not in {"membership", "verification"}:
        return
    now = datetime.utcnow()
    members = list((await db.scalars(select(Member).where(Member.account_id == account_id,
        Member.telegram_group_id.in_(identity_aliases(peer_identity(fact["peer"]))),
        ).with_for_update(of=Member))).all())
    if not members and kind == "membership" and fact.get("member") is True:
        from app.core.group.identity import is_owned_group_target
        aliases = identity_aliases(peer_identity(fact["peer"]))
        group = await db.scalar(select(Group).where(Group.group_id.in_(aliases)).limit(1))
        # Unknown private approval events can create a new local relationship.
        # They do not guess a previous request or inherit historical authorization.
        if group is None:
            target = int(fact["peer"])
            group = Group(group_id=target, title=str(target), status="active")
            db.add(group)
            await db.flush()
        if not await is_owned_group_target(db, core_group_id=group.id, telegram_group_id=group.group_id):
            member = Member(group_id=group.id, telegram_group_id=group.group_id, account_id=account_id,
                            status="pending", review_status="pending", ad_status="blocked",
                            verification_status="unknown")
            db.add(member)
            members.append(member)
    for member in members:
        paused = member.ad_status == "paused"
        if member.joined_at and float(fact.get("at", 0)) < member.joined_at.replace(tzinfo=UTC).timestamp():
            continue
        if member.left_at and float(fact.get("at", 0)) <= member.left_at.replace(tzinfo=UTC).timestamp():
            continue
        version = fact.get("joined_at")
        if version and member.joined_at and version != member.joined_at.isoformat():
            continue
        if member.status != "joined":
            if fact.get("member") is not True:
                continue
            member.status, member.joined_at, member.left_at = "joined", now, None
            member.verification_status = "unknown"
        if fact.get("member") is False:
            member.status, member.left_at, member.review_status, member.ad_status = "left", now, "rejected", "blocked"
            member.can_send_text, member.permission_checked_at = None, now
        elif (kind != "membership" or fact.get("blocked") is True
              or member.review_status != "approved" or member.verification_status != "complete"):
            member.review_status, member.ad_status, member.can_send_text = "pending", "blocked", None
            member.review_next_at = now
            member.permission_checked_at = now  # Invalidates any permission read already in flight.
            member.permission_reason = "event_permission_check"
            if kind == "verification":
                member.verification_status = "pending"
        if paused:
            member.ad_status = "paused"
        if kind == "membership" and type(fact.get("member")) is bool:
            from app.modules.acquisition.models import AutoJoinAttempt
            # Explicit platform membership events close uncertainty; no polling or RPC.
            observed = datetime.fromtimestamp(float(fact.get("at", 0)), UTC).replace(tzinfo=None)
            await db.execute(update(AutoJoinAttempt).where(
                AutoJoinAttempt.account_id == account_id, AutoJoinAttempt.group_id == member.group_id,
                AutoJoinAttempt.telegram_action_attempted.is_(True),
                AutoJoinAttempt.status.in_(["pending", "unknown"]),
                AutoJoinAttempt.request_sent_at <= observed,
            ).values(status="success" if fact["member"] else "not_member",
                     joined_at=observed if fact["member"] else None,
                     reason="membership_event_confirmed"))
