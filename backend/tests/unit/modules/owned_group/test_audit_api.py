from __future__ import annotations

import json

import pytest

from app.core.security import get_current_user
from app.main import app
from app.modules.owned_group.models_extra import OwnedGroupAuditEvent


@pytest.fixture(autouse=True)
def override_authenticated_user():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 7001,
        "username": "audit-test",
        "role": "auditor",
    }
    yield
    app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_audit_list_redacts_free_form_secret_values(client, test_db):
    test_db.add(
        OwnedGroupAuditEvent(
            event_type="owned_group_item_updated",
            operation_id=11,
            before_state=json.dumps(
                {
                    "status": "pending",
                    "bot_token": "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcd",
                }
            ),
            after_state="invite=https://t.me/+AbCdEfGh12345678",
            result="failed",
            reason_code="network_timeout",
            correlation_id="correlation-1",
        )
    )
    await test_db.flush()

    response = await client.get("/api/owned-groups/audit-events", params={"operation_id": 11})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    event = body["data"][0]
    assert json.loads(event["before_state"])["bot_token"] == "[REDACTED]"
    assert "+AbCdEfGh12345678" not in event["after_state"]
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZabcd" not in response.text


@pytest.mark.asyncio
async def test_audit_export_is_bounded_and_redacted(client, test_db):
    test_db.add(
        OwnedGroupAuditEvent(
            event_type="owned_group_created",
            result="success",
            after_state=json.dumps({"link_ciphertext": "secret"}),
        )
    )
    await test_db.flush()

    response = await client.get("/api/owned-groups/audit-events/export", params={"limit": 1})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["exported_at"]
    assert body["data"][0]["after_state"] == '{"link_ciphertext":"[REDACTED]"}'


@pytest.mark.asyncio
async def test_audit_is_forbidden_for_unknown_role(client):
    app.dependency_overrides[get_current_user] = lambda: {"id": 7002, "role": "viewer"}

    response = await client.get("/api/owned-groups/audit-events")

    assert response.status_code == 403
