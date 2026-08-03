"""Async OneBot 11 client for NapCatQQ."""

from __future__ import annotations

from typing import Any

import httpx

from app.core.config import settings


class OneBotAPIError(RuntimeError):
    """A structured NapCat OneBot API failure."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retcode: int | None = None,
        uncertain: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.retcode = retcode
        self.uncertain = uncertain


class OneBotClient:
    """Call NapCatQQ through its OneBot 11 HTTP API."""

    def __init__(
        self,
        *,
        account_id: str | None = None,
        http_url: str | None = None,
        websocket_url: str | None = None,
        access_token: str | None = None,
        timeout: float | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.account_id = (account_id or settings.QQ_ONEBOT_ACCOUNT_ID or "").strip()
        self.http_url = (http_url or settings.QQ_ONEBOT_HTTP_URL).rstrip("/")
        self.websocket_url = websocket_url or settings.QQ_ONEBOT_WS_URL
        self.access_token = access_token or settings.QQ_ONEBOT_ACCESS_TOKEN or ""
        self.timeout = timeout or settings.QQ_ONEBOT_REQUEST_TIMEOUT_SECONDS
        self._client = http_client or httpx.AsyncClient(timeout=self.timeout)
        self._owns_client = http_client is None

    @property
    def configured(self) -> bool:
        return bool(self.account_id and self.http_url and self.access_token)

    @property
    def websocket_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_login_info(self) -> dict[str, Any]:
        data = await self._call("get_login_info")
        return self._expect_object(data, "get_login_info")

    async def get_group_list(self) -> list[dict[str, Any]]:
        data = await self._call("get_group_list", {"no_cache": True})
        if not isinstance(data, list):
            raise OneBotAPIError("NapCat get_group_list returned invalid data")
        return [item for item in data if isinstance(item, dict)]

    async def send_group_message(self, group_id: str, content: str) -> dict[str, Any]:
        data = await self._call(
            "send_group_msg",
            {
                "group_id": self._numeric_id(group_id, "QQ group number"),
                "message": content,
                "auto_escape": True,
            },
            write_operation=True,
        )
        return self._expect_object(data, "send_group_msg")

    async def recall_group_message(self, group_id: str, message_id: str) -> None:
        # group_id is validated to prevent commands from being routed to stale
        # official-OpenAPI records. OneBot delete_msg only needs message_id.
        self._numeric_id(group_id, "QQ group number")
        await self._call(
            "delete_msg",
            {"message_id": self._integer_id(message_id, "OneBot message ID")},
            write_operation=True,
        )

    async def _call(
        self,
        action: str,
        payload: dict[str, Any] | None = None,
        *,
        write_operation: bool = False,
    ) -> Any:
        if not self.configured:
            raise OneBotAPIError(
                "NapCat OneBot account, HTTP URL, and access token are not configured"
            )
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        try:
            response = await self._client.post(
                f"{self.http_url}/{action}",
                headers=headers,
                json=payload or {},
            )
        except httpx.TimeoutException as exc:
            raise OneBotAPIError(
                f"NapCat OneBot action {action} timed out",
                uncertain=write_operation,
            ) from exc
        except httpx.HTTPError as exc:
            raise OneBotAPIError(
                f"NapCat OneBot action {action} failed: {exc}",
                uncertain=write_operation,
            ) from exc

        body = self._safe_json(response)
        if response.status_code >= 400:
            raise OneBotAPIError(
                self._error_message(body, response.text, action),
                status_code=response.status_code,
                uncertain=write_operation and response.status_code >= 500,
            )
        if not isinstance(body, dict):
            raise OneBotAPIError(f"NapCat OneBot action {action} returned invalid JSON")

        retcode = self._retcode(body.get("retcode"))
        if body.get("status") != "ok" or retcode != 0:
            raise OneBotAPIError(
                self._error_message(body, response.text, action),
                status_code=response.status_code,
                retcode=retcode,
            )
        return body.get("data")

    @staticmethod
    def _expect_object(data: Any, action: str) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise OneBotAPIError(f"NapCat OneBot action {action} returned invalid data")
        return data

    @staticmethod
    def _numeric_id(value: str | int, label: str) -> int:
        raw = str(value).strip()
        if not raw.isdigit() or int(raw) <= 0:
            raise OneBotAPIError(f"{label} must be a positive numeric ID")
        return int(raw)

    @staticmethod
    def _integer_id(value: str | int, label: str) -> int:
        try:
            parsed = int(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise OneBotAPIError(f"{label} must be an integer") from exc
        if parsed == 0:
            raise OneBotAPIError(f"{label} must not be zero")
        return parsed

    @staticmethod
    def _safe_json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return None

    @staticmethod
    def _retcode(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _error_message(body: Any, fallback: str, action: str) -> str:
        if isinstance(body, dict):
            message = body.get("message") or body.get("wording")
            if message:
                return str(message)
        return fallback or f"NapCat OneBot action {action} failed"
