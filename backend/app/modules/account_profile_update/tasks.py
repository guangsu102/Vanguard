"""Celery entry point for durable serial ad-account profile updates."""

from __future__ import annotations

import asyncio
from typing import Any

from app.celery import celery_app
from app.modules.account_profile_update.service import run_account_profile_update_tick


async def _run_tick_with_db(*, stale_after_seconds: int) -> dict[str, Any]:
    from app.core import database as db_module
    from app.core import redis as redis_module
    from app.core.account.pool import get_account_pool

    if db_module.async_session_factory is None:
        await db_module.init_db(create_tables=False)
    if redis_module.redis_client is None:
        await redis_module.init_redis()
    async with db_module.get_db_session() as db:
        return await run_account_profile_update_tick(
            db,
            limit=1,
            stale_after_seconds=stale_after_seconds,
            account_pool=get_account_pool(),
        )


async def _run_with_cleanup(awaitable):
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
    return asyncio.run(_run_with_cleanup(awaitable))


@celery_app.task(
    name="app.modules.account_profile_update.tasks.account_profile_update_tick",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def account_profile_update_tick(
    self,
    stale_after_seconds: int = 300,
):
    """Recover queue state and attempt at most one actual Telegram profile update."""
    try:
        return _run_async(
            _run_tick_with_db(
                stale_after_seconds=max(90, int(stale_after_seconds)),
            )
        )
    except Exception as exc:  # pragma: no cover - worker infrastructure failures
        raise self.retry(exc=exc) from exc


__all__ = ["account_profile_update_tick"]
