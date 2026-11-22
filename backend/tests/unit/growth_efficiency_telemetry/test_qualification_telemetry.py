from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock
import pytest

ROOT = Path(__file__).resolve().parents[3] / "app" / "modules" / "acquisition"
spec = importlib.util.spec_from_file_location("sla_telemetry_draft", ROOT / "qualification_telemetry.py")
telemetry = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = telemetry
spec.loader.exec_module(telemetry)
NOW = datetime(2026, 11, 5, 19)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(telemetry, "_enabled", lambda: True)
    monkeypatch.setattr(telemetry, "_engine_enabled", lambda account: account == 33)
    events = []
    @asynccontextmanager
    async def own_session():
        events.append("own session entered")
        try:
            yield object()
        finally:
            events.append("own session closed")
    monkeypatch.setattr(telemetry, "_session_factory", lambda: own_session)
    return events


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,account", [(False, 33), (True, 34), (True, None)])
async def test_disabled_or_unbound_account_does_not_acquire_session(configured, monkeypatch, enabled, account):
    monkeypatch.setattr(telemetry, "_enabled", lambda: enabled)
    result = await telemetry.snapshot(account, NOW)
    assert configured == []
    assert result["startup"]["eligible_mature"] is None
    assert result["cost"]["total_tasks"] is None


@pytest.mark.asyncio
async def test_projection_uses_own_existing_factory_session_and_hard_payload_limit(configured, monkeypatch):
    seen = []
    async def startup(db, account, now, *, since):
        seen.append(("startup", account))
        return {"available": True, "strict_rate": None}
    async def cost(db, account, now, *, since, sdk_coverage, limit):
        seen.append(("cost", account, sdk_coverage, limit))
        return {"coverage_complete": False, "tasks": []}
    monkeypatch.setattr(telemetry, "_load_reports", lambda: (startup, cost))
    result = await telemetry.snapshot(33, NOW, sdk_coverage={"account_id": 33})
    assert seen == [("startup", 33), ("cost", 33, {"account_id": 33}, 20)]
    assert result["cost"]["available"] is True
    assert configured == ["own session entered", "own session closed"]


@pytest.mark.asyncio
async def test_timeout_closes_own_read_session_and_keeps_unknown_denominator(configured, monkeypatch):
    async def blocked(*args, **kwargs):
        await asyncio.Event().wait()
    monkeypatch.setattr(telemetry, "REPORT_TIMEOUT", .01)
    monkeypatch.setattr(telemetry, "_load_reports", lambda: (blocked, blocked))
    result = await telemetry.snapshot(33, NOW)
    assert result["startup"]["reason"] == "measurement_timeout"
    assert result["startup"]["eligible_mature"] is None
    assert configured[-1] == "own session closed"


@pytest.mark.asyncio
async def test_later_cost_failure_preserves_valid_startup_projection(configured, monkeypatch):
    async def startup(*args, **kwargs):
        return {"available": True, "strict_rate": None, "potential_mature_never_started": 2}
    async def failed(*args, **kwargs):
        raise RuntimeError("measurement failure")
    monkeypatch.setattr(telemetry, "_load_reports", lambda: (startup, failed))
    result = await telemetry.snapshot(33, NOW)
    assert result["startup"]["potential_mature_never_started"] == 2
    assert result["cost"]["total_tasks"] is None


@pytest.mark.asyncio
async def test_callers_original_cancellation_is_not_swallowed(configured, monkeypatch):
    original = asyncio.CancelledError("original cancellation")
    async def cancelled(*args, **kwargs):
        raise original
    monkeypatch.setattr(telemetry, "_load_reports", lambda: (cancelled, cancelled))
    with pytest.raises(asyncio.CancelledError) as caught:
        await telemetry.snapshot(33, NOW)
    assert caught.value is original
    assert configured[-1] == "own session closed"


@pytest.mark.asyncio
async def test_postgres_own_transaction_is_read_only_and_timeout_is_local(configured, monkeypatch):
    calls = []
    async def execute(statement):
        calls.append(str(statement))
    session = Obj(bind=Obj(dialect=Obj(name="postgresql")), execute=execute,
        flush=AsyncMock(side_effect=AssertionError("flush")), commit=AsyncMock(side_effect=AssertionError("commit")))
    @asynccontextmanager
    async def factory():
        try:
            yield session
        finally:
            calls.append("own session closed")
    async def startup(db, *args, **kwargs):
        assert db is session
        calls.append("read startup")
        return {"available": True}
    async def cost(db, *args, **kwargs):
        assert db is session
        calls.append("read cost")
        return {"tasks": []}
    monkeypatch.setattr(telemetry, "_session_factory", lambda: factory)
    monkeypatch.setattr(telemetry, "_load_reports", lambda: (startup, cost))
    await telemetry.snapshot(33, NOW)
    assert calls == ["SET TRANSACTION READ ONLY", "SET LOCAL statement_timeout='200ms'",
        "read startup", "read cost", "own session closed"]
    session.flush.assert_not_awaited()
    session.commit.assert_not_awaited()
