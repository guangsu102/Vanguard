"""Metric role coverage must own real loops and close task sources on failure."""

import asyncio
import threading

import pytest

from app.core import database, redis
from app.core.account import pool, read_metrics
from app.core.config import settings
from app.core.scheduler import tasks, worker


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "operation", "database"])
async def test_celery_health_closes_after_all_resources_even_when_task_or_cleanup_fails(monkeypatch, failure):
    events = []

    def start():
        assert asyncio.get_running_loop().is_running()
        events.append("health_start")

    async def stop():
        events.append("health_stop")

    async def initialized():
        events.append("redis_init")

    async def closed_pool():
        events.append("pool_close")

    async def closed_redis():
        events.append("redis_close")

    async def closed_database():
        events.append("db_close")
        if failure == "database":
            raise ValueError("database cleanup failed")

    async def operation():
        events.append("operation")
        if failure == "operation":
            raise RuntimeError("operation failed")
        return 42

    monkeypatch.setattr(read_metrics, "start_metrics_health", start)
    monkeypatch.setattr(read_metrics, "stop_metrics_health", stop)
    monkeypatch.setattr(redis, "init_redis", initialized)
    monkeypatch.setattr(redis, "close_redis", closed_redis)
    monkeypatch.setattr(redis, "redis_client", object())
    monkeypatch.setattr(pool, "close_account_pool", closed_pool)
    monkeypatch.setattr(database, "engine", object())
    monkeypatch.setattr(database, "close_db", closed_database)
    if failure:
        with pytest.raises(RuntimeError if failure == "operation" else ValueError):
            await tasks._run_with_worker_cleanup(operation())
    else:
        assert await tasks._run_with_worker_cleanup(operation()) == 42
    assert events == ["redis_init", "health_start", "operation", "pool_close",
                      "redis_close", "db_close", "health_stop"]


@pytest.mark.parametrize("role", ["celery", "resource"])
def test_idle_celery_manager_health_uses_and_closes_a_separate_real_event_loop(monkeypatch, role):
    started, stopped = threading.Event(), threading.Event()
    observed = []

    def start():
        observed.append((threading.get_ident(), asyncio.get_running_loop().is_running()))
        started.set()

    async def stop():
        stopped.set()

    monkeypatch.setattr(settings, "REVIEW_READ_METRICS_ENABLED", True)
    monkeypatch.setattr(settings, "REVIEW_READ_METRICS_ROLE", role)
    monkeypatch.setattr(read_metrics, "start_metrics_health", start)
    monkeypatch.setattr(read_metrics, "stop_metrics_health", stop)
    close = worker._start_metrics_manager_health()
    try:
        assert started.wait(2)
    finally:
        close()
    assert stopped.is_set()
    assert observed == [(observed[0][0], True)]
    assert observed[0][0] != threading.get_ident()


def test_disabled_manager_health_does_not_create_a_source(monkeypatch):
    monkeypatch.setattr(settings, "REVIEW_READ_METRICS_ENABLED", False)
    monkeypatch.setattr(read_metrics, "start_metrics_health", lambda: pytest.fail("must remain disabled"))
    worker._start_metrics_manager_health()()


@pytest.mark.parametrize("fails", [False, True])
def test_single_queue_resource_worker_keeps_idle_source_and_stops_on_exit(monkeypatch, fails):
    events = []
    monkeypatch.setattr(worker, "_start_metrics_manager_health",
        lambda: events.append("start") or (lambda: events.append("stop")))
    def process(command):
        events.append("worker")
        if fails:
            raise RuntimeError("worker failed")
    monkeypatch.setattr(worker.os, "system", process)
    if fails:
        with pytest.raises(RuntimeError):
            worker.start_worker(queues=["resource_search"])
    else:
        worker.start_worker(queues=["resource_search"])
    assert events == ["start", "worker", "stop"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_idle_reconciliation_source_closes_even_when_cleanup_fails(monkeypatch, cleanup_fails):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.core import reconciliation_worker as reconciliation
    events = []
    monkeypatch.setattr(reconciliation, "init_db", AsyncMock())
    monkeypatch.setattr(reconciliation, "init_redis", AsyncMock())
    monkeypatch.setattr(reconciliation, "get_redis", AsyncMock(return_value=object()))
    monkeypatch.setattr(reconciliation, "_serve_metrics", AsyncMock(return_value=SimpleNamespace(cleanup=AsyncMock())))
    monkeypatch.setattr(settings, "RECONCILIATION_STREAM_ENABLED", False)
    started = asyncio.Event()
    def start():
        events.append("start")
        started.set()
    async def stop():
        events.append("stop")
    async def maintenance():
        await asyncio.Future()
    async def close_db():
        events.append("db_close")
        if cleanup_fails:
            raise RuntimeError("db cleanup")
    monkeypatch.setattr(read_metrics, "start_metrics_health", start)
    monkeypatch.setattr(read_metrics, "stop_metrics_health", stop)
    monkeypatch.setattr(reconciliation, "maintenance", maintenance)
    monkeypatch.setattr(pool, "get_account_pool", lambda: SimpleNamespace(close_all=AsyncMock()))
    monkeypatch.setattr(reconciliation, "close_redis", AsyncMock())
    monkeypatch.setattr(reconciliation, "close_db", close_db)
    task = asyncio.create_task(reconciliation.run())
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(RuntimeError if cleanup_fails else asyncio.CancelledError):
        await task
    assert events == ["start", "db_close", "stop"]
