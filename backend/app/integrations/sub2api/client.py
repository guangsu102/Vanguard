"""Sub2API Admin API client for redeem-code coupon generation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import httpx
import structlog

logger = structlog.get_logger()


@dataclass(slots=True)
class Sub2APIClientConfig:
    base_url: str
    admin_api_key: str
    timeout: float = 5.0


@dataclass(slots=True)
class Sub2APIRedeemCode:
    id: Optional[int]
    code: str
    type: str
    value: float
    status: str
    expires_at: Optional[str] = None


class Sub2APIError(RuntimeError):
    """Raised when Sub2API returns an error response."""

    def __init__(self, message: str, status_code: Optional[int] = None, payload: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload or {}


class Sub2APIClient:
    """Small async client for Sub2API admin redeem-code endpoints."""

    def __init__(self, config: Sub2APIClientConfig):
        self.config = config
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.config.base_url.rstrip("/"),
                timeout=self.config.timeout,
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict[str, Any]:
        client = await self._get_client()
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.config.admin_api_key,
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        response = await client.request(method.upper(), path, headers=headers, json=json_body)
        try:
            payload = response.json()
        except ValueError:
            payload = {}

        if response.status_code >= 400:
            message = payload.get("message") or payload.get("error") or response.text
            raise Sub2APIError(message, status_code=response.status_code, payload=payload)

        code = payload.get("code")
        if code not in (0, None):
            message = payload.get("message") or payload.get("error") or "sub2api error"
            raise Sub2APIError(message, status_code=response.status_code, payload=payload)

        return payload

    async def generate_redeem_codes(
        self,
        *,
        count: int,
        code_type: str,
        value: float,
        expires_at: Optional[datetime] = None,
        expires_in_days: Optional[int] = None,
        group_id: Optional[int] = None,
        validity_days: Optional[int] = None,
        idempotency_key: Optional[str] = None,
    ) -> list[Sub2APIRedeemCode]:
        body: dict[str, Any] = {
            "count": count,
            "type": code_type,
            "value": value,
        }
        if expires_at is not None:
            body["expires_at"] = expires_at.isoformat().replace("+00:00", "Z")
        if expires_in_days is not None:
            body["expires_in_days"] = expires_in_days
        if group_id is not None:
            body["group_id"] = group_id
        if validity_days is not None:
            body["validity_days"] = validity_days

        payload = await self.request(
            "POST",
            "/api/v1/admin/redeem-codes/generate",
            json_body=body,
            idempotency_key=idempotency_key,
        )
        data = payload.get("data")
        if not isinstance(data, list):
            raise Sub2APIError("sub2api response data must be a list", payload=payload)

        codes: list[Sub2APIRedeemCode] = []
        for item in data:
            if not isinstance(item, dict) or not item.get("code"):
                continue
            code_value = str(item["code"]).strip()
            if code_value == "***":
                raise Sub2APIError(
                    "sub2api returned a redacted redeem code; exact idempotency replay is unavailable",
                    payload=payload,
                )
            codes.append(
                Sub2APIRedeemCode(
                    id=int(item["id"]) if item.get("id") is not None else None,
                    code=code_value,
                    type=str(item.get("type") or code_type),
                    value=float(item.get("value") or value),
                    status=str(item.get("status") or ""),
                    expires_at=item.get("expires_at"),
                )
            )
        return codes


_sub2api_clients: dict[tuple[str, str, str, float], Sub2APIClient] = {}


def get_sub2api_client(
    base_url: str,
    admin_api_key: str,
    instance_name: str = "default",
    timeout: float = 5.0,
) -> Sub2APIClient:
    key_fingerprint = hashlib.sha256(admin_api_key.encode("utf-8")).hexdigest()
    cache_key = (instance_name, base_url.rstrip("/"), key_fingerprint, float(timeout))
    if cache_key not in _sub2api_clients:
        _sub2api_clients[cache_key] = Sub2APIClient(
            Sub2APIClientConfig(base_url=base_url, admin_api_key=admin_api_key, timeout=timeout)
        )
    return _sub2api_clients[cache_key]


async def close_all_sub2api_clients() -> None:
    for client in _sub2api_clients.values():
        await client.close()
    _sub2api_clients.clear()
