import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from app.api.guardian_validation import require_guardian_reader
from app.api.managed_groups import router as managed_groups_router
from app.core.security import get_current_user
from app.main import app


def _managed_group_read_routes() -> list[APIRoute]:
    return [
        route
        for route in managed_groups_router.routes
        if isinstance(route, APIRoute) and route.methods == {"GET"}
    ]


def test_every_managed_group_read_route_requires_guardian_reader() -> None:
    routes = _managed_group_read_routes()
    assert routes

    missing = []
    for route in routes:
        dependency_calls = {dependency.call for dependency in route.dependant.dependencies}
        if require_guardian_reader not in dependency_calls:
            missing.append(route.path)

    assert missing == []


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["admin", "operator", "auditor"])
async def test_guardian_reader_allows_operational_roles(role: str) -> None:
    current_user = {"id": 1, "username": role, "role": role}
    assert await require_guardian_reader(current_user) is current_user


@pytest.mark.asyncio
async def test_guardian_reader_rejects_other_authenticated_roles() -> None:
    with pytest.raises(HTTPException) as caught:
        await require_guardian_reader(
            {"id": 2, "username": "viewer", "role": "viewer"}
        )

    assert caught.value.status_code == 403
    assert caught.value.detail == {
        "reason": "owned_group_role_forbidden",
        "message": "Guardian read access required",
        "retryable": False,
    }


@pytest.fixture
def viewer_authentication():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 2,
        "username": "viewer",
        "role": "viewer",
    }
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/managed-groups",
        "/api/managed-groups/1/pinned-message-config",
    ],
)
async def test_other_authenticated_roles_cannot_read_managed_group_state(
    client,
    viewer_authentication,
    path: str,
) -> None:
    response = await client.get(path)

    assert response.status_code == 403
    assert response.json()["detail"] == {
        "reason": "owned_group_role_forbidden",
        "message": "Guardian read access required",
        "retryable": False,
    }
