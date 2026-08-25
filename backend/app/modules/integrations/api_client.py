"""
Bot API Client

Provides HTTP client for Bot modules to communicate with the Backend API.
"""

import asyncio
from typing import Any, Optional
import httpx
import structlog

logger = structlog.get_logger()


class BotAPIClient:
    """
    HTTP client for Bot modules to interact with the Backend API.

    This client provides methods for:
    - Account operations
    - Group operations
    - User tracking
    - Message sending
    - Rule fetching
    - Punishment recording
    - Verification processing
    - Broadcast sending
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 30.0,
        max_retries: int = 3,
    ):
        """
        Initialize the Bot API Client.

        Args:
            base_url: Base URL of the Backend API
            api_key: API key for authentication
            timeout: Request timeout in seconds
            max_retries: Maximum number of retry attempts
        """
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def headers(self) -> dict[str, str]:
        """Get default headers for API requests."""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-Bot-Request": "true",
        }

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers=self.headers,
                timeout=self.timeout,
            )
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs,
    ) -> dict[str, Any]:
        """
        Make an HTTP request with retry logic.

        Args:
            method: HTTP method
            path: API path
            **kwargs: Additional request arguments

        Returns:
            Response data as dictionary

        Raises:
            httpx.HTTPStatusError: On HTTP error status
        """
        client = await self._get_client()

        last_error = None
        for attempt in range(self.max_retries):
            try:
                response = await client.request(method, path, **kwargs)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                last_error = e
                logger.warning(
                    "api_request_failed",
                    attempt=attempt + 1,
                    max_retries=self.max_retries,
                    status=e.response.status_code,
                    path=path,
                )
                if e.response.status_code < 500:
                    raise
            except httpx.RequestError as e:
                last_error = e
                logger.warning(
                    "api_request_error",
                    attempt=attempt + 1,
                    max_retries=self.max_retries,
                    error=str(e),
                    path=path,
                )

            if attempt < self.max_retries - 1:
                await asyncio.sleep(2 ** attempt)

        raise last_error or Exception("Request failed")

    # =============================================================================
    # Account Operations
    # =============================================================================

    async def get_account(self, account_id: int) -> dict[str, Any]:
        """Get account details by ID."""
        return await self._request("GET", f"/api/accounts/{account_id}")

    async def list_accounts(
        self,
        status_filter: Optional[str] = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """List accounts with optional status filter."""
        params = {"limit": limit}
        if status_filter:
            params["status_filter"] = status_filter
        return await self._request("GET", "/api/accounts", params=params)

    async def update_account_status(
        self,
        account_id: int,
        status: str,
    ) -> dict[str, Any]:
        """Update account status."""
        return await self._request(
            "PUT",
            f"/api/accounts/{account_id}",
            json={"status": status},
        )

    async def get_account_health(self) -> dict[str, Any]:
        """Get account health statistics."""
        return await self._request("GET", "/api/accounts/stats/health")

    # =============================================================================
    # Group Operations
    # =============================================================================

    async def get_target_groups(
        self,
        min_level: Optional[int] = None,
        min_members: Optional[int] = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Get target groups for acquisition."""
        params = {"limit": limit}
        if min_level is not None:
            params["min_level"] = min_level
        if min_members is not None:
            params["min_members"] = min_members
        return await self._request("GET", "/api/groups", params=params)

    async def get_group(self, group_id: int) -> dict[str, Any]:
        """Get group details by ID."""
        return await self._request("GET", f"/api/groups/{group_id}")

    async def update_group_metrics(
        self,
        group_id: int,
        metrics: dict[str, Any],
    ) -> dict[str, Any]:
        """Update group metrics."""
        return await self._request(
            "PUT",
            f"/api/groups/{group_id}/metrics",
            json=metrics,
        )

    # =============================================================================
    # User Tracking
    # =============================================================================

    async def track_user_action(
        self,
        user_id: int,
        action: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Track user action for analytics."""
        payload = {
            "user_id": user_id,
            "action": action,
            "metadata": metadata or {},
        }
        return await self._request("POST", "/api/users/track", json=payload)

    async def register_user(
        self,
        user_id: int,
        username: str,
        source: str,
        source_group_id: Optional[int] = None,
        tracking_code: Optional[str] = None,
    ) -> dict[str, Any]:
        """Register a new user."""
        payload = {
            "user_id": user_id,
            "username": username,
            "source": source,
        }
        if source_group_id:
            payload["source_group_id"] = source_group_id
        if tracking_code:
            payload["tracking_code"] = tracking_code
        return await self._request("POST", "/api/users", json=payload)

    # =============================================================================
    # Keyword Operations
    # =============================================================================

    async def get_keywords(self, enabled: bool = True) -> dict[str, Any]:
        """Get enabled keywords."""
        return await self._request("GET", "/api/keywords", params={"enabled": enabled})

    async def record_keyword_trigger(
        self,
        keyword_id: int,
        user_id: int,
        group_id: int,
        matched_text: str,
    ) -> dict[str, Any]:
        """Record a keyword trigger event."""
        payload = {
            "keyword_id": keyword_id,
            "user_id": user_id,
            "group_id": group_id,
            "matched_text": matched_text,
        }
        return await self._request("POST", "/api/keywords/trigger", json=payload)

    # =============================================================================
    # Message Operations
    # =============================================================================

    async def send_message(
        self,
        account_id: int,
        chat_id: int,
        content: str,
        message_type: str = "text",
    ) -> dict[str, Any]:
        """Record a sent message."""
        payload = {
            "account_id": account_id,
            "chat_id": chat_id,
            "content": content,
            "message_type": message_type,
        }
        return await self._request("POST", "/api/messages", json=payload)

    # =============================================================================
    # Rule Operations (for Guardian Bot)
    # =============================================================================

    async def get_rules(
        self,
        group_id: Optional[int] = None,
        rule_type: Optional[str] = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        """Get moderation rules."""
        params: dict[str, Any] = {"enabled": enabled}
        if group_id is not None:
            params["group_id"] = group_id
        if rule_type:
            params["rule_type"] = rule_type
        return await self._request("GET", "/api/rules", params=params)

    async def check_whitelist(
        self,
        user_id: int,
        whitelist_type: str = "user",
    ) -> dict[str, Any]:
        """Check if user is whitelisted."""
        params = {"value": str(user_id), "whitelist_type": whitelist_type}
        return await self._request("GET", "/api/rules/whitelist/check", params=params)

    # =============================================================================
    # Punishment Operations (for Guardian Bot)
    # =============================================================================

    async def record_punishment(
        self,
        user_id: int,
        group_id: int,
        rule_type: str,
        action: str,
        content: Optional[str] = None,
        duration: Optional[int] = None,
    ) -> dict[str, Any]:
        """Record a punishment action."""
        payload = {
            "user_id": user_id,
            "group_id": group_id,
            "rule_type": rule_type,
            "action": action,
        }
        if content:
            payload["content"] = content
        if duration:
            payload["duration"] = duration
        return await self._request("POST", "/api/punishments", json=payload)

    async def get_user_violations(
        self,
        user_id: int,
        group_id: Optional[int] = None,
    ) -> dict[str, Any]:
        """Get user's violation history."""
        params = {"user_id": user_id}
        if group_id is not None:
            params["group_id"] = group_id
        return await self._request("GET", "/api/punishments/history", params=params)

    # =============================================================================
    # Verification Operations (for Guardian Bot)
    # =============================================================================

    async def create_verification(
        self,
        user_id: int,
        chat_id: int,
        verify_type: str = "captcha",
    ) -> dict[str, Any]:
        """Create a verification session."""
        payload = {
            "user_id": user_id,
            "chat_id": chat_id,
            "verify_type": verify_type,
        }
        return await self._request("POST", "/api/verifications", json=payload)

    async def verify_captcha(
        self,
        session_id: str,
        captcha_code: str,
    ) -> dict[str, Any]:
        """Verify captcha answer."""
        payload = {
            "session_id": session_id,
            "captcha_code": captcha_code,
        }
        return await self._request("POST", "/api/verifications/verify", json=payload)

    async def verify_answer(
        self,
        session_id: str,
        answer: str,
    ) -> dict[str, Any]:
        """Verify question answer."""
        payload = {
            "session_id": session_id,
            "answer": answer,
        }
        return await self._request("POST", "/api/verifications/verify", json=payload)

    # =============================================================================
    # Broadcast Operations (for Guardian Bot)
    # =============================================================================

    async def send_broadcast(
        self,
        content: str,
        target_groups: list[int],
        broadcast_type: str = "node_update",
    ) -> dict[str, Any]:
        """Send broadcast message to multiple groups."""
        payload = {
            "content": content,
            "target_groups": target_groups,
            "broadcast_type": broadcast_type,
        }
        return await self._request("POST", "/api/broadcasts", json=payload)

    # =============================================================================
    # Campaign Operations
    # =============================================================================

    async def get_active_campaigns(self) -> dict[str, Any]:
        """Get active campaigns."""
        return await self._request("GET", "/api/campaigns", params={"enabled": True})

    async def record_campaign_action(
        self,
        campaign_id: int,
        user_id: int,
        action: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Record campaign participation."""
        payload = {
            "campaign_id": campaign_id,
            "user_id": user_id,
            "action": action,
            "metadata": metadata or {},
        }
        return await self._request("POST", "/api/campaigns/track", json=payload)

    async def distribute_reward(
        self,
        user_id: int,
        campaign_id: int,
        reward_type: str,
        reward_value: Any,
    ) -> dict[str, Any]:
        """Distribute reward to user."""
        payload = {
            "user_id": user_id,
            "campaign_id": campaign_id,
            "reward_type": reward_type,
            "reward_value": reward_value,
        }
        return await self._request("POST", "/api/campaigns/reward", json=payload)

    # =============================================================================
    # Statistics
    # =============================================================================

    async def get_dashboard_stats(self) -> dict[str, Any]:
        """Get dashboard statistics."""
        return await self._request("GET", "/api/stats/dashboard")

    async def record_metric(
        self,
        metric_name: str,
        value: float,
        tags: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """Record a custom metric."""
        payload = {
            "metric_name": metric_name,
            "value": value,
            "tags": tags or {},
        }
        return await self._request("POST", "/api/stats/metric", json=payload)


# Singleton instance factory
_client_instances: dict[str, BotAPIClient] = {}


def get_bot_api_client(
    base_url: str,
    api_key: str,
    instance_name: str = "default",
) -> BotAPIClient:
    """
    Get or create a Bot API Client instance.

    Args:
        base_url: Base URL of the Backend API
        api_key: API key for authentication
        instance_name: Name for the client instance

    Returns:
        BotAPIClient instance
    """
    if instance_name not in _client_instances:
        _client_instances[instance_name] = BotAPIClient(base_url, api_key)
    return _client_instances[instance_name]


async def close_all_clients() -> None:
    """Close all client instances."""
    for client in _client_instances.values():
        await client.close()
    _client_instances.clear()
