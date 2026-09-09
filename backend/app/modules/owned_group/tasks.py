"""Celery entry points for the persistent self-owned group worker."""

from __future__ import annotations

import asyncio
from typing import Any

from app.celery import celery_app
from app.modules.owned_group.worker import run_owned_group_worker_tick


def _build_owned_group_adapter(db: Any):
    """Build the concrete Telegram boundary only for an explicit live run.

    Keeping the import and construction lazy is important: API processes and
    test workers must be able to load the module without opening a Telegram
    client or accidentally falling back to the no-op adapter when execution is
    enabled.
    """

    # Keep this compatibility wrapper because a few deployment probes and
    # integrations import the private task helper directly.  Construction is
    # centralized so API reconciliation and Celery use identical guard wiring.
    from app.modules.owned_group.factory import build_owned_group_adapter

    return build_owned_group_adapter(db)


async def _run_tick_with_db(*, limit: int, stale_after_seconds: int) -> dict[str, Any]:
    from app.core import database as db_module
    from app.core import redis as redis_module

    if db_module.async_session_factory is None:
        await db_module.init_db(create_tables=False)
    # Celery workers do not run the FastAPI lifespan.  Initialize the shared
    # Redis client here so the persisted global stop is honored across workers.
    if redis_module.redis_client is None:
        await redis_module.init_redis()
    async with db_module.get_db_session() as db:
        adapter = _build_owned_group_adapter(db)
        return await run_owned_group_worker_tick(
            db,
            limit=limit,
            stale_after_seconds=stale_after_seconds,
            adapter=adapter,
            durable_claims=True,
            strict_precheck=True,
        )


async def _run_with_worker_cleanup(awaitable):
    """Dispose every loop-bound singleton before ``asyncio.run`` closes.

    Celery invokes this task repeatedly in one process while ``asyncio.run``
    creates a fresh event loop for each invocation.  Reusing AsyncPG, Redis or
    Telethon objects from the prior closed loop makes the second tick fail in
    production, so cleanup is part of the task's correctness boundary.
    """

    from app.core import database as db_module
    from app.core import redis as redis_module
    from app.core.account.pool import close_account_pool

    try:
        return await awaitable
    finally:
        try:
            await close_account_pool()
        finally:
            try:
                if redis_module.redis_client is not None:
                    await redis_module.close_redis()
            finally:
                if db_module.engine is not None:
                    await db_module.close_db()


def _run_async(awaitable):
    """Run one task on a fresh loop, matching the project's Celery pattern."""

    return asyncio.run(_run_with_worker_cleanup(awaitable))


@celery_app.task(
    name="app.modules.owned_group.tasks.owned_group_worker_tick",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def owned_group_worker_tick(self, limit: int = 10, stale_after_seconds: int = 900):
    """Process prechecks, due operations, and stale-worker recovery once."""

    try:
        return _run_async(
            _run_tick_with_db(
                limit=max(1, min(int(limit), 200)),
                stale_after_seconds=max(30, int(stale_after_seconds)),
            )
        )
    except Exception as exc:  # pragma: no cover - broker/DB failures are runtime concerns
        raise self.retry(exc=exc) from exc


__all__ = ["owned_group_worker_tick"]
