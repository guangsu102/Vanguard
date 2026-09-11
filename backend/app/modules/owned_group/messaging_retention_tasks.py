"""Celery entry point for the daily owned-group Persona retention pass."""

from __future__ import annotations

import asyncio
from typing import Any

from app.celery import celery_app
from app.modules.owned_group.messaging_retention import run_persona_retention


async def _run_retention_with_db(*, batch_size: int, dry_run: bool) -> dict[str, int | bool]:
    from app.core import database as db_module
    from app.core import redis as redis_module

    if db_module.async_session_factory is None:
        await db_module.init_db(create_tables=False)
    if redis_module.redis_client is None:
        await redis_module.init_redis()

    async with db_module.get_db_session() as db:
        return await run_persona_retention(
            db,
            redis_client=redis_module.redis_client,
            batch_size=max(1, min(int(batch_size), 500)),
            dry_run=bool(dry_run),
        )


async def _run_with_cleanup(awaitable: Any) -> dict[str, int | bool]:
    from app.core import database as db_module
    from app.core import redis as redis_module

    try:
        return await awaitable
    finally:
        try:
            if redis_module.redis_client is not None:
                await redis_module.close_redis()
        finally:
            if db_module.engine is not None:
                await db_module.close_db()


def _run_async(awaitable: Any) -> dict[str, int | bool]:
    return asyncio.run(_run_with_cleanup(awaitable))


@celery_app.task(
    name="app.modules.owned_group.messaging_retention_tasks.cleanup_owned_group_persona_retention",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
)
def cleanup_owned_group_persona_retention(
    self: Any,
    batch_size: int = 500,
    dry_run: bool = False,
) -> dict[str, int | bool]:
    """Run one pass; failed DB batches surface to Celery for retry."""

    try:
        result = _run_async(
            _run_retention_with_db(
                batch_size=batch_size,
                dry_run=dry_run,
            )
        )
        if int(result.get("failed", 0)) > 0:
            raise RuntimeError("owned-group Persona retention reported a failed batch")
        return result
    except Exception as exc:  # pragma: no cover - broker/DB runtime boundary
        raise self.retry(exc=exc) from exc


__all__ = ["cleanup_owned_group_persona_retention"]
