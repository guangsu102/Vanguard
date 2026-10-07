import httpx
import pytest

from app.integrations.qq.client import OneBotAPIError
from app.integrations.qq.join_client import QQJoinClient


@pytest.mark.asyncio
async def test_active_join_uses_explicit_bridge_and_idempotency_key():
    def handler(request):
        assert request.url.path == "/active-join"
        assert request.headers["Idempotency-Key"] == "operation-123"
        return httpx.Response(
            200,
            json={
                "status": "pending_approval",
                "account_id": "10001",
                "group_number": "123456789",
            },
        )

    bridge = QQJoinClient("http://bridge.test/active-join", "token")
    await bridge.client.aclose()
    bridge.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await bridge.request_join(
            account_id="10001",
            group_number="123456789",
            verify_message="hello",
            operation_id="operation-123",
        )
        assert result["status"] == "pending_approval"
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_join_receipt_for_different_account_is_uncertain():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "status": "joined",
                "account_id": "10002",
                "group_number": "123456789",
            },
        )

    bridge = QQJoinClient("http://bridge.test/active-join", "token")
    await bridge.client.aclose()
    bridge.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(OneBotAPIError) as caught:
            await bridge.request_join(
                account_id="10001",
                group_number="123456789",
                verify_message="",
                operation_id="operation-123",
            )
        assert caught.value.uncertain
    finally:
        await bridge.close()
