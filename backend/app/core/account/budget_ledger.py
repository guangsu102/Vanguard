"""Durable work-unit accounting; the Redis allocator alone enforces limits."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update

from app.core.account.control_models import AccountBudgetReservation, AccountBudgetSnapshot
from app.core.config import settings

CONSUMED_PREFIX = "vanguard:budget:consumed:"
SNAPSHOT_AT = "vanguard:budget:snapshot_at:v1"


def conflict_insert(db: Any, model: Any) -> Any:
    """PostgreSQL in production; SQLite only for isolated unit tests."""
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert(model)


async def reserve(
    db: Any,
    reservation_id: str,
    *,
    account_id: int,
    workload: str,
    lane: str,
    reserved_reads: int,
    lease_expires_at: datetime,
    now: datetime | None = None,
) -> AccountBudgetReservation:
    now = now or datetime.utcnow()
    await db.execute(
        conflict_insert(db, AccountBudgetReservation)
        .values(
            reservation_id=reservation_id,
            account_id=account_id,
            workload=workload,
            lane=lane,
            window_start=now.replace(hour=0, minute=0, second=0, microsecond=0),
            reserved_reads=reserved_reads,
            consumed_reads=0,
            state="reserved",
            lease_expires_at=lease_expires_at,
            created_at=now,
        )
        .on_conflict_do_nothing(index_elements=["reservation_id"])
    )
    row = await db.get(AccountBudgetReservation, reservation_id, populate_existing=True)
    if row is None or (row.account_id, row.workload, row.lane, row.reserved_reads) != (
        account_id,
        workload,
        lane,
        reserved_reads,
    ):
        raise ValueError("budget_reservation_idempotency_conflict")
    if row.state != "reserved" or row.lease_expires_at <= now:
        raise ValueError("budget_reservation_not_active")
    return row


async def settle(
    db: Any,
    reservation_id: str,
    consumed_reads: int,
    *,
    state: str = "settled",
    reason: str | None = None,
    now: datetime | None = None,
) -> bool:
    """CAS terminal state; duplicate settlement cannot charge Redis or PG twice."""
    if state not in {"settled", "released", "expired"} or consumed_reads < 0:
        raise ValueError("invalid_budget_settlement")
    result = await db.execute(
        update(AccountBudgetReservation)
        .where(
            AccountBudgetReservation.reservation_id == reservation_id,
            AccountBudgetReservation.state == "reserved",
            AccountBudgetReservation.reserved_reads >= consumed_reads,
        )
        .values(
            consumed_reads=consumed_reads,
            state=state,
            reason=reason,
            settled_at=now or datetime.utcnow(),
        )
        .returning(AccountBudgetReservation.reservation_id)
    )
    return result.scalar_one_or_none() is not None


async def reserve_read_work(db: Any, cache: Any, work: Any, reserved: int) -> None:
    """Called only after the Redis claim and before the first Telegram RPC."""
    if not settings.BUDGET_LEDGER_ENABLED:
        return
    from app.core.account.read_work import key, lane_for

    work.ledger_id = "read:" + work.token
    ttl = max(1, await cache.ttl(key(work.account_id)))
    await reserve(
        db,
        work.ledger_id,
        account_id=work.account_id,
        workload=work.kind,
        lane=lane_for(work),
        reserved_reads=reserved,
        lease_expires_at=datetime.utcnow() + timedelta(seconds=ttl),
    )
    await db.commit()
    # Attach only while this generation owns the Redis reservation. Lua updates
    # the consumption journal in the same transaction as the actual budget.
    attached = await cache.eval(
        "if redis.call('HGET',KEYS[1],'token')~=ARGV[1] then return 0 end "
        "redis.call('HSET',KEYS[1],'audit_id',ARGV[2]); "
        "redis.call('SET',KEYS[2],0,'EX',604800,'NX'); return 1",
        2,
        key(work.account_id),
        CONSUMED_PREFIX + work.ledger_id,
        work.token,
        work.ledger_id,
    )
    if not attached:
        raise RuntimeError("budget_reservation_lease_lost")


async def finish_read_work(work: Any) -> None:
    if not work.ledger_id:
        return
    import structlog

    from app.core.database import get_db_session
    from app.core.redis import get_redis

    try:
        cache = await get_redis()
        actual = await cache.get(CONSUMED_PREFIX + work.ledger_id)
        async with get_db_session() as db:
            await settle(
                db,
                work.ledger_id,
                int(actual) if actual is not None else work.reads,
                reason="handoff" if work.handed_off else "work_complete",
            )
    except Exception as exc:
        # The expiry scanner uses the Redis journal after a crash or DB outage.
        structlog.get_logger().warning("budget_settlement_deferred", error_type=type(exc).__name__)


async def expire_reservations(db: Any, cache: Any, now: datetime) -> int:
    rows = list(
        (
            await db.scalars(
                select(AccountBudgetReservation)
                .where(
                    AccountBudgetReservation.state == "reserved",
                    AccountBudgetReservation.lease_expires_at <= now,
                )
                .order_by(AccountBudgetReservation.lease_expires_at)
                .limit(200)
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    for row in rows:
        actual = (
            await cache.get(CONSUMED_PREFIX + row.reservation_id) if row.reserved_reads else "0"
        )
        # A missing evicted journal is explicitly uncertain. Never refund Redis
        # counters or invent a successful external outcome from lease expiry.
        await settle(
            db,
            row.reservation_id,
            int(actual) if actual is not None else row.consumed_reads,
            state="expired",
            reason="lease_expired" if actual is not None else "consumption_evidence_missing",
            now=now,
        )
    return len(rows)


async def write_snapshots(db: Any, cache: Any, now: datetime) -> list[dict]:
    from app.core.account.models import TelegramAccount
    from app.core.account.rpc_governor import (
        limits_for,
        load_state,
        read_budget_state,
        recovery_factor,
    )

    tick = now.replace(second=0, microsecond=0)
    accounts = list(
        (
            await db.scalars(
                select(TelegramAccount.id)
                .where(
                    TelegramAccount.is_active.is_(True),
                )
                .order_by(TelegramAccount.id)
            )
        ).all()
    )
    values = []
    for account_id in accounts:
        state = await load_state(db, account_id)
        limits = limits_for(state, now, account_id)
        budget = await read_budget_state(account_id, limits, now)
        for window, duration in (("hour", 3600), ("day", 86400)):
            total = budget["usage"][window]
            ttl = total["ttl_seconds"]
            start = (
                now - timedelta(seconds=duration - max(0, min(duration, ttl))) if ttl >= 0 else now
            )
            loans = {
                lane: budget["usage"][f"{lane}_lent_{window}"]["used"]
                for lane in ("survival", "sync", "ad")
            }
            loans.update({
                f"sync_borrowed_{donor}": budget["usage"][f"sync_borrowed_{donor}_{window}"]["used"]
                for donor in ("ad", "survival")
            })
            loans["sync_borrowed"] = loans["sync_borrowed_ad"] + loans["sync_borrowed_survival"]
            for lane in ("ad", "survival", "critical", "sync", "routine", "listener"):
                item = budget["usage"][f"{lane}_{window}"]
                cap = item.get("guaranteed_limit", item["limit"])
                values.append(
                    {
                        "account_id": account_id,
                        "window": window,
                        "window_start": start,
                        "lane": lane,
                        "limit": cap,
                        "used": item["used"],
                        "remaining": max(0, cap - item["used"]),
                        "ttl_seconds": ttl,
                        "read_factor": recovery_factor(state, now),
                        "loan_json": json.dumps(loans),
                        "snapshot_at": tick,
                    }
                )
    if values:
        await db.execute(
            conflict_insert(db, AccountBudgetSnapshot)
            .values(values)
            .on_conflict_do_nothing(index_elements=["account_id", "window", "lane", "snapshot_at"])
        )
    await expire_reservations(db, cache, now)
    await db.commit()
    await cache.set(SNAPSHOT_AT, now.timestamp())
    return values
