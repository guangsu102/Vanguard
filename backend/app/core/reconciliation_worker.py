"""Independent PostgreSQL-backed Redis Stream reconciliation consumer."""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
import uuid
from datetime import datetime

import structlog
from aiohttp import web

from app.core.account import reconciliation_stream as stream
from app.core.account.budget_ledger import write_snapshots
from app.core.account.reconciliation_handlers import execute_task
from app.core.config import settings
from app.core.database import close_db, get_db_session, init_db
from app.core.redis import close_redis, get_redis, init_redis

logger = structlog.get_logger()


async def process(task, owner: str) -> dict:
    from app.core.scheduler.growth_dispatch import claim_account_run, release_account_run

    cache = await get_redis()
    if not await claim_account_run(cache, task.account_id, owner, stage="reconciliation"):
        return {"state": "pending", "error": "account_execution_busy", "retry_after": 15}
    try:
        return await _process_owned(task, owner)
    finally:
        await release_account_run(cache, task.account_id, owner, stage="reconciliation")


async def _process_owned(task, owner: str) -> dict:
    from app.core.account.session_exec import PROCESS_ID, forward, owner_id

    cache = await get_redis()
    session_owner = await owner_id(cache, task.account_id)
    if session_owner and session_owner != PROCESS_ID:
        reply = await forward(task.account_id, f"reconciliation:{task.id}:{owner}")
        if not reply or reply.get("error"):
            return {"state": "pending", "error": "session_owner_unavailable", "retry_after": 30}
        return reply.get("result") or {"state": "pending", "retry_after": 30}
    from app.core.account.pool import get_account_pool

    async with get_db_session() as db:
        return await execute_task(
            db, task.id, owner, task.account_id, account_pool=get_account_pool()
        )


async def consume_messages(cache, messages: list, consumer: str) -> int:
    processed = 0
    for message_id, fields in messages:
        key = fields.get("task_key")
        if not key:
            await cache.xack(stream.STREAM_KEY, stream.CONSUMER_GROUP, message_id)
            continue
        owner = f"{consumer}:{uuid.uuid4().hex}"
        async with get_db_session() as db:
            task = await stream.claim_task(db, key, owner, active_only=True)
        if task is None:
            await cache.xack(stream.STREAM_KEY, stream.CONSUMER_GROUP, message_id)
            continue
        if not stream.active_for(task.account_id):
            outcome = {"state": "pending", "error": "rollout_deferred", "retry_after": 300}
        elif settings.RECONCILIATION_SHADOW_MODE:
            outcome = {"state": "pending", "error": "shadow_mode", "retry_after": 300}
        else:
            try:
                async with asyncio.timeout(270):
                    outcome = await process(task, owner)
            except Exception as exc:
                from app.core.account.rpc_governor import deferred_error

                deferred = deferred_error(exc)
                outcome = {
                    "state": "pending",
                    "error": deferred[0] if deferred else type(exc).__name__,
                    "retry_after": deferred[1] if deferred else 60,
                }
        async with get_db_session() as db:
            committed = await stream.finish_task(
                db,
                key,
                owner,
                state=outcome.get("state", "pending"),
                error_code=outcome.get("error"),
                retry_after=outcome.get("retry_after", 60),
                next_verification_at=(datetime.fromisoformat(outcome["next_verification_at"])
                                      if outcome.get("next_verification_at") else None),
            )
        if committed and (outcome.get("state") in {"succeeded", "manual_required", "cancelled"}
                          or outcome.get("observed") and outcome.get("next_verification_at")):
            from app.core.metrics import record_reconciliation

            await record_reconciliation(
                max(0, (datetime.utcnow() - (task.verification_due_at or task.created_at)).total_seconds()),
                event_key=(f"reconciliation:{task.id}:cycle:"
                           f"{(task.verification_due_at or task.created_at).isoformat()}"),
            )
        # ACK only after the owner CAS commits. If the lease expired while the
        # external read was running, leave the entry pending for XAUTOCLAIM;
        # acknowledging it here could erase the successor consumer's work.
        if committed:
            await cache.xack(stream.STREAM_KEY, stream.CONSUMER_GROUP, message_id)
            processed += 1
    return processed


async def maintenance() -> None:
    last_snapshot = 0.0
    while True:
        try:
            cache = await get_redis()
            async with get_db_session() as db:
                if settings.RECONCILIATION_STREAM_ENABLED:
                    await stream.recover_expired(db)
                    await stream.scan_database(db)
                    await stream.publish_pending(db, cache)
                if settings.BUDGET_LEDGER_ENABLED:
                    if time.monotonic() - last_snapshot >= 60:
                        await write_snapshots(db, cache, datetime.utcnow())
                        last_snapshot = time.monotonic()
            await cache.set("vanguard:reconciliation:heartbeat:v1", str(time.time()), ex=120)
        except Exception as exc:
            logger.warning("reconciliation_maintenance_failed", error_type=type(exc).__name__)
        await asyncio.sleep(30)


async def _serve_metrics(cache):
    """Expose worker health only; business counters are scraped once on backend."""

    async def metrics(request):
        if settings.is_production and not settings.METRICS_SCRAPE_TOKEN:
            return web.Response(status=404)
        if settings.METRICS_SCRAPE_TOKEN:
            import secrets

            if not secrets.compare_digest(
                request.headers.get("authorization", ""),
                f"Bearer {settings.METRICS_SCRAPE_TOKEN}",
            ):
                return web.Response(status=404)
        try:
            stamp = await cache.get("vanguard:reconciliation:heartbeat:v1")
            age = max(0, time.time() - float(stamp or 0))
            return web.Response(
                text=("# TYPE vanguard_reconciliation_worker_heartbeat_age_seconds gauge\n"
                      f"vanguard_reconciliation_worker_heartbeat_age_seconds {age}\n"),
                content_type="text/plain",
                charset="utf-8",
            )
        except Exception:
            return web.Response(status=503)

    async def health(_request):
        if await cache.exists("vanguard:reconciliation:heartbeat:v1"):
            return web.json_response({"status": "healthy"})
        return web.json_response({"status": "unready"}, status=503)

    app = web.Application()
    app.router.add_get("/metrics", metrics)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8000)
    await site.start()
    return runner


async def run() -> None:
    await init_db(create_tables=False)
    await init_redis()
    consumer = f"reconciliation-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    cache = await get_redis()
    metrics_runner = await _serve_metrics(cache)
    maintenance_task = asyncio.create_task(maintenance())
    try:
        while True:
            if not settings.RECONCILIATION_STREAM_ENABLED or settings.RECONCILIATION_SHADOW_MODE:
                await asyncio.sleep(5)
                continue
            try:
                messages = await stream.reclaim(cache, consumer=consumer)
                messages += await stream.read_group(cache, consumer=consumer, count=1)
                if messages:
                    await consume_messages(cache, messages, consumer)
            except Exception as exc:
                logger.warning("reconciliation_consumer_deferred", error_type=type(exc).__name__)
                await asyncio.sleep(5)
    finally:
        maintenance_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await maintenance_task
        await metrics_runner.cleanup()
        from app.core.account.pool import get_account_pool

        await get_account_pool().close_all()
        await close_redis()
        await close_db()


if __name__ == "__main__":
    asyncio.run(run())
