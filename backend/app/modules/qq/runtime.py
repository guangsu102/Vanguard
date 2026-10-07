"""Resolve each QQ account's credentials without falling back to another account."""

from __future__ import annotations

from app.core.config import settings
from app.core.ephemeral_secret import decrypt_ephemeral_secret
from app.integrations.qq.client import OneBotAPIError, OneBotClient
from app.integrations.qq.join_client import QQJoinClient
from app.modules.qq.models import QQBotConnection


def client_for_connection(connection: QQBotConnection) -> OneBotClient:
    is_default = connection.app_id == (settings.QQ_ONEBOT_ACCOUNT_ID or "").strip()
    token = (
        decrypt_ephemeral_secret(connection.access_token_encrypted)
        if connection.access_token_encrypted
        else settings.QQ_ONEBOT_ACCESS_TOKEN
        if is_default
        else None
    )
    http_url = connection.http_url or (settings.QQ_ONEBOT_HTTP_URL if is_default else "")
    if not http_url or not token:
        raise OneBotAPIError("QQ account has no configured OneBot connection")
    return OneBotClient(
        account_id=connection.app_id,
        http_url=http_url,
        websocket_url=connection.websocket_url or (settings.QQ_ONEBOT_WS_URL if is_default else ""),
        access_token=token,
    )


def join_client_for_connection(connection: QQBotConnection) -> QQJoinClient | None:
    if not connection.join_api_url or not connection.join_api_token_encrypted:
        return None
    return QQJoinClient(
        connection.join_api_url,
        decrypt_ephemeral_secret(connection.join_api_token_encrypted) or "",
        settings.QQ_ONEBOT_REQUEST_TIMEOUT_SECONDS,
    )
