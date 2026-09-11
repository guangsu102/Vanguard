import inspect

from fastapi import APIRouter

from app import main
from app.api import websocket as websocket_router
from app.api.websocket import start_redis_bridge, stop_redis_bridge


def test_main_uses_router_and_lifecycle_functions_from_websocket_module() -> None:
    assert isinstance(websocket_router, APIRouter)
    assert main.websocket_router is websocket_router
    assert main.start_redis_bridge is start_redis_bridge
    assert main.stop_redis_bridge is stop_redis_bridge


def test_lifespan_refreshes_persona_gauge_after_database_before_other_services() -> None:
    source = inspect.getsource(main.lifespan)
    assert source.index("await init_db") < source.index(
        "await refresh_persona_configured_gauge_at_startup"
    ) < source.index("await init_redis")
    assert "except Exception:" in source
