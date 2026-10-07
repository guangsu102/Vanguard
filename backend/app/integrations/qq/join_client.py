"""Explicit active-join bridge, separate from OneBot request approval.

NapCat's set_group_add_request is deliberately never used for active joins.
The configured bridge must implement the documented idempotent join contract.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.integrations.qq.client import OneBotAPIError


class QQJoinClient:
    def __init__(self, url: str, token: str, timeout: float = 10) -> None:
        self.url = url
        self.token = token
        self.client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self.client.aclose()

    async def request_join(
        self, *, account_id: str, group_number: str, verify_message: str, operation_id: str
    ) -> dict[str, Any]:
        try:
            response = await self.client.post(
                self.url,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Idempotency-Key": operation_id,
                },
                json={
                    "account_id": account_id,
                    "group_number": group_number,
                    "verify_message": verify_message,
                    "operation_id": operation_id,
                },
            )
        except httpx.HTTPError as exc:
            raise OneBotAPIError(
                "Active QQ join request has no confirmed result", uncertain=True
            ) from exc
        if response.status_code >= 400:
            raise OneBotAPIError(
                f"Active QQ join bridge returned HTTP {response.status_code}",
                uncertain=response.status_code >= 500,
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise OneBotAPIError(
                "Active QQ join bridge returned invalid JSON", uncertain=True
            ) from exc
        statuses = {"pending_approval", "joined", "rejected", "action_required", "unsupported"}
        if not isinstance(data, dict) or data.get("status") not in statuses:
            raise OneBotAPIError("Active QQ join bridge returned an invalid status", uncertain=True)
        if (
            str(data.get("account_id") or "") != account_id
            or str(data.get("group_number") or "") != group_number
        ):
            raise OneBotAPIError(
                "Active QQ join receipt does not match the requested account/group", uncertain=True
            )
        return data
