"""
Integration Package

Provides integration layer between Bot modules and Backend API.
"""

from app.modules.integrations.api_client import BotAPIClient
from app.modules.integrations.ws_client import WebSocketClient

__all__ = [
    "BotAPIClient",
    "WebSocketClient",
]
