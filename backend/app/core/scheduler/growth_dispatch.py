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
ACCOUNT_RUN_TTL = RUN_SECONDS + 30
# A pending protected operation prevents new background work from taking the
# account fence while the current operation finishes. It is deliberately
# shorter than the run lease so a crashed waiter cannot strand an account.
ACCOUNT_PRIORITY_TTL = 90
ACCOUNT_BUSY_RETRY_SECONDS = 30
PROTECTED_RETRY_SECONDS = 15
BROKER_CAP = 8
PROTECTED_STAGES = frozenset({"ads", "join"})
SESSION_EXEC_PARALLEL_STAGES = frozenset({"review", "ads", "join"})
SESSION_EXEC_QUEUED_STAGES = PROTECTED_STAGES


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


def account_run_key(account_id: int) -> str:
    """Cross-stage execution fence for one Telegram account.

    Stage tickets are intentionally independent so one slow account cannot
    starve other accounts.  The account operation lease remains the final
    Telegram safety boundary; this shorter scheduler fence prevents review,
    join, verification and ad stages for the same account from being launched
    concurrently and then losing work to lease contention.
    """

    return PREFIX + f"account:{int(account_id)}:run"


def account_priority_key(account_id: int) -> str:
    """Pending protected work marker for one Telegram account."""

    return PREFIX + f"account:{int(account_id)}:priority"


_RELEASE_ACCOUNT_RUN = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""

_CLAIM_ACCOUNT_RUN_WITH_PRIORITY = """
local run_key, priority_key = KEYS[1], KEYS[2]
local token, critical = ARGV[1], ARGV[2] == '1'
local running = redis.call('GET', run_key)
if critical then
    -- If no operation is running, the current protected waiter becomes the
    -- owner of the marker. This replaces a stale marker left by a crashed
    -- waiter and lets the successful claimant clear it immediately.
    if not running then
        redis.call('SET', priority_key, token, 'EX', ARGV[3])
    elseif not redis.call('GET', priority_key) then
        redis.call('SET', priority_key, token, 'EX', ARGV[3])
    end
elseif redis.call('GET', priority_key) then
    return -1
end
if running then return 0 end
redis.call('SET', run_key, token, 'EX', ARGV[4])
return 1
"""

_RELEASE_ACCOUNT_RUN_WITH_PRIORITY = """
local released = 0
if redis.call('GET', KEYS[1]) == ARGV[1] then
    redis.call('DEL', KEYS[1])
    released = 1
end
if redis.call('GET', KEYS[2]) == ARGV[1] then
    redis.call('DEL', KEYS[2])
end
return released
"""


async def claim_account_run(
    client: Any,
    account_id: int,
    token: str,
    *,
    stage: str | None = None,
) -> bool:
    """Claim one account-wide slot while reserving protected work priority.

    Ads and joins publish a short-lived marker before attempting the shared
    account fence. New review/maintenance tickets see that marker and defer,
    so a due protected operation gets the first free slot after the current
    Telegram call completes. The no-stage form is retained for small callers
    and tests that only need the legacy fence.
    """

    if stage is None:
        return bool(
            await client.set(
                account_run_key(account_id),
                token,
                nx=True,
                ex=ACCOUNT_RUN_TTL,
            )
        )
    result = await client.eval(
        _CLAIM_ACCOUNT_RUN_WITH_PRIORITY,
        2,
        account_run_key(account_id),
        account_priority_key(account_id),
        token,
        "1" if stage in PROTECTED_STAGES else "0",
        ACCOUNT_PRIORITY_TTL,
        ACCOUNT_RUN_TTL,
    )
    return int(result) == 1


