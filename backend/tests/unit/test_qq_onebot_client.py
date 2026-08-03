from __future__ import annotations

import json

import httpx
import pytest

from app.integrations.qq import OneBotAPIError, OneBotClient


@pytest.mark.asyncio
async def test_onebot_client_authenticates_and_sends_group_message() -> None:
    sent_payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer token-1"
        if request.url.path == "/get_login_info":
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "retcode": 0,
                    "data": {"user_id": 10001, "nickname": "Vanguard"},
                },
            )
        if request.url.path == "/send_group_msg":
            sent_payloads.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={"status": "ok", "retcode": 0, "data": {"message_id": 9001}},
            )
        return httpx.Response(404, json={"message": "not found"})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OneBotClient(
        account_id="10001",
        http_url="http://napcat.test",
        websocket_url="ws://napcat.test",
        access_token="token-1",
        http_client=http_client,
    )
    try:
        login = await client.get_login_info()
        result = await client.send_group_message("123456789", "maintenance at 22:00")
    finally:
        await http_client.aclose()

    assert login["user_id"] == 10001
    assert result["message_id"] == 9001
    assert sent_payloads == [
        {
            "group_id": 123456789,
            "message": "maintenance at 22:00",
            "auto_escape": True,
        }
    ]


@pytest.mark.asyncio
async def test_onebot_write_timeout_is_marked_uncertain() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OneBotClient(
        account_id="10001",
        http_url="http://napcat.test",
        access_token="token-1",
        http_client=http_client,
    )
    try:
        with pytest.raises(OneBotAPIError) as caught:
            await client.send_group_message("123456789", "hello")
    finally:
        await http_client.aclose()

    assert caught.value.uncertain is True


@pytest.mark.asyncio
async def test_onebot_retcode_error_is_exposed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": "failed", "retcode": 1200, "wording": "group not found"},
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OneBotClient(
        account_id="10001",
        http_url="http://napcat.test",
        access_token="token-1",
        http_client=http_client,
    )
    try:
        with pytest.raises(OneBotAPIError, match="group not found") as caught:
            await client.send_group_message("123456789", "hello")
    finally:
        await http_client.aclose()

    assert caught.value.retcode == 1200


@pytest.mark.asyncio
async def test_onebot_recall_accepts_signed_message_ids() -> None:
    deleted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        deleted.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": None})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OneBotClient(
        account_id="10001",
        http_url="http://napcat.test",
        access_token="token-1",
        http_client=http_client,
    )
    try:
        await client.recall_group_message("123456789", "-9001")
    finally:
        await http_client.aclose()

    assert deleted == [{"message_id": -9001}]
