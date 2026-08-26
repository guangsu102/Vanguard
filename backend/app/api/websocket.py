"""
WebSocket API Router

Real-time communication with WebSocket connections.
"""

import asyncio
import contextlib
import json
from collections import defaultdict

import structlog
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status

from app.core.security import verify_access_token

router = APIRouter()


class ConnectionManager:
    """
    WebSocket connection manager.

    Thread-safe implementation with proper locking for concurrent access.
    """

    def __init__(self):
        self._connections: dict[int, WebSocket] = {}
        self._subscriptions: dict[int, set[str]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, client_id: int):
        """Connect a new client."""
        async with self._lock:
            await websocket.accept()
            self._connections[client_id] = websocket
            self._subscriptions[client_id] = set()

    def disconnect(self, client_id: int):
        """Disconnect a client (non-blocking version for cleanup)."""
        if client_id in self._connections:
            del self._connections[client_id]
        if client_id in self._subscriptions:
            del self._subscriptions[client_id]

    async def disconnect_safe(self, client_id: int):
        """Safely disconnect a client with lock."""
        async with self._lock:
            self.disconnect(client_id)

    async def send_personal_message(self, message: dict, client_id: int):
        """Send message to specific client."""
        async with self._lock:
            if client_id in self._connections:
                try:
                    await self._connections[client_id].send_json(message)
                except Exception:
                    # Connection might be closed, clean up
                    del self._connections[client_id]

    async def broadcast(self, message: dict):
        """Broadcast message to all connected clients."""
        async with self._lock:
            disconnected = []

            for client_id, connection in self._connections.items():
                try:
                    await connection.send_json(message)
                except Exception:
                    disconnected.append(client_id)

            # Clean up disconnected clients
            for client_id in disconnected:
                del self._connections[client_id]

    async def broadcast_to_channel(self, message: dict, channel: str):
        """Broadcast message to clients subscribed to a channel."""
        async with self._lock:
            disconnected = []

            for client_id, connection in self._connections.items():
                if channel in self._subscriptions.get(client_id, set()):
                    try:
                        await connection.send_json(message)
                    except Exception:
                        disconnected.append(client_id)

            # Clean up disconnected clients
            for client_id in disconnected:
                del self._connections[client_id]
                if client_id in self._subscriptions:
                    del self._subscriptions[client_id]

    async def subscribe(self, client_id: int, channel: str):
        """Subscribe a client to a channel."""
        async with self._lock:
            self._subscriptions[client_id].add(channel)

    async def unsubscribe(self, client_id: int, channel: str):
        """Unsubscribe a client from a channel."""
        async with self._lock:
            if client_id in self._subscriptions:
                self._subscriptions[client_id].discard(channel)

    async def get_client_count(self) -> int:
        """Get the number of connected clients."""
        async with self._lock:
            return len(self._connections)

    async def get_subscribed_clients(self, channel: str) -> list[int]:
        """Get list of clients subscribed to a channel."""
        async with self._lock:
            return [
                client_id for client_id, channels in self._subscriptions.items()
                if channel in channels
            ]


manager = ConnectionManager()
logger = structlog.get_logger()
_redis_bridge_task: asyncio.Task | None = None


async def _redis_bridge_loop() -> None:
    """Fan Redis events into the WebSocket connections owned by this API process."""
    from app.core.redis import get_redis
    from app.modules.qq.service import QQ_WS_CHANNEL
    from app.modules.private_chat.service import PRIVATE_CHAT_WS_REDIS_CHANNEL

    while True:
        pubsub = None
        try:
            client = await get_redis()
            pubsub = client.pubsub()
            await pubsub.subscribe(QQ_WS_CHANNEL, PRIVATE_CHAT_WS_REDIS_CHANNEL)
            async for item in pubsub.listen():
                if item.get("type") != "message":
                    continue
                try:
                    payload = json.loads(item.get("data") or "{}")
                    channel = str(payload.get("channel") or "")
                    message = payload.get("message")
                    if channel and isinstance(message, dict):
                        await manager.broadcast_to_channel(message, channel)
                except (TypeError, json.JSONDecodeError):
                    logger.warning("websocket_redis_bridge_invalid_payload")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("websocket_redis_bridge_failed", error=str(exc))
            await asyncio.sleep(5)
        finally:
            if pubsub is not None:
                with contextlib.suppress(Exception):
                    await pubsub.aclose()


async def start_redis_bridge() -> None:
    global _redis_bridge_task
    if _redis_bridge_task is None or _redis_bridge_task.done():
        _redis_bridge_task = asyncio.create_task(
            _redis_bridge_loop(),
            name="websocket-redis-bridge",
        )


async def stop_redis_bridge() -> None:
    global _redis_bridge_task
    if _redis_bridge_task is None:
        return
    _redis_bridge_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await _redis_bridge_task
    _redis_bridge_task = None


@router.websocket("/connect")
async def websocket_endpoint(
    websocket: WebSocket,
    client_id: int = Query(...),
    token: str | None = Query(None),
):
    """WebSocket connection endpoint with token authentication."""
    # Authenticate via token query parameter
    if not token:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Missing authentication token")
        return

    try:
        user = verify_access_token(token)
        if not user:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid or expired token")
            return
    except Exception:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Authentication failed")
        return

    await manager.connect(websocket, client_id)

    try:
        # Send connection confirmation
        await manager.send_personal_message(
            {"type": "connected", "client_id": client_id},
            client_id
        )

        # Listen for messages
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)

            # Handle different message types
            if message.get("type") == "ping":
                await manager.send_personal_message(
                    {"type": "pong"},
                    client_id
                )

            elif message.get("type") == "subscribe":
                channel = message.get("channel")
                if channel:
                    await manager.subscribe(client_id, channel)
                await manager.send_personal_message(
                    {"type": "subscribed", "channel": channel},
                    client_id
                )

            elif message.get("type") == "unsubscribe":
                channel = message.get("channel")
                if channel:
                    await manager.unsubscribe(client_id, channel)
                await manager.send_personal_message(
                    {"type": "unsubscribed", "channel": channel},
                    client_id
                )

    except WebSocketDisconnect:
        await manager.disconnect_safe(client_id)
        # Broadcast disconnection
        await manager.broadcast({
            "type": "client_disconnected",
            "client_id": client_id,
        })
    except json.JSONDecodeError:
        # Invalid JSON received
        await manager.send_personal_message(
            {"type": "error", "message": "Invalid JSON format"},
            client_id
        )
    except Exception:
        # Unexpected error
        await manager.disconnect_safe(client_id)
        await manager.broadcast({
            "type": "client_error",
            "client_id": client_id,
        })


# Channel constants
class Channels:
    """WebSocket channel constants."""
    MESSAGE_NEW = "message:new"
    ACCOUNT_STATUS = "account:status"
    VIOLATION_NEW = "violation:new"
    STATS_UPDATE = "stats:update"
    PROXY_HEALTH = "proxy:health"
    CAMPAIGN_UPDATE = "campaign:update"
    USER_ACTIVITY = "user:activity"
    QQ_MESSAGES = "qq:messages"
    QQ_GROUPS = "qq:groups"
    TELEGRAM_PRIVATE_CHATS = "telegram:private-chats"