async def release_account_run(
    client: Any,
    account_id: int,
    token: str,
    *,
    stage: str | None = None,
) -> bool:
    """Release only the execution slot and matching priority marker."""

    if stage is None or stage not in PROTECTED_STAGES:
        return bool(
            await client.eval(
                _RELEASE_ACCOUNT_RUN,
                1,
                account_run_key(account_id),
                token,
            )
        )
    return bool(
        await client.eval(
            _RELEASE_ACCOUNT_RUN_WITH_PRIORITY,
            2,
            account_run_key(account_id),
            account_priority_key(account_id),
            token,
        )
    )


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
    from app.core.account.reconciliation_stream import legacy_account_filter
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

    from app.modules.acquisition.qualification_due import current_clause

    eligible = eligible_accounts(now)
    config = await policy(db, fresh=True)
    reviews = []
    if config.get("enabled") and config.get("execute_reviews", True):
        from app.modules.acquisition.qualification_due import current_clause, due_clause

        reviews = await ids(select(GroupQualificationAudit.account_id).where(
            GroupQualificationAudit.account_id.in_(eligible),
            current_clause(), due_clause(now),
        ))
        if config.get("automatic_identity_registration"):
            from app.modules.acquisition.qualification_system_identity import parse_identity_map
            mapping = parse_identity_map(config.get("system_account_user_ids")) or {}
            reviews = sorted(set(reviews) | set(await ids(eligible.where(
                TelegramAccount.id.not_in(mapping),
            ))))
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
            GroupQualificationAudit.state.in_(["completed", "queued"]),
            GroupQualificationAudit.policy_version == POLICY_VERSION,
            GroupQualificationAudit.content_scope == "text_profile",
            GroupQualificationAudit.decision.in_(["observe", "wait"]),
            current_clause(),
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
            AccountAdBinding.enabled.is_(True), AccountAdBinding.account_id.in_(eligible),
            or_(
                AccountAdBinding.account_id.not_in(select(AccountOperationConfig.account_id).where(
                    AccountOperationConfig.dynamic_capacity_enabled.is_(True),
                )),
                AccountAdBinding.account_id.in_(select(GroupAccountMembership.account_id).where(
                    GroupAccountMembership.status == "joined", GroupAccountMembership.left_at.is_(None),
                    GroupAccountMembership.review_status == "approved", GroupAccountMembership.ad_status == "active",
                )),
            ))),
        "join": joins,
        "review": reviews,
        "exit": sorted(exits),
        "verify": verifications,
        "survival": await ids(select(AdDeliveryLog.account_id).where(
            AdDeliveryLog.account_id.in_(eligible), AdDeliveryLog.status.in_(
                ["success", "pending", "unknown", "sending", "reconciliation_required"]))),
        "reconcile": await ids(select(AutoJoinAttempt.account_id).where(
            AutoJoinAttempt.account_id.in_(eligible),
            legacy_account_filter(AutoJoinAttempt.account_id),
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
        from app.modules.acquisition.qualification_recovery import repair
        repairs = await repair(db, client, datetime.utcfromtimestamp(now))
        available = await account_sets(db, datetime.utcfromtimestamp(now))
        # The P5 kill switch removes the stopped cohort from new ads/join
        # admission; maintenance lanes keep resolving unconfirmed results.
        try:
            from app.modules.acquisition import p5_rollout

            p5_blocked = await p5_rollout.dispatch_blocked_accounts(client)
        except Exception:
            p5_blocked = set()
        if p5_blocked:
            for guarded_stage in ("ads", "join"):
                if available.get(guarded_stage):
                    available[guarded_stage] = [
                        account_id for account_id in available[guarded_stage]
                        if int(account_id) not in p5_blocked
                    ]
        await db.commit()  # No held DB connection while publishing.
        # Rotate maintenance stages too: a full shared queue cannot favor survival forever.
        stage_names = list(STAGES)
        turn = int(await client.incr(PREFIX + "turn")) % len(stage_names)
        stage_names = stage_names[turn:] + stage_names[:turn]
        last_runs: dict[str, Any] = {}
        from app.core.config import settings

        if settings.P4_AD_PACING and available.get("ads"):
            # One bounded read per dispatch tick feeds the pacing decision's
            # last-scheduled tiebreak; nothing else changes while flags are off.
            try:
                last_runs = await client.hgetall(PREFIX + "last_runs")
            except Exception:
                last_runs = {}
        for stage in stage_names:
            spec = STAGES[stage]
            pacing_decision_id: int | None = None
            published_accounts: set[int] = set()
            if available[stage]:
                await client.zadd(PREFIX + stage + ":due", dict.fromkeys(available[stage], now), nx=True)
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
            ordered = sorted(zip(rotated, scores, strict=True), key=lambda pair: pair[1] if pair[1] is not None else now)
            order_accounts = [account_id for account_id, _ in ordered]
            if stage == "ads" and settings.P4_AD_PACING and order_accounts:
                from app.modules.acquisition.ad_fairness_pacing import apply_to_dispatch

                try:
                    order_accounts = await apply_to_dispatch(
                        db, order_accounts, now,
                        window_seconds=spec.interval, last_runs=last_runs,
                    )
                    pacing_decision_id = db.info.pop("p4_last_decision_id", None)
                except Exception:
                    # Pacing is advisory: any decision-layer failure keeps the
                    # existing due-time order for this tick.
                    order_accounts = [account_id for account_id, _ in ordered]
            try:
                for account_id in order_accounts:
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
                    if stage == "ads":
                        published_accounts.add(int(account_id))
            finally:
                if stage == "ads" and pacing_decision_id is not None:
                    try:
                        from app.modules.acquisition.ad_fairness_pacing import (
                            finalize_published_decision,
                        )

                        await finalize_published_decision(db, pacing_decision_id, published_accounts)
                    except Exception:
                        # A publish remains durable even if its audit update fails.
                        await db.rollback()
        await client.set(PREFIX + "last_dispatch", json.dumps({"at": now, "enqueued": enqueued, "accounts": available}))
        return {"enqueued": enqueued, "accounts": {k: len(v) for k, v in available.items()}, "repairs": repairs}
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
    from app.core.config import settings
    p2_kind = "send_ad" if stage == "ads" else "join_group" if stage == "join" else None
    p2_enabled = p2_kind is not None and settings.p2_outbox_enabled_for(
        p2_kind, account_id
    )
    try:
        redis_client = await get_redis()
        owner = await owner_id(redis_client, account_id)
    except Exception:
        owner = None
    if p2_enabled and owner is None:
        # An enabled P2 lane is fail-closed.  A worker without an owner Actor
        # must never fall through to the legacy direct Telegram path.
        return {"processed": 0, "reason": "p2_actor_owner_unavailable", "retryable": True}, 30
    if owner is not None and owner != PROCESS_ID:
        await db.commit()  # The owner may spend minutes reading Telegram.
        reply = await forward(account_id, stage)
        if reply is None:
            return {"processed": 0, "reason": "session_owner_unreachable"}, 30
        if reply.get("error") == "not_owner":
            # The registry entry was stale (owner died); fall through to a
            # local execution attempt against the now-free lease.
            pass
        elif reply.get("error"):
            return {"processed": 0, "reason": "session_owner_execution_failed",
                    "error_type": reply.get("error_type"), "retryable": True}, 30
        else:
            return reply.get("result") or {"processed": 0}, int(reply.get("interval") or 30)
    # P2 rows are consumed only by the session-owning Actor.  Keep this gate
    # after the forwarding branch so a Celery worker can never fall through to
    # a direct Telegram call when a P2 lane is enabled for its account.
    if p2_enabled:
        if account_pool is None:
            return {"processed": 0, "reason": "p2_actor_owner_unavailable", "retryable": True}, 30
        from app.modules.acquisition.p2_dispatch import execute_owner_outbox

        return await execute_owner_outbox(
            db, account_id, p2_kind, account_pool=account_pool
        )
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
    if stage in PROTECTED_STAGES:
        # A ticket published before a P5 hard stop must not execute either;
        # the account retries with a short lease once the stop is cleared.
        try:
            from app.modules.acquisition import p5_rollout

            if await p5_rollout.ticket_blocked(client, stage, account_id):
                await finish(client, stage, account_id, token, now + 60)
                return {
                    "stage": stage,
                    "account_id": account_id,
                    "processed": 0,
                    "skipped": 1,
                    "reason": "p5_rollout_stopped",
                    "retryable": False,
                }
        except Exception:
            pass
    interval = 30
    outcome = "failed"
    result = {}
    account_run_claimed = False
    try:
        # A stage ticket is scoped to a lane.  Guard the account as well so a
        # review and a join (or an ad and a verification) cannot race for the
        # same Telegram client and merely produce an operation-lease skip.
        account_token = f"{stage}:{token}"
        account_run_claimed = await claim_account_run(
            client, account_id, account_token, stage=stage
        )
        if not account_run_claimed:
            result = {
                "processed": 0,
                "skipped": 1,
                "reason": "account_operation_lease_busy",
                "retryable": True,
            }
            interval = (
                PROTECTED_RETRY_SECONDS
                if stage in PROTECTED_STAGES
                else ACCOUNT_BUSY_RETRY_SECONDS
            )
            outcome = "deferred"
            return {"stage": stage, "account_id": account_id, **result}

        async def run(db):
            return await execute(db, stage, account_id)
        result, interval = await _run_with_db(run)
        outcome = "completed"
        return {"stage": stage, "account_id": account_id, **result}
    finally:
        elapsed = time.time() - now
        if account_run_claimed:
            await release_account_run(
                client, account_id, f"{stage}:{token}", stage=stage
            )
        await finish(client, stage, account_id, token, next_due(result, interval))
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


def next_due(result: dict, interval: int, *, now: float | None = None) -> float:
    """Honor explicit business deadlines; malformed dates keep the bounded scan."""
    now = time.time() if now is None else now
    from app.modules.acquisition.evidence_progress import date
    deadlines = []
    retry_deadlines = []
    for item in [result, *(result.get("details") or []), *(result.get("results") or [])]:
        if not isinstance(item, dict):
            continue
        for field in (
            "next_allowed_at",
            "next_join_after",
            "retry_at",
            "next_retry_at",
            "resume_at",
            "next_due_at",
        ):
            value = date(item.get(field))
            if value is not None:
                stamp = (value - datetime(1970, 1, 1)).total_seconds()
                if stamp > now:
                    deadlines.append(stamp)
        # RpcDeferred and admission paths often expose a relative deadline
        # without an absolute timestamp.  Dropping this hint makes a known
        # one-hour wait look like a normal 60-second dispatcher interval and
        # needlessly reclaims the same account ticket over and over.
        for field in ("retry_after_seconds", "retry_after"):
            value = item.get(field)
            if isinstance(value, bool):
                continue
            try:
                seconds = float(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if seconds > 0:
                retry_deadlines.append(now + max(1.0, seconds))
    # After useful work, other groups may already be ready. A single group's
    # deadline must not postpone the whole account after a successful quantum.
    if result.get("succeeded") or result.get("attempted") or any(
        isinstance(item, dict) and item.get("decision") in {"allowed", "trial"}
        for item in result.get("results", [])
    ):
        return now + interval
    deadlines.extend(retry_deadlines)
    return min(deadlines) if deadlines else now + interval


async def snapshot(client: Any) -> dict:
    from app.core.config import settings
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
        "session_owner_concurrency_limit": min(
            settings.SESSION_EXEC_CONCURRENCY,
            max(1, settings.DATABASE_POOL_SIZE + settings.DATABASE_MAX_OVERFLOW - 1),
        ),
        "session_owner_parallel_stages": sorted(SESSION_EXEC_PARALLEL_STAGES),
        "session_owner_priority_stages": sorted(PROTECTED_STAGES),
        "session_owner_priority_reserved_slots": 1,
        "session_owner_queued_stages": sorted(SESSION_EXEC_QUEUED_STAGES),
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
