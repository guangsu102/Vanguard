"""Bounded local repair independent of an already queued review ticket."""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import lazyload


async def repair(db: Any, client: Any, now: datetime) -> dict[str, int]:
    if not await client.set("vanguard:review-repair:scan", now.isoformat(), nx=True, ex=60):
        return {}
    from app.core.account.event_inbox import recover_safe_events, recover_stale_processing
    from app.core.account.models import TelegramAccount
    from app.core.account.rpc_governor import RpcDeferred, check_read_ready
    from app.core.scheduler.growth_dispatch import eligible_accounts
    from app.modules.acquisition.models import GroupQualificationAudit as Audit
    from app.modules.acquisition.qualification_due import current_clause
    from app.modules.acquisition.qualification_events import queue_due_gaps, retire_legacy_synthetic_gaps
    from app.modules.acquisition.qualification_identity_refresh import queue_identity_refreshes
    from app.modules.acquisition.qualification_service import _ids, ensure_membership_reviews, policy
    from app.modules.acquisition.review_waits import budget_wait_ids

    stale_events = await recover_stale_processing(db)
    stats = {"safe_events": await recover_safe_events(db),
             "stale_events_retried": stale_events["retried"],
             "stale_events_reconciled": stale_events["reconciled"],
             "stale_claims_recovered": 0,
             "missing_reviews": 0,
             "identity_refreshes": 0, "budget_wakeups": 0, "event_reviews": 0,
             "legacy_gaps_retired": 0}
    from app.modules.acquisition.review_retention import retire_obsolete_reviews
    await retire_obsolete_reviews(db, limit=500)
    stats["stale_claims_recovered"] = await _recover_stale_claims(db, now=now, limit=32)
    config = await policy(db, fresh=True)
    if not config.get("enabled") or not config.get("execute_reviews", True):
        await db.commit()
        return stats
    stats["missing_reviews"] = len(await ensure_membership_reviews(db, config, limit=32))
    stats["identity_refreshes"] = await queue_identity_refreshes(db, config, limit=2)
    accounts = list((await db.scalars(eligible_accounts(now).order_by(
        # Account IDs are few; network I/O never occurs in this recovery lane.
        TelegramAccount.id
    ).limit(32))).all())
    for account_id in accounts:
        if config.get("exit_all_accounts") is True or not config.get("account_ids") or account_id in _ids(config["account_ids"]):
            stats["legacy_gaps_retired"] += await retire_legacy_synthetic_gaps(db, account_id)
            stats["event_reviews"] += await queue_due_gaps(db, account_id, limit=2)
        ids = await budget_wait_ids(db, account_id, now=now)
        legacy = list((await db.scalars(select(Audit.id).where(
            Audit.account_id == account_id, current_clause(),
            Audit.state.in_(["queued", "completed"]),
            Audit.reason == "telegram_read_budget", Audit.next_retry_at > now,
        ).limit(32))).all())
        ids.update(legacy)
        if not ids:
            continue
        try:
            await check_read_ready(db, account_id, now, purpose="group_qualification", minimum_reads=1)
        except RpcDeferred:
            continue
        rows = (await db.scalars(select(Audit).where(
            Audit.id.in_(ids), current_clause(),
        ).with_for_update(skip_locked=True).limit(32))).all()
        for row in rows:
            row.next_retry_at = now
        stats["budget_wakeups"] += len(rows)
    await db.commit()
    return stats


async def _recover_stale_claims(db: Any, *, now: datetime, limit: int = 32) -> int:
    """Release review claims whose durable worker deadline has elapsed.

    The dispatcher normally repairs these rows before selecting a review, but
    this local lane also runs when qualification is paused and after a worker
    restart.  It only updates database state; Telegram and model calls remain
    in the normal review quantum.
    """
    from app.core.group.models import GroupAccountMembership
    from app.modules.acquisition.models import GroupQualificationAudit as Audit
    from app.modules.acquisition.qualification_due import current_clause
    from app.modules.acquisition.qualification_service import (
        STALE_CLAIM_GRACE_SECONDS,
        recover_stale_review_row,
    )
    from app.modules.acquisition.review_waits import clear, record

    cutoff = now - timedelta(seconds=STALE_CLAIM_GRACE_SECONDS)
    rows = list(
        (
            await db.scalars(
                select(Audit)
                .where(
                    current_clause(),
                    Audit.state.in_(["running", "reviewing_ai", "waiting_ai"]),
                    Audit.next_retry_at.is_not(None),
                    Audit.next_retry_at <= cutoff,
                )
                .order_by(Audit.next_retry_at, Audit.id)
                .with_for_update(of=Audit, skip_locked=True)
                .limit(max(1, min(int(limit), 100)))
            )
        ).all()
    )
    recovered = 0
    for row in rows:
        # ``GroupAccountMembership.account`` uses joined loading.  A plain
        # ``Session.get(..., with_for_update=True)`` therefore emits an outer
        # join and PostgreSQL rejects the lock as applying to its nullable
        # side.  This repair only needs membership columns, so suppress the
        # relationship load and lock the membership row directly.
        membership = await db.scalar(
            select(GroupAccountMembership)
            .options(lazyload(GroupAccountMembership.account))
            .where(GroupAccountMembership.id == row.membership_id)
            .with_for_update(skip_locked=True)
        )
        action = recover_stale_review_row(row, membership, now=now)
        if row.next_retry_at is None:
            await clear(db, row.id)
        else:
            await record(
                db,
                row.id,
                "stale_review_claim_recovered",
                0,
                row.next_retry_at,
                now,
            )
        recovered += 1
    if recovered:
        await db.commit()
    return recovered
