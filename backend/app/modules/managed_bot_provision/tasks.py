"""Celery entry point for durable Telegram Managed Bot provisioning."""

from __future__ import annotations

import asyncio
from typing import Any

from app.celery import celery_app
from app.modules.managed_bot_provision.service import (
    run_managed_bot_provision_tick,
)


async def _run_tick_with_db(
    *,
    limit: int,
    stale_after_seconds: int,
) -> dict[str, int]:
    from app.core import database as db_module
    from app.core import redis as redis_module
    from app.core.account.pool import get_account_pool

    if db_module.async_session_factory is None:
        await db_module.init_db(create_tables=False)
    if redis_module.redis_client is None:
        await redis_module.init_redis()
    async with db_module.get_db_session() as db:
        return await run_managed_bot_provision_tick(
            db,
            limit=limit,
            stale_after_seconds=stale_after_seconds,
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
    name="app.modules.managed_bot_provision.tasks.managed_bot_provision_tick",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def managed_bot_provision_tick(
    self: Any,
    limit: int = 5,
    stale_after_seconds: int = 300,
) -> dict[str, int]:
    """Process queued provisions and recover expired leases once."""

    try:
        return _run_async(
            _run_tick_with_db(
                limit=max(1, min(int(limit), 20)),
                stale_after_seconds=max(90, int(stale_after_seconds)),
            )
        )
    except Exception as exc:  # pragma: no cover - runtime infrastructure failure
        safe_error = RuntimeError(
            f"managed_bot_provision_tick_failed:{type(exc).__name__}"
        )
        raise self.retry(exc=safe_error) from None


__all__ = ["managed_bot_provision_tick"]
