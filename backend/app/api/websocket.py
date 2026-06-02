"""
WebSocket API Router

Real-time communication with WebSocket connections.
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from typing import Dict, List, Set
import json
import asyncio
from collections import defaultdict


router = APIRouter()


class ConnectionManager:
    """
    WebSocket connection manager.

    Thread-safe implementation with proper locking for concurrent access.
    """

    def __init__(self):
        self._connections: Dict[int, WebSocket] = {}
        self._subscriptions: Dict[int, Set[str]] = defaultdict(set)
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

    async def get_subscribed_clients(self, channel: str) -> List[int]:
        """Get list of clients subscribed to a channel."""
        async with self._lock:
            return [
                client_id for client_id, channels in self._subscriptions.items()
                if channel in channels
            ]


manager = ConnectionManager()


@router.websocket("/connect")
async def websocket_endpoint(
    websocket: WebSocket,
    client_id: int = Query(...),
):
    """WebSocket connection endpoint."""
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
