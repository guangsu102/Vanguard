from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.security import get_current_user
from app.main import app
from app.modules.owned_group.operations_read_model import OwnedGroupOperationsReadModel
from app.modules.owned_group.operations_schemas import (
    Coverage,
    MemberSummary,
    MessagingSummary,
    OperationsCenterData,
    OwnedGroupMember,
)


@pytest.fixture(autouse=True)
def override_authenticated_user():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 7501,
        "username": "stage-five-reader",
        "role": "operator",
    }
    yield
    app.dependency_overrides.pop(get_current_user, None)


def _coverage() -> Coverage:
    return Coverage(
        coverage_status="collecting",
        blocking_reasons=["telegram_full_roster_not_loaded"],
    )


def _assert_error_envelope(response, *, status_code: int, code: str, retryable: bool):
    assert response.status_code == status_code
    body = response.json()
    assert set(body) == {"error", "correlation_id"}
    assert set(body["error"]) == {"code", "message", "details", "retryable"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]
    assert isinstance(body["error"]["details"], dict)
    assert body["error"]["retryable"] is retryable
    assert body["correlation_id"] == response.headers["X-Correlation-ID"]
    return body


def _member_summary() -> MemberSummary:
    return MemberSummary(
        managed_resource_count=1,
        observed_real_user_count=1,
        counts_by_kind={
            "real_user": 1,
            "system_ad_account": 0,
            "system_bot": 0,
        },
        coverage=_coverage(),
    )


def _member() -> OwnedGroupMember:
    return OwnedGroupMember(
        member_key="telegram:551105",
        member_kind="real_user",
        classification_status="resolved",
        classification_reason="observed_real_user",
        telegram_user_id=551105,
        display_name="Stage Five User",
        username="stage_five_user",
        primary_source="guardian_observation",
        sources=[
            {
                "source": "guardian_observation",
                "record_id": 91,
                "observed_at": datetime(2026, 9, 11, tzinfo=UTC),
            }
        ],
        telegram_role="member",
        is_admin=False,
        presence_status="present",
        presence_confidence="observed",
        last_observed_at=datetime(2026, 9, 11, tzinfo=UTC),
        data_quality="partial",
        quality_codes=["telegram_full_roster_not_loaded"],
    )


def _operations_data(role: str) -> OperationsCenterData:
    now = datetime(2026, 9, 11, tzinfo=UTC)
    section = {
        "state": "ready",
        "can_view": True,
        "can_manage": role in {"admin", "operator"},
        "can_execute": False,
        "blocking_reasons": [],
    }
    return OperationsCenterData(
        asset={
            "asset_id": 51,
            "internal_name": "stage-five",
            "title": "Stage Five",
            "visibility": "private",
            "asset_status": "ready",
            "telegram_chat_id": -100551105,
            "core_group_id": 61,
            "managed_binding_id": 71,
            "guardian_bot_account_id": 81,
            "owner_account_id": 82,
            "managed_resource_count": 1,
            "updated_at": now,
        },
        sections={
            "orchestration": section,
            "governance": section,
            "members": {**section, "can_manage": False},
            "messaging": section,
            "activities": section,
            "audit": {**section, "can_manage": False},
        },
        governance={
            "status": "managed",
            "bot_account_id": 81,
            "bot_role": "admin",
            "health_status": "healthy",
            "last_checked_at": now,
        },
        messaging=MessagingSummary(
            static_enabled=True,
            runtime_enabled=True,
            dry_run=True,
            can_send=False,
            policy_count=1,
            enabled_policy_count=1,
            pending_review_count=0,
            sent_today=0,
        ),
        member_summary=_member_summary(),
        permissions={
            "manage_orchestration": role in {"admin", "operator"},
            "manage_governance": role in {"admin", "operator"},
            "manage_messaging": role == "admin",
            "manage_persona": role == "admin",
            "manage_activities": role in {"admin", "operator"},
            "view_members": True,
            "view_audit": True,
        },
        snapshot_at=now,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["admin", "operator", "auditor"])
async def test_operations_center_allows_audit_reader_roles(
    client,
    monkeypatch,
    role,
):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 7501,
        "username": "stage-five-reader",
        "role": role,
    }
    captured = {}

    async def fake_summary(self, asset_id, *, role, correlation_id=None):
        captured.update(
            asset_id=asset_id,
            role=role,
            correlation_id=correlation_id,
        )
        return _operations_data(role)

    monkeypatch.setattr(
        OwnedGroupOperationsReadModel,
        "get_operations_center",
        fake_summary,
    )

    response = await client.get(
        "/api/owned-groups/51/operations-center",
        headers={"X-Correlation-ID": "stage5-api-contract"},
    )

    assert response.status_code == 200
    assert response.headers["X-Correlation-ID"] == "stage5-api-contract"
    assert response.json()["correlation_id"] == "stage5-api-contract"
    assert response.json()["data"]["asset"]["asset_id"] == 51
    assert captured == {
        "asset_id": 51,
        "role": role,
        "correlation_id": "stage5-api-contract",
    }


