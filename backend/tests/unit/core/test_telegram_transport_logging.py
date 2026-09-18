import logging

import httpx
import pytest

from app.integrations.telegram.client import TelegramClient, TelegramConfig


@pytest.mark.asyncio
async def test_telegram_client_suppresses_transport_urls_containing_bot_token(caplog):
    token = "123456789:test-secret-token"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "id": 123456789,
                    "is_bot": True,
                    "first_name": "Test",
                    "username": "test_bot",
                },
            },
        )

    httpx_logger = logging.getLogger("httpx")
    httpcore_logger = logging.getLogger("httpcore")
    original_httpx_level = httpx_logger.level
    original_httpcore_level = httpcore_logger.level
    try:
        httpx_logger.setLevel(logging.INFO)
        httpcore_logger.setLevel(logging.INFO)
        with caplog.at_level(logging.INFO):
            client = TelegramClient(TelegramConfig(bot_token=token))
            client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                await client.get_me()
            finally:
                await client.close()

        assert httpx_logger.level == logging.WARNING
        assert httpcore_logger.level == logging.WARNING
        assert token not in caplog.text
        assert "api.telegram.org/bot" not in caplog.text
    finally:
        httpx_logger.setLevel(original_httpx_level)
        httpcore_logger.setLevel(original_httpcore_level)
