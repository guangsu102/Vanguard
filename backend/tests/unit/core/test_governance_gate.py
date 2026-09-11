from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from app.core import governance_gate
from app.core.governance_gate import (
    GOVERNANCE_STOP_REASON_REDIS_KEY,
    GOVERNANCE_STOP_REDIS_KEY,
    GOVERNANCE_STOP_UPDATED_AT_REDIS_KEY,
    GovernanceGateState,
    get_governance_gate_state,
    require_owned_group_governance_available,
    set_governance_stop,
)


class _FakePipeline:
    def __init__(self, client: _FakeRedisClient) -> None:
        self.client = client
        self.operations: list[tuple[str, str]] = []

    def set(self, key: str, value: str) -> _FakePipeline:
        self.operations.append((key, value))
        return self

    async def execute(self) -> list[bool]:
        for key, value in self.operations:
            self.client.values[key] = value
        return [True] * len(self.operations)


class _FakeRedisClient:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    def pipeline(self, *, transaction: bool) -> _FakePipeline:
        assert transaction is True
        return _FakePipeline(self)


class _FakeCache:
    def __init__(self, client: _FakeRedisClient | None) -> None:
        self.client = client

    async def get(self, key: str) -> str | None:
        if self.client is None:
            return None
        return await self.client.get(key)


def _install_cache(monkeypatch: pytest.MonkeyPatch, client: _FakeRedisClient | None) -> None:
    cache = _FakeCache(client)

    async def fake_redis_cache() -> _FakeCache:
        return cache

    monkeypatch.setattr(governance_gate, "_redis_cache", fake_redis_cache)


@pytest.fixture(autouse=True)
def _reset_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(governance_gate, "_fallback_state", GovernanceGateState())


@pytest.mark.asyncio
async def test_get_state_remains_available_when_feature_is_disabled(monkeypatch):
    monkeypatch.setattr(
        governance_gate.settings,
        "OWNED_GROUP_GOVERNANCE_ENABLED",
        False,
    )
    _install_cache(monkeypatch, _FakeRedisClient())

    state = await get_governance_gate_state()

    assert state == GovernanceGateState(
        governance_stop=False,
        reason="",
        updated_at=None,
        backend_available=True,
    )


@pytest.mark.asyncio
async def test_require_rejects_disabled_feature_without_reading_redis(monkeypatch):
    monkeypatch.setattr(
        governance_gate.settings,
        "OWNED_GROUP_GOVERNANCE_ENABLED",
        False,
    )

    async def unexpected_redis_cache() -> Any:
        raise AssertionError("disabled feature must reject before reading Redis")

    monkeypatch.setattr(governance_gate, "_redis_cache", unexpected_redis_cache)

    with pytest.raises(HTTPException) as exc_info:
        await require_owned_group_governance_available()

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["reason"] == "governance_feature_disabled"


@pytest.mark.asyncio
async def test_stop_round_trip_blocks_and_resume_reopens_governance(monkeypatch):
    monkeypatch.setattr(
        governance_gate.settings,
        "OWNED_GROUP_GOVERNANCE_ENABLED",
        True,
    )
    redis_client = _FakeRedisClient()
    _install_cache(monkeypatch, redis_client)

    stopped = await set_governance_stop(enabled=True, reason="maintenance", operator="admin-1")

    assert stopped.governance_stop is True
    assert stopped.reason == "maintenance"
    assert stopped.backend_available is True
    assert redis_client.values[GOVERNANCE_STOP_REDIS_KEY] == "1"
    assert redis_client.values[GOVERNANCE_STOP_REASON_REDIS_KEY] == "maintenance"
    assert redis_client.values[GOVERNANCE_STOP_UPDATED_AT_REDIS_KEY] == stopped.updated_at

    with pytest.raises(HTTPException) as exc_info:
        await require_owned_group_governance_available()

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["reason"] == "governance_stop_enabled"
    assert exc_info.value.detail["stop_reason"] == "maintenance"

    resumed = await set_governance_stop(enabled=False, reason="maintenance complete")
    available = await require_owned_group_governance_available()

    assert resumed.governance_stop is False
    assert available.governance_stop is False
    assert available.reason == "maintenance complete"
    assert redis_client.values[GOVERNANCE_STOP_REDIS_KEY] == "0"


@pytest.mark.asyncio
async def test_unavailable_redis_reports_backend_and_preserves_local_stop(monkeypatch):
    monkeypatch.setattr(
        governance_gate.settings,
        "OWNED_GROUP_GOVERNANCE_ENABLED",
        True,
    )
    _install_cache(monkeypatch, None)

    written = await set_governance_stop(enabled=True, reason="local emergency stop")
    state = await get_governance_gate_state()

    assert written.backend_available is False
    assert state.backend_available is False
    assert state.governance_stop is True
    assert state.reason == "local emergency stop"

    with pytest.raises(HTTPException) as exc_info:
        await require_owned_group_governance_available()
    assert exc_info.value.detail["reason"] == "governance_gate_backend_unavailable"


@pytest.mark.asyncio
async def test_unavailable_redis_fails_closed_when_no_stop_was_recorded(monkeypatch):
    monkeypatch.setattr(
        governance_gate.settings,
        "OWNED_GROUP_GOVERNANCE_ENABLED",
        True,
    )
    _install_cache(monkeypatch, None)

    with pytest.raises(HTTPException) as exc_info:
        await require_owned_group_governance_available()

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == {
        "reason": "governance_gate_backend_unavailable",
        "message": "Owned-group Guardian governance gate is unavailable",
        "retryable": True,
    }
