from fastapi.routing import APIRoute

from app.api.guardian_bots import router
from app.core.security import require_admin


def _route(path: str, method: str) -> APIRoute:
    return next(
        route
        for route in router.routes
        if isinstance(route, APIRoute)
        and route.path == path
        and method in route.methods
    )


def test_guardian_bot_writes_require_admin() -> None:
    for route in (
        _route("", "POST"),
        _route("/{profile_id:int}", "PUT"),
    ):
        assert require_admin in {
            dependency.call for dependency in route.dependant.dependencies
        }