@pytest.mark.asyncio
async def test_members_endpoint_validates_and_forwards_query(client, monkeypatch):
    captured = {}

    async def fake_members(self, asset_id, query, *, correlation_id=None):
        captured.update(
            asset_id=asset_id,
            query=query,
            correlation_id=correlation_id,
        )
        return [_member()], 1, _coverage(), _member_summary()

    monkeypatch.setattr(
        OwnedGroupOperationsReadModel,
        "list_members",
        fake_members,
    )

    response = await client.get(
        "/api/owned-groups/51/members",
        params={
            "member_kind": "real_user",
            "presence_status": "present",
            "q": "  @stage_five  ",
            "sort": "name_asc",
            "offset": 5,
            "limit": 10,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["offset"] == 5
    assert body["limit"] == 10
    assert body["coverage"]["is_complete"] is False
    assert body["data"][0]["member_key"] == "telegram:551105"
    assert captured["asset_id"] == 51
    assert captured["query"].q == "@stage_five"
    assert captured["query"].sort == "name_asc"
    assert captured["correlation_id"].startswith("group-ops-")


@pytest.mark.asyncio
async def test_member_query_length_is_checked_after_trimming(client, monkeypatch):
    captured = {}

    async def fake_members(self, asset_id, query, *, correlation_id=None):
        captured["query"] = query
        return [], 0, _coverage(), _member_summary()

    monkeypatch.setattr(
        OwnedGroupOperationsReadModel,
        "list_members",
        fake_members,
    )

    response = await client.get(
        "/api/owned-groups/51/members",
        params={"q": f"  {'a' * 100}  "},
    )

    assert response.status_code == 200
    assert captured["query"].q == "a" * 100


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"member_kind": "advertiser"},
        {"limit": 101},
        {"offset": -1},
        {"q": "   "},
        {"q": "a" * 101},
    ],
)
async def test_members_invalid_query_uses_dedicated_error_envelope(client, params):
    response = await client.get("/api/owned-groups/51/members", params=params)

    body = _assert_error_envelope(
        response,
        status_code=422,
        code="MEMBER_QUERY_INVALID",
        retryable=False,
    )
    assert body["error"]["message"] == "Member query parameters are invalid"
    assert body["error"]["details"]["validation_errors"]


@pytest.mark.asyncio
async def test_operations_endpoints_reject_non_reader_role(client):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 7502,
        "username": "viewer",
        "role": "viewer",
    }

    response = await client.get("/api/owned-groups/51/members")

    body = _assert_error_envelope(
        response,
        status_code=403,
        code="OWNED_GROUP_ROLE_FORBIDDEN",
        retryable=False,
    )
    assert body["error"]["message"] == "Owned group operations read access is forbidden"


@pytest.mark.asyncio
async def test_operations_error_adapter_does_not_change_legacy_routes(client):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 7502,
        "username": "viewer",
        "role": "viewer",
    }

    response = await client.get("/api/owned-groups/audit-events")

    assert response.status_code == 403
    assert "error" not in response.json()
    assert response.json()["detail"]["reason"] == "owned_group_role_forbidden"


@pytest.mark.asyncio
async def test_operations_endpoints_require_authentication(client):
    app.dependency_overrides.pop(get_current_user, None)

    response = await client.get("/api/owned-groups/51/members")

    body = _assert_error_envelope(
        response,
        status_code=401,
        code="AUTHENTICATION_REQUIRED",
        retryable=False,
    )
    assert body["error"]["message"] == "Authentication is required"


@pytest.mark.asyncio
async def test_missing_asset_uses_dedicated_not_found_envelope(client):
    response = await client.get("/api/owned-groups/999999/members")

    body = _assert_error_envelope(
        response,
        status_code=404,
        code="OWNED_GROUP_NOT_FOUND",
        retryable=False,
    )
    assert body["error"] == {
        "code": "OWNED_GROUP_NOT_FOUND",
        "message": "Owned group asset was not found",
        "details": {},
        "retryable": False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "method_name", "expected_code"),
    [
        (
            "/api/owned-groups/51/operations-center",
            "get_operations_center",
            "OPERATIONS_CENTER_READ_FAILED",
        ),
        (
            "/api/owned-groups/51/members",
            "list_members",
            "MEMBER_SOURCE_READ_FAILED",
        ),
    ],
)
async def test_read_failure_uses_retryable_sanitized_503(
    client,
    monkeypatch,
    path,
    method_name,
    expected_code,
):
    async def fail(*args, **kwargs):
        raise RuntimeError("bot_token=551105:THIS_MUST_NOT_LEAK")

    monkeypatch.setattr(OwnedGroupOperationsReadModel, method_name, fail)

    response = await client.get(
        path,
        headers={"X-Correlation-ID": "bad correlation with spaces"},
    )

    body = _assert_error_envelope(
        response,
        status_code=503,
        code=expected_code,
        retryable=True,
    )
    assert "THIS_MUST_NOT_LEAK" not in response.text
    assert body["correlation_id"].startswith("group-ops-")
