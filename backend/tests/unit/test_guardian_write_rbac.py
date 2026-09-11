import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from app.api.campaigns import router as campaigns_router
from app.api.group_governance import router as group_governance_router
from app.api.guardian_validation import require_guardian_operator
from app.api.moderation_sensitive_keywords import router as sensitive_keywords_router
from app.api.rules import router as rules_router
from app.core.security import get_current_user
from app.main import app

MUTATION_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
GUARDIAN_ROUTERS = (
    group_governance_router,
    rules_router,
    sensitive_keywords_router,
    campaigns_router,
)


def _guardian_routes() -> list[APIRoute]:
    return [
        route
        for router in GUARDIAN_ROUTERS
        for route in router.routes
        if isinstance(route, APIRoute)
    ]


def test_every_guardian_mutation_route_requires_operator() -> None:
    routes = [route for route in _guardian_routes() if route.methods & MUTATION_METHODS]
    assert routes

    missing = []
    for route in routes:
        dependency_calls = {dependency.call for dependency in route.dependant.dependencies}
        if require_guardian_operator not in dependency_calls:
            missing.append(f"{sorted(route.methods & MUTATION_METHODS)} {route.path}")

    assert missing == []


def test_guardian_get_routes_remain_read_only_without_operator_dependency() -> None:
    routes = [route for route in _guardian_routes() if route.methods == {"GET"}]
    assert routes
    for route in routes:
        dependency_calls = {dependency.call for dependency in route.dependant.dependencies}
        assert require_guardian_operator not in dependency_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["admin", "operator"])
async def test_guardian_operator_allows_admin_and_operator(role: str) -> None:
    current_user = {"id": 1, "username": role, "role": role}
    assert await require_guardian_operator(current_user) is current_user


@pytest.mark.asyncio
async def test_guardian_operator_rejects_auditor_with_stable_reason() -> None:
    with pytest.raises(HTTPException) as caught:
        await require_guardian_operator(
            {"id": 2, "username": "auditor", "role": "auditor"}
        )

    assert caught.value.status_code == 403
    assert caught.value.detail == {
        "reason": "owned_group_role_forbidden",
        "message": "Guardian operator access required",
        "retryable": False,
    }


@pytest.fixture
def auditor_authentication():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 2,
        "username": "auditor",
        "role": "auditor",
    }
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PUT", "/api/group-governance/verification"),
        ("POST", "/api/rules/test"),
        ("POST", "/api/moderation-sensitive-keywords"),
        ("POST", "/api/campaigns"),
    ],
)
async def test_auditor_is_rejected_by_each_guardian_write_router(
    client,
    auditor_authentication,
    method: str,
    path: str,
) -> None:
    response = await client.request(method, path, json={})

    assert response.status_code == 403
    assert response.json()["detail"] == {
        "reason": "owned_group_role_forbidden",
        "message": "Guardian operator access required",
        "retryable": False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/group-governance/verification/-1001234567890",
        "/api/rules?limit=1",
        "/api/moderation-sensitive-keywords?limit=1",
        "/api/campaigns?limit=1",
    ],
)
async def test_auditor_can_still_reach_guardian_read_routes(
    client,
    auditor_authentication,
    path: str,
) -> None:
    response = await client.get(path)

    assert response.status_code != 403
