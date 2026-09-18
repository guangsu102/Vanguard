"""Celery entry point for official SpamBot account checks."""

from __future__ import annotations

import asyncio
from typing import Any

from app.celery import celery_app
from app.modules.account_spam.service import run_account_spam_check_tick


async def _run_tick_with_db(*, limit: int, stale_after_seconds: int) -> dict[str, Any]:
    from app.core import database as db_module
    from app.core import redis as redis_module
    from app.core.account.pool import get_account_pool

    if db_module.async_session_factory is None:
        await db_module.init_db(create_tables=False)
    if redis_module.redis_client is None:
        await redis_module.init_redis()
    async with db_module.get_db_session() as db:
        return await run_account_spam_check_tick(
            db,
            limit=limit,
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
    name="app.modules.account_spam.tasks.account_spam_check_tick",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def account_spam_check_tick(
    self,
    limit: int = 10,
    stale_after_seconds: int = 300,
):
    """Process queued checks and recover abandoned item leases once."""

    try:
        return _run_async(
            _run_tick_with_db(
                limit=max(1, min(int(limit), 100)),
                stale_after_seconds=max(90, int(stale_after_seconds)),
            )
        )
    except Exception as exc:  # pragma: no cover - runtime infrastructure failure
        raise self.retry(exc=exc) from exc


__all__ = ["account_spam_check_tick"]
