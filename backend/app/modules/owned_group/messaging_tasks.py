"""Celery entry point for stage-two owned-group message dispatch."""

from __future__ import annotations

import asyncio
from typing import Any

from app.celery import celery_app
from app.modules.owned_group.messaging_worker import run_owned_group_message_tick


async def _run_tick_with_db(*, limit: int) -> dict[str, int]:
    from app.core import database as db_module
    from app.core import redis as redis_module
    from app.core.account.pool import get_account_pool

    if db_module.async_session_factory is None:
        await db_module.init_db(create_tables=False)
    if redis_module.redis_client is None:
        await redis_module.init_redis()
    async with db_module.get_db_session() as db:
        return await run_owned_group_message_tick(
            db,
            limit=max(1, min(int(limit), 200)),
            account_pool=get_account_pool(),
        )


async def _run_with_cleanup(awaitable: Any) -> dict[str, int]:
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


def _run_async(awaitable: Any) -> dict[str, int]:
    return asyncio.run(_run_with_cleanup(awaitable))


@celery_app.task(
    name="app.modules.owned_group.messaging_tasks.dispatch_owned_group_messages",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def dispatch_owned_group_messages(self, limit: int = 50) -> dict[str, int]:
    try:
        return _run_async(_run_tick_with_db(limit=limit))
    except Exception as exc:  # pragma: no cover - broker/DB runtime boundary
        raise self.retry(exc=exc) from exc


__all__ = ["dispatch_owned_group_messages"]
