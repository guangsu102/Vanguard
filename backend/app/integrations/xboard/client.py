"""
XBoard API client for Vanguard integration.

Implements signed requests according to the Vanguard <-> XBoard integration spec.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Optional
from urllib.parse import urlencode, urlsplit

import httpx
import structlog

logger = structlog.get_logger()


@dataclass(slots=True)
class XBoardClientConfig:
    base_url: str
    app_id: str
    signing_secret: str
    timeout: float = 5.0
    timestamp_tolerance: int = 300


class XBoardAPIError(RuntimeError):
    """Raised when XBoard returns an error response."""

    def __init__(self, code: int, message: str, trace_id: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.trace_id = trace_id


class XBoardClient:
    def __init__(self, config: XBoardClientConfig):
        self.config = config
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(base_url=self.config.base_url.rstrip("/"), timeout=self.config.timeout)
        return self._client

    @staticmethod
    def _json_dumps(payload: Any) -> str:
        if payload is None:
            return ""
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def _build_signing_string(self, method: str, path: str, query_string: str, timestamp: str, request_id: str, raw_body: str) -> str:
        return "\n".join([method.upper(), path, query_string, timestamp, request_id, raw_body])

    def _sign(self, signing_string: str) -> str:
        digest = hmac.new(self.config.signing_secret.encode("utf-8"), signing_string.encode("utf-8"), hashlib.sha256).hexdigest()
        return digest

    def _build_headers(self, method: str, url: str, body: Any = None, request_id: Optional[str] = None, timestamp: Optional[int] = None) -> dict[str, str]:
        parts = urlsplit(url)
        path = parts.path
        query_string = parts.query
        raw_body = self._json_dumps(body) if method.upper() != "GET" else ""
        ts = str(timestamp or int(time.time() * 1000))
        rid = request_id or f"req_{uuid.uuid4().hex}"
        signing_string = self._build_signing_string(method, path, query_string, ts, rid, raw_body)
        return {
            "Content-Type": "application/json",
            "X-App-Id": self.config.app_id,
            "X-Timestamp": ts,
            "X-Request-Id": rid,
            "X-Signature": self._sign(signing_string),
        }

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def request(self, method: str, path: str, *, params: Optional[Mapping[str, Any]] = None, json_body: Any = None, request_id: Optional[str] = None, timestamp: Optional[int] = None) -> dict[str, Any]:
        client = await self._get_client()
        query_string = urlencode([(k, str(v)) for k, v in (params or {}).items() if v is not None])
        url = path if not query_string else f"{path}?{query_string}"
        headers = self._build_headers(method, url, json_body, request_id=request_id, timestamp=timestamp)
        content = self._json_dumps(json_body) if method.upper() != "GET" else None
        response = await client.request(method.upper(), url, headers=headers, content=content)
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") not in (0, None):
            raise XBoardAPIError(payload.get("code", -1), payload.get("message", "xboard error"), payload.get("trace_id"))
        return payload

    async def ingest_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.request("POST", "/api/v1/events/ingest", json_body=payload)

    async def get_user_status(self, *, tg_user_id: Optional[int] = None, tracking_code: Optional[str] = None, external_user_id: Optional[str] = None, trace_id: Optional[str] = None) -> dict[str, Any]:
        params: dict[str, Any] = {"tg_user_id": tg_user_id, "tracking_code": tracking_code, "external_user_id": external_user_id, "trace_id": trace_id}
        return await self.request("GET", "/api/v1/users/status", params=params)

    async def report_coupon(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.request("POST", "/api/v1/coupons/report", json_body=payload)


_xboard_clients: dict[str, XBoardClient] = {}


def get_xboard_client(base_url: str, app_id: str, signing_secret: str, instance_name: str = "default", timeout: float = 5.0) -> XBoardClient:
    if instance_name not in _xboard_clients:
        _xboard_clients[instance_name] = XBoardClient(XBoardClientConfig(base_url=base_url, app_id=app_id, signing_secret=signing_secret, timeout=timeout))
    return _xboard_clients[instance_name]


async def close_all_xboard_clients() -> None:
    for client in _xboard_clients.values():
        await client.close()
    _xboard_clients.clear()
