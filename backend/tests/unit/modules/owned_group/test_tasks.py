from __future__ import annotations

from unittest.mock import MagicMock, patch


def test_owned_group_task_closes_loop_bound_resources_after_each_tick():
    from app.core import database, redis
    from app.core.account import pool as pool_module
    from app.modules.owned_group import tasks

    events: list[str] = []

    async def operation():
        events.append("operation")
        return "completed"

    async def close_pool():
        events.append("pool_close")

    async def close_redis():
        events.append("redis_close")

    async def close_db():
        events.append("db_close")

    with (
        patch.object(pool_module, "close_account_pool", new=close_pool),
        patch.object(redis, "redis_client", MagicMock()),
        patch.object(redis, "close_redis", new=close_redis),
        patch.object(database, "engine", MagicMock()),
        patch.object(database, "close_db", new=close_db),
    ):
        result = tasks._run_async(operation())

    assert result == "completed"
    assert events == ["operation", "pool_close", "redis_close", "db_close"]
