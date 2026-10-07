"""Bounded account quanta over durable business state, with restart-safe fairness.

Redis holds one outstanding ticket per (stage, account), not copies of business
work. A persistent cursor rotates through all eligible accounts. Queued tickets
and broker lists are both bounded; stale deliveries cannot execute a new ticket.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import or_, select

from app.core.account.models import AccountOperationConfig, TelegramAccount

PREFIX = "vanguard:growth_dispatch:v1:"
TASK = "app.core.scheduler.growth_dispatch.account_quantum_task"
TICK_TASK = "app.core.scheduler.growth_dispatch.dispatch_growth_task"
QUEUED_SECONDS = 120
RUN_SECONDS = 480  # Longer than Celery's hard deadline plus resource cleanup.
BROKER_CAP = 8


@dataclass(frozen=True)
class Stage:
    queue: str
    interval: int
    outstanding: int
    quantum: int


STAGES = {
    "ads": Stage("growth_ads", 60, 4, 1),
    "join": Stage("growth_join", 300, 4, 1),
    "review": Stage("qualification", 30, 4, 2),
    "survival": Stage("growth_maintenance", 60, 2, 2),
    "reconcile": Stage("growth_maintenance", 60, 2, 2),
    "verify": Stage("growth_maintenance", 60, 2, 1),
    "exit": Stage("growth_maintenance", 120, 2, 1),
}


def eligible_accounts(now: datetime):
    """A cheap local filter; execution still revalidates all business authority."""
    return select(TelegramAccount.id).where(
        TelegramAccount.is_active.is_(True),
        TelegramAccount.status.not_in(["banned", "restricted", "error"]),
        TelegramAccount.risk_level.in_(["normal", "watch"]),
        or_(TelegramAccount.spam_check_status.is_(None),
            TelegramAccount.spam_check_status.not_in(["restricted", "flagged"])),
        or_(TelegramAccount.risk_pause_until.is_(None), TelegramAccount.risk_pause_until <= now),
    )


def rotate(account_ids: list[int], cursor: int) -> list[int]:
    ids = sorted(set(account_ids))
    return [i for i in ids if i > cursor] + [i for i in ids if i <= cursor]


def ticket_key(stage: str, account_id: int) -> str:
    return f"{PREFIX}{stage}:ticket:{account_id}"


RESERVE = """
local now, account, token = tonumber(ARGV[1]), ARGV[2], ARGV[3]
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
if redis.call('GET', KEYS[1]) then return 0 end
local due = redis.call('ZSCORE', KEYS[3], account)
if due and tonumber(due) > now then return 0 end
if redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[4]) then return -1 end
redis.call('SET', KEYS[1], 'q:' .. token, 'EX', ARGV[5])
redis.call('ZADD', KEYS[2], now + tonumber(ARGV[5]), account)
redis.call('SET', KEYS[4], account)
return 1
"""

START = """
if redis.call('GET', KEYS[1]) ~= 'q:' .. ARGV[1] then return 0 end
local now = tonumber(ARGV[2])
redis.call('ZREMRANGEBYSCORE', KEYS[3], '-inf', now)
if redis.call('ZCARD', KEYS[3]) >= tonumber(ARGV[5]) then return -1 end
redis.call('SET', KEYS[1], 'r:' .. ARGV[1], 'EX', ARGV[4])
redis.call('ZADD', KEYS[2], now + tonumber(ARGV[4]), ARGV[3])
redis.call('ZADD', KEYS[3], now + tonumber(ARGV[4]), ARGV[1])
return 1
"""

FINISH = """
local value = redis.call('GET', KEYS[1])
if value ~= 'q:' .. ARGV[1] and value ~= 'r:' .. ARGV[1] then return 0 end
redis.call('DEL', KEYS[1])
redis.call('ZREM', KEYS[2], ARGV[2])
redis.call('ZREM', KEYS[3], ARGV[1])
redis.call('ZADD', KEYS[4], ARGV[3], ARGV[2])
return 1
"""

UNLOCK = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""


def active_key(stage: str) -> str:
    return PREFIX + stage + ":outstanding"


def running_key(stage: str) -> str:
    # Capacity belongs to a lane. A shared semaphore alone can starve maintenance
    # whenever all review/join consumers repeatedly acquire its last permits.
    return PREFIX + "running:" + STAGES[stage].queue


async def reserve(client: Any, stage: str, account_id: int, token: str, now: float) -> int:
    return int(await client.eval(
        RESERVE, 4, ticket_key(stage, account_id), active_key(stage),
        PREFIX + stage + ":due", PREFIX + stage + ":cursor",
        now, account_id, token, STAGES[stage].outstanding, QUEUED_SECONDS,
    ))


async def start(client: Any, stage: str, account_id: int, token: str, now: float) -> int:
    return int(await client.eval(
        START, 3, ticket_key(stage, account_id), active_key(stage), running_key(stage),
        token, now, account_id, RUN_SECONDS, 2 if stage == "review" else 1,
    ))


async def finish(client: Any, stage: str, account_id: int, token: str, due: float) -> None:
    await client.eval(
        FINISH, 4, ticket_key(stage, account_id), active_key(stage), running_key(stage),
        PREFIX + stage + ":due", token, account_id, due,
    )


async def broker_depth(client: Any, queue: str) -> int:
    # Kombu's Redis transport uses these four default priority lists.
    return sum([await client.llen(queue + (f"\x06\x16{p}" if p else "")) for p in (0, 3, 6, 9)])


async def account_sets(db: Any, now: datetime) -> dict[str, list[int]]:
    from app.core.automation_settings import get_auto_join_scheduler_settings
    from app.core.group.models import GroupAccountMembership
    from app.modules.acquisition.group_qualification import POLICY_VERSION
    from app.modules.acquisition.join_reconciliation import unresolved_join_request_due
    from app.modules.acquisition.models import (
        AccountAdBinding,
        AdDeliveryLog,
        AutoJoinAttempt,
        GroupAdFrequency,
        GroupAdFrequencyEvent,
        GroupQualificationAudit,
    )
    from app.modules.acquisition.qualification_service import policy

    async def ids(query):
        return list((await db.scalars(query.distinct())).all())

    eligible = eligible_accounts(now)
    members = await ids(select(GroupAccountMembership.account_id).where(
        GroupAccountMembership.account_id.in_(eligible),
        GroupAccountMembership.status.in_(["joined", "pending", "leave_failed"]),
    ))
    config = await policy(db, fresh=True)
    reviews = []
    if config.get("enabled") and config.get("execute_reviews", True):
        # A dispatch ticket represents due work.  Keeping every joined member
        # here forced a fresh database scan on every beat even when all audits
        # were complete or scheduled in the future.
        from sqlalchemy import and_, exists

        due_audit = exists(select(GroupQualificationAudit.id).where(
            GroupQualificationAudit.account_id == GroupAccountMembership.account_id,
            GroupQualificationAudit.membership_id == GroupAccountMembership.id,
            GroupQualificationAudit.state.in_(["queued", "running", "completed"]),
            or_(
                GroupQualificationAudit.state == "queued",
                and_(
                    GroupQualificationAudit.state == "running",
                    GroupQualificationAudit.next_retry_at.is_not(None),
                    GroupQualificationAudit.next_retry_at <= now,
                ),
                and_(
                    GroupQualificationAudit.state == "completed",
                    GroupQualificationAudit.decision.in_(["unknown", "technical_wait"]),
                    GroupQualificationAudit.next_retry_at.is_not(None),
                    GroupQualificationAudit.next_retry_at <= now,
                ),
            ),
        ))
        reviews = await ids(select(GroupAccountMembership.account_id).where(
            GroupAccountMembership.account_id.in_(eligible),
            GroupAccountMembership.status.in_(["joined", "pending", "leave_failed"]),
            due_audit,
        ))
    if config.get("exit_all_accounts") is not True and config.get("account_ids"):
        reviews = [i for i in reviews if i in config["account_ids"]]
    exits: set[int] = set()
    if config.get("enabled") and config.get("execute_exits"):
        # Include both ordinary exits and recovery of stale non-memberships.
        exits.update(await ids(select(GroupAccountMembership.account_id).where(
            GroupAccountMembership.account_id.in_(eligible),
            GroupAccountMembership.review_status.in_(["exit_pending", "leave_failed", "membership_reconciliation"]),
            GroupAccountMembership.review_next_at <= now,
        )))
        # Frequency exits have their own lifecycle and may precede the
        # membership's exit_pending marker. Keep this independent work visible.
        exits.update(await ids(select(GroupAccountMembership.account_id).join(
            AdDeliveryLog, (AdDeliveryLog.group_id == GroupAccountMembership.group_id)
            & (AdDeliveryLog.account_id == GroupAccountMembership.account_id),
        ).join(GroupAdFrequencyEvent, GroupAdFrequencyEvent.log_id == AdDeliveryLog.id).join(
            GroupAdFrequency, GroupAdFrequency.telegram_group_id == GroupAdFrequencyEvent.telegram_group_id,
        ).where(
            GroupAccountMembership.account_id.in_(eligible),
            GroupAccountMembership.status.in_(["joined", "leave_failed"]),
            GroupAdFrequency.status == "exit_pending",
            or_(GroupAccountMembership.leave_retry_at.is_(None), GroupAccountMembership.leave_retry_at <= now),
        )))
        if config.get("exit_all_accounts") is not True:
            exits.intersection_update(config.get("account_ids") or [])
    verifications = []
    if config.get("enabled") and config.get("execute_verification"):
        verifications = await ids(select(GroupQualificationAudit.account_id).join(
            GroupAccountMembership, GroupAccountMembership.id == GroupQualificationAudit.membership_id,
        ).where(
            GroupQualificationAudit.account_id.in_(eligible),
            GroupQualificationAudit.state == "completed",
            GroupQualificationAudit.policy_version == POLICY_VERSION,
            GroupQualificationAudit.content_scope == "text_profile",
            GroupQualificationAudit.decision.in_(["observe", "wait"]),
            GroupAccountMembership.status.in_(["joined", "pending"]),
            GroupAccountMembership.joined_at >= now - timedelta(hours=48),
        ))
        verification_ids = config.get("verification_account_ids", config.get("account_ids"))
        if verification_ids is not None:
            if not isinstance(verification_ids, list) or any(type(i) is not int or i <= 0 for i in verification_ids):
                verifications = []
            else:
                verifications = [i for i in verifications if i in verification_ids]
    joins = await ids(select(AccountOperationConfig.account_id).where(
        AccountOperationConfig.account_id.in_(eligible),
        AccountOperationConfig.enabled.is_(True), AccountOperationConfig.auto_join_enabled.is_(True),
        AccountOperationConfig.operation_mode != "ad_only",
        or_(AccountOperationConfig.next_join_after.is_(None), AccountOperationConfig.next_join_after <= now),
    )) if (await get_auto_join_scheduler_settings(db))["enabled"] else []
    return {
        "ads": await ids(select(AccountAdBinding.account_id).where(
            AccountAdBinding.enabled.is_(True), AccountAdBinding.account_id.in_(eligible))),
        "join": joins,
        "review": reviews,
        "exit": sorted(exits),
        "verify": verifications,
        "survival": await ids(select(AdDeliveryLog.account_id).where(
            AdDeliveryLog.account_id.in_(eligible), AdDeliveryLog.status.in_(
                ["success", "pending", "unknown", "sending", "reconciliation_required"]))),
        "reconcile": await ids(select(AutoJoinAttempt.account_id).where(
            AutoJoinAttempt.account_id.in_(eligible),
            unresolved_join_request_due(now))),
    }


async def recover_cooldowns(db: Any, client: Any) -> None:
    """Local recovery shares the short dispatcher, never the Telegram work lane."""
    from app.core.account.risk_guard import AccountRiskGuard
    now = datetime.utcnow()
    rows = (await db.scalars(select(TelegramAccount).where(
        TelegramAccount.risk_reason == "telegram_read_flood_wait",
        TelegramAccount.risk_pause_until <= now,
    ).with_for_update(skip_locked=True, of=TelegramAccount))).all()
    guard = AccountRiskGuard(db)
    for account in rows:
        await guard._apply_risk_lifecycle(account, now, commit=False)
    await db.commit()
    await client.set("vanguard:runtime_recovery:last_run_at", now.isoformat(), ex=600)


async def dispatch(db: Any, client: Any, broker: Any, publish: Any, *, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    token = uuid4().hex
    key = PREFIX + "dispatch_lock"
    if not await client.set(key, token, nx=True, ex=30):
        return {"enqueued": 0, "reason": "dispatch_inflight"}
    enqueued = 0
    try:
        from app.core.account.backlog_discard import maybe_sweep_ineligible_backlog

        await maybe_sweep_ineligible_backlog(db, client, now=datetime.utcfromtimestamp(now))
        available = await account_sets(db, datetime.utcfromtimestamp(now))
        await db.commit()  # No held DB connection while publishing.
        # Rotate maintenance stages too: a full shared queue cannot favor survival forever.
        stage_names = list(STAGES)
        turn = int(await client.incr(PREFIX + "turn")) % len(stage_names)
        stage_names = stage_names[turn:] + stage_names[:turn]
        for stage in stage_names:
            spec = STAGES[stage]
            if available[stage]:
                await client.zadd(PREFIX + stage + ":due", {i: now for i in available[stage]}, nx=True)
            # Due tickets of accounts that no longer have work in this lane are
            # stale leftovers; they inflate overdue-age reporting forever.  A
            # ticket that is still outstanding re-adds its entry on finish, and
            # returning work re-adds through the zadd above.
            available_set = set(available[stage])
            stale_due = []
            for member in await client.zrange(PREFIX + stage + ":due", 0, -1):
                try:
                    known = int(member) in available_set
                except (TypeError, ValueError):
                    known = False
                if not known:
                    stale_due.append(member)
            if stale_due:
                await client.zrem(PREFIX + stage + ":due", *stale_due)
            cursor = int(await client.get(PREFIX + stage + ":cursor") or 0)
            rotated = rotate(available[stage], cursor)
            scores = await client.zmscore(PREFIX + stage + ":due", rotated) if rotated else []
            # Expired deliveries keep their original due time. Prioritize that
            # age before the cursor so a slow predecessor cannot repeatedly make
            # the same accounts lose their queued lease without ever executing.
            ordered = sorted(zip(rotated, scores), key=lambda pair: pair[1] if pair[1] is not None else now)
            for account_id, _ in ordered:
                if await broker_depth(broker, spec.queue) >= BROKER_CAP:
                    break
                job_token = uuid4().hex
                reserved = await reserve(client, stage, account_id, job_token, now)
                if reserved < 0:
                    break
                if reserved == 0:
                    continue
                try:
                    await publish(stage, account_id, job_token, spec.queue)
                except Exception:
                    await finish(client, stage, account_id, job_token, now + 15)
                    raise
                enqueued += 1
        await client.set(PREFIX + "last_dispatch", json.dumps({"at": now, "enqueued": enqueued, "accounts": available}))
        return {"enqueued": enqueued, "accounts": {k: len(v) for k, v in available.items()}}
    finally:
        await client.eval(UNLOCK, 1, key, token)


async def execute(db: Any, stage: str, account_id: int, *, account_pool: Any = None) -> tuple[dict, int]:
    """Run one account through the original authority, budget and receipt paths.

    When another process owns the account session (the growth listener), the
    whole stage execution is forwarded there; the owner runs this same function
    with its already-connected client, so every one-shot contract applies
    unchanged regardless of which side executes.
    """
    from app.core.account.session_exec import PROCESS_ID, forward, owner_id
    from app.core.redis import get_redis

    account = await db.get(TelegramAccount, account_id, populate_existing=True)
    from app.modules.acquisition.qualification_service import account_block_reason
    reason = account_block_reason(account, datetime.utcnow())
    if reason:
        return {"processed": 0, "reason": reason}, 60
    try:
        redis_client = await get_redis()
        owner = await owner_id(redis_client, account_id)
    except Exception:
        owner = None
    if owner is not None and owner != PROCESS_ID:
        reply = await forward(account_id, stage)
        if reply is None:
            return {"processed": 0, "reason": "session_owner_unreachable"}, 30
        if reply.get("error") == "not_owner":
            # The registry entry was stale (owner died); fall through to a
            # local execution attempt against the now-free lease.
            pass
        else:
            return reply.get("result") or {"processed": 0}, int(reply.get("interval") or 30)
    from app.modules.acquisition.automation import AcquisitionAutomationService
    from app.modules.acquisition.qualification_service import run_reviews
    service = AcquisitionAutomationService(db, account_pool=account_pool)
    spec = STAGES[stage]
    if stage == "ads":
        from app.core.automation_settings import get_ad_delivery_execution_settings
        settings = await get_ad_delivery_execution_settings(db)
        return await service.run_ad_delivery(account_id=account_id, max_deliveries=spec.quantum), max(60, int(settings["dispatcher_interval_seconds"]))
    if stage == "join":
        from app.core.automation_settings import get_auto_join_scheduler_settings
        settings = await get_auto_join_scheduler_settings(db)
        if not settings["enabled"]:
            return {"processed": 0, "reason": "scheduler_disabled"}, spec.interval
        return await service.run_auto_join(account_id=account_id, max_accounts=1, keywords_per_account=1, max_groups_per_keyword=10), max(60, int(settings["scan_interval_minutes"]) * 60)
    if stage == "review":
        return await run_reviews(service, account_id=account_id, limit=spec.quantum), spec.interval
    if stage == "exit":
        from app.modules.acquisition.qualification_actions import run_exits
        return await run_exits(service, account_id=account_id, limit=spec.quantum), spec.interval
    if stage == "verify":
        from app.modules.acquisition.qualification_verification import run_verifications
        return await run_verifications(service, account_id=account_id, limit=spec.quantum), spec.interval
    if stage == "reconcile":
        return await service.reconcile_join_requests(account_id=account_id, limit=spec.quantum), spec.interval
    if stage == "survival":
        return await service.check_ad_survival(account_id=account_id, limit=spec.quantum), spec.interval
    raise ValueError("unknown_growth_stage")


async def run_ticket(stage: str, account_id: int, token: str) -> dict:
    from app.core.redis import get_redis
    from app.core.scheduler.tasks import _run_with_db
    client = await get_redis()
    now = time.time()
    claimed = await start(client, stage, account_id, token, now)
    if claimed == 0:
        return {"reason": "stale_ticket"}
    if claimed < 0:
        await finish(client, stage, account_id, token, now + 15)
        return {"reason": "growth_capacity_wait"}
    interval = 30
    outcome = "failed"
    result = {}
    try:
        async def run(db):
            return await execute(db, stage, account_id)
        result, interval = await _run_with_db(run)
        outcome = "completed"
        return {"stage": stage, "account_id": account_id, **result}
    finally:
        elapsed = time.time() - now
        await finish(client, stage, account_id, token, time.time() + interval)
        details = [item for item in result.get("details", []) if isinstance(item, dict)]
        await client.hset(PREFIX + "last_runs", f"{stage}:{account_id}", json.dumps({
            "at": time.time(), "seconds": round(elapsed, 3), "outcome": outcome,
            "processed": result.get("processed", result.get("checked", 0)),
            "succeeded": result.get("succeeded", 0), "skipped": result.get("skipped", 0),
            "failed": result.get("failed", 0),
            "sent": result.get("succeeded", 0) if stage == "ads" else 0,
            "joined": sum(item.get("action") in {"joined", "joined_pending_review"} for item in details)
                      if stage == "join" else 0,
            "reason": result.get("reason") or next((item["reason"] for item in details if item.get("reason")), None),
        }))


async def snapshot(client: Any) -> dict:
    now = time.time()
    async def optional_call(name: str, default: Any, *args, **kwargs):
        method = getattr(client, name, None)
        if not callable(method):
            return default
        try:
            return await method(*args, **kwargs)
        except (AttributeError, TypeError):
            # Health reporting must remain available while Redis is degraded
            # or represented by a minimal test client.  A metrics probe must
            # never turn into a scheduler failure.
            return default

    last = json.loads(await optional_call("get", "{}", PREFIX + "last_dispatch") or "{}")
    lanes = {}

    for stage, spec in STAGES.items():
        eligible = set((last.get("accounts") or {}).get(stage, []))
        overdue = await optional_call(
            "zrangebyscore", [], PREFIX + stage + ":due", "-inf", now, withscores=True
        )
        ages = [now - due for account, due in overdue if int(account) in eligible]
        lanes[stage] = {
            "eligible_accounts": len(eligible), "waiting_accounts": len(ages),
            "oldest_dispatch_wait_seconds": round(max(ages, default=0), 1),
            "outstanding": await optional_call("zcount", 0, active_key(stage), now, "+inf"),
            "outstanding_limit": spec.outstanding, "queue": spec.queue,
        }
    return {
        "last_dispatch_at": last.get("at"), "lanes": lanes,
        "concurrency_limit": 5, "broker_queue_limit": BROKER_CAP,
        "last_runs": {
            key: json.loads(value)
            for key, value in (await optional_call("hgetall", {}, PREFIX + "last_runs")).items()
        },
    }


# Imported by Celery in both beat and workers; decorators do not acquire resources.
from app.celery import celery_app  # noqa: E402


@celery_app.task(name=TASK, time_limit=360, soft_time_limit=330)
def account_quantum_task(stage: str, account_id: int, token: str) -> dict:
    from app.core.scheduler.tasks import _run_async
    if stage not in STAGES:
        raise ValueError("unknown_growth_stage")
    return _run_async(run_ticket(stage, account_id, token))


@celery_app.task(name=TICK_TASK, time_limit=30, soft_time_limit=25)
def dispatch_growth_task() -> dict:
    from redis.asyncio import Redis
    from app.core.config import settings
    from app.core.redis import get_redis
    from app.core.scheduler.tasks import _run_async, _run_with_db

    async def run(db):
        client = await get_redis()
        await recover_cooldowns(db, client)
        broker = Redis.from_url(settings.CELERY_BROKER_URL, decode_responses=True)
        try:
            async def publish(stage, account_id, token, queue):
                account_quantum_task.apply_async(args=[stage, account_id, token], queue=queue, expires=QUEUED_SECONDS)
            return await dispatch(db, client, broker, publish)
        finally:
            await broker.aclose()
    return _run_async(_run_with_db(run))
