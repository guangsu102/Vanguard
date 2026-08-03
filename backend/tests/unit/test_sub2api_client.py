from __future__ import annotations

import httpx
import pytest

from app.integrations.sub2api.client import (
    Sub2APIClient,
    Sub2APIClientConfig,
    Sub2APIError,
    close_all_sub2api_clients,
    get_sub2api_client,
)


@pytest.mark.asyncio
async def test_generate_redeem_codes_uses_stable_contract_headers_and_body() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        captured["body"] = request.content
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": [
                    {
                        "id": 7,
                        "code": "REAL-CODE",
                        "type": "balance",
                        "value": 10,
                        "status": "unused",
                    }
                ],
            },
        )

    client = Sub2APIClient(
        Sub2APIClientConfig(
            base_url="https://sub2api.example.com",
            admin_api_key="admin-test",
        )
    )
    client._client = httpx.AsyncClient(
        base_url="https://sub2api.example.com",
        transport=httpx.MockTransport(handler),
    )
    try:
        codes = await client.generate_redeem_codes(
            count=1,
            code_type="balance",
            value=10,
            expires_in_days=7,
            idempotency_key="stable-key",
        )
    finally:
        await client.close()

    assert codes[0].code == "REAL-CODE"
    assert captured["headers"]["x-api-key"] == "admin-test"
    assert captured["headers"]["idempotency-key"] == "stable-key"
    assert captured["body"] == b'{"count":1,"type":"balance","value":10,"expires_in_days":7}'


@pytest.mark.asyncio
async def test_generate_redeem_codes_rejects_redacted_replay() -> None:
    client = Sub2APIClient(
        Sub2APIClientConfig(
            base_url="https://sub2api.example.com",
            admin_api_key="admin-test",
        )
    )
    client._client = httpx.AsyncClient(
        base_url="https://sub2api.example.com",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"code": 0, "data": [{"id": 7, "code": "***"}]},
            )
        ),
    )
    try:
        with pytest.raises(Sub2APIError, match="redacted redeem code"):
            await client.generate_redeem_codes(
                count=1,
                code_type="balance",
                value=10,
                expires_in_days=7,
                idempotency_key="stable-key",
            )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_cache_changes_when_admin_key_rotates() -> None:
    first = get_sub2api_client(
        base_url="https://sub2api.example.com",
        admin_api_key="admin-one",
    )
    same = get_sub2api_client(
        base_url="https://sub2api.example.com",
        admin_api_key="admin-one",
    )
    rotated = get_sub2api_client(
        base_url="https://sub2api.example.com",
        admin_api_key="admin-two",
    )

    assert same is first
    assert rotated is not first
    await close_all_sub2api_clients()
