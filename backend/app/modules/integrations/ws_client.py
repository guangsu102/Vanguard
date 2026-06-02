"""
WebSocket Client

Provides WebSocket client for real-time communication with the Backend.
"""

import asyncio
import json
from typing import Any, Callable, Optional
import httpx
import structlog

logger = structlog.get_logger()


class WebSocketClient:
    """
    WebSocket client for real-time communication.

    Supports:
    - Real-time message receiving
    - Event subscriptions
    - Automatic reconnection
    """

    def __init__(
        self,
        base_url: str,
        client_id: int,
        api_key: str,
        reconnect_delay: float = 5.0,
        max_reconnects: int = 10,
    ):
        """
        Initialize WebSocket client.

        Args:
            base_url: Base URL of the Backend API (WebSocket endpoint)
            client_id: Unique client identifier
            api_key: API key for authentication
            reconnect_delay: Delay between reconnection attempts
            max_reconnects: Maximum number of reconnection attempts
        """
        self.base_url = base_url.rstrip("/").replace("http://", "ws://").replace("https://", "wss://")
        self.ws_url = f"{self.base_url}/api/ws/connect?client_id={client_id}"
        self.client_id = client_id
        self.api_key = api_key
        self.reconnect_delay = reconnect_delay
        self.max_reconnects = max_reconnects

        self._ws: Optional[Any] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._running = False
        self._handlers: dict[str, list[Callable]] = {}
        self._subscriptions: set[str] = set()
        self._lock = asyncio.Lock()

    def on(self, event: str, handler: Callable) -> None:
        """
        Register an event handler.

        Args:
            event: Event name (e.g., "message:new", "account:status")
            handler: Callback function to handle the event
        """
        if event not in self._handlers:
            self._handlers[event] = []
        self._handlers[event].append(handler)

    def off(self, event: str, handler: Optional[Callable] = None) -> None:
        """
        Unregister an event handler.

        Args:
            event: Event name
            handler: Specific handler to remove, or None to remove all
        """
        if event not in self._handlers:
            return

        if handler is None:
            self._handlers[event].clear()
        else:
            self._handlers[event] = [h for h in self._handlers[event] if h != handler]

    async def connect(self) -> bool:
        """
        Connect to the WebSocket server.

        Returns:
            True if connection successful, False otherwise
        """
        try:
            import websockets

            headers = {"Authorization": f"Bearer {self.api_key}"}
            self._ws = await websockets.connect(self.ws_url, extra_headers=headers)
            self._running = True

            logger.info("websocket_connected", client_id=self.client_id)

            await self._send({"type": "subscribe", "channel": "stats:update"})

            return True

        except Exception as e:
            logger.error("websocket_connect_failed", error=str(e), client_id=self.client_id)
            return False

    async def disconnect(self) -> None:
        """Disconnect from the WebSocket server."""
        self._running = False

        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None

        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        logger.info("websocket_disconnected", client_id=self.client_id)

    async def _reader(self) -> None:
        """Read messages from WebSocket and dispatch to handlers."""
        while self._running and self._ws:
            try:
                message = await self._ws.recv()
                await self._handle_message(message)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("websocket_read_error", error=str(e))
                if self._running:
                    await asyncio.sleep(self.reconnect_delay)
                    await self._ensure_connection()

    async def _handle_message(self, raw_message: str) -> None:
        """Handle incoming WebSocket message."""
        try:
            message = json.loads(raw_message)
            event_type = message.get("type", "unknown")
            data = message.get("data", {})

            if event_type == "connected":
                logger.info(
                    "websocket_confirmed",
                    client_id=message.get("client_id"),
                )

            handlers = self._handlers.get(event_type, [])
            handlers.extend(self._handlers.get("*", []))

            for handler in handlers:
                try:
                    if asyncio.iscoroutinefunction(handler):
                        await handler(data)
                    else:
                        handler(data)
                except Exception as e:
                    logger.error(
                        "websocket_handler_error",
                        handler=handler.__name__,
                        error=str(e),
                    )

        except json.JSONDecodeError:
            logger.warning("websocket_invalid_json", raw=raw_message[:100])

    async def _ensure_connection(self) -> bool:
        """Ensure WebSocket connection is active."""
        if self._ws is None:
            return await self.connect()
        return True

    async def _send(self, message: dict) -> bool:
        """
        Send message through WebSocket.

        Args:
            message: Message dictionary to send

        Returns:
            True if sent successfully, False otherwise
        """
        if not await self._ensure_connection():
            return False

        try:
            await self._ws.send(json.dumps(message))
            return True
        except Exception as e:
            logger.error("websocket_send_failed", error=str(e))
            return False

    async def subscribe(self, channel: str) -> bool:
        """
        Subscribe to a channel.

        Args:
            channel: Channel name (e.g., "message:new", "account:status")

        Returns:
            True if subscription successful
        """
        if channel in self._subscriptions:
            return True

        success = await self._send({"type": "subscribe", "channel": channel})
        if success:
            self._subscriptions.add(channel)
            logger.info("websocket_subscribed", channel=channel)

        return success

    async def unsubscribe(self, channel: str) -> bool:
        """
        Unsubscribe from a channel.

        Args:
            channel: Channel name

        Returns:
            True if unsubscription successful
        """
        if channel not in self._subscriptions:
            return True

        success = await self._send({"type": "unsubscribe", "channel": channel})
        if success:
            self._subscriptions.discard(channel)
            logger.info("websocket_unsubscribed", channel=channel)

        return success

    async def ping(self) -> bool:
        """
        Send ping to keep connection alive.

        Returns:
            True if ping sent successfully
        """
        return await self._send({"type": "ping"})

    async def start(self) -> None:
        """Start the WebSocket client."""
        if await self.connect():
            self._reader_task = asyncio.create_task(self._reader())

            asyncio.create_task(self._heartbeat())

    async def _heartbeat(self) -> None:
        """Send periodic pings to keep connection alive."""
        while self._running:
            await asyncio.sleep(30)
            if self._running:
                await self.ping()


class WebSocketManager:
    """
    Manages multiple WebSocket connections.
    """

    def __init__(self):
        self._clients: dict[int, WebSocketClient] = {}
        self._lock = asyncio.Lock()

    async def add_client(
        self,
        client_id: int,
        base_url: str,
        api_key: str,
    ) -> WebSocketClient:
        """
        Add a new WebSocket client.

        Args:
            client_id: Unique client identifier
            base_url: Base URL of the Backend API
            api_key: API key for authentication

        Returns:
            WebSocketClient instance
        """
        async with self._lock:
            if client_id in self._clients:
                await self._clients[client_id].disconnect()

            client = WebSocketClient(base_url, client_id, api_key)
            self._clients[client_id] = client
            return client

    async def remove_client(self, client_id: int) -> None:
        """Remove a WebSocket client."""
        async with self._lock:
            if client_id in self._clients:
                await self._clients[client_id].disconnect()
                del self._clients[client_id]

    async def get_client(self, client_id: int) -> Optional[WebSocketClient]:
        """Get a WebSocket client by ID."""
        return self._clients.get(client_id)

    async def broadcast(self, message: dict, exclude: Optional[list[int]] = None) -> None:
        """
        Broadcast message to all connected clients.

        Args:
            message: Message dictionary to send
            exclude: List of client IDs to exclude
        """
        exclude = exclude or []
        for client_id, client in self._clients.items():
            if client_id not in exclude:
                await client._send(message)


# Global WebSocket manager instance
ws_manager = WebSocketManager()
