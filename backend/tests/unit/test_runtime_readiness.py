from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_health_is_unready_when_dependency_fails(client, monkeypatch):
    from app.core import runtime_health
    monkeypatch.setattr(runtime_health, "dependencies", AsyncMock(side_effect=ConnectionError()))
    result = await client.get('/health')
    assert result.status_code == 503 and result.json()['status'] == 'unready'


@pytest.mark.asyncio
async def test_operational_details_require_authentication(client):
    assert (await client.get('/api/operations/status')).status_code == 401


@pytest.mark.asyncio
async def test_dependency_health_success_preserves_health_contract(client, monkeypatch):
    from app.core import runtime_health
    monkeypatch.setattr(runtime_health, "dependencies", AsyncMock(return_value={}))
    response = await client.get('/health')
    assert response.status_code == 200 and response.json()['status'] == 'healthy'
