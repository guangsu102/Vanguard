"""Run QQ automation on the existing QQ Celery queue."""

from __future__ import annotations

import asyncio
from typing import Any

from app.celery import celery_app
from app.modules.qq.automation import QQAutomationService


async def run_qq_automation() -> dict[str, Any]:
    from app.core import database, redis

    await database.init_db(create_tables=False)
    await redis.init_redis()
    try:
        if redis.redis_client is None:
            return {"status": "unavailable", "reason": "redis_not_connected"}
        async with database.get_db_session() as db:
            return await QQAutomationService(db, redis.redis_client).tick()
    finally:
        await redis.close_redis()
        await database.close_db()


@celery_app.task(name="app.modules.qq.automation_tasks.qq_automation_tick")
def qq_automation_tick() -> dict[str, Any]:
    return asyncio.run(run_qq_automation())
