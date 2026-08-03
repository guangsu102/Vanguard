from fastapi import APIRouter

from app import main
from app.api import websocket as websocket_router
from app.api.websocket import start_redis_bridge, stop_redis_bridge


def test_main_uses_router_and_lifecycle_functions_from_websocket_module() -> None:
    assert isinstance(websocket_router, APIRouter)
    assert main.websocket_router is websocket_router
    assert main.start_redis_bridge is start_redis_bridge
    assert main.stop_redis_bridge is stop_redis_bridge
