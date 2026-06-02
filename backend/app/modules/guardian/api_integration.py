"""
Guardian Bot API Integration

Provides integration layer between Guardian Bot and Backend API.
"""

from typing import Any, Optional
from app.modules.integrations.api_client import BotAPIClient
import structlog

logger = structlog.get_logger()


class GuardianAPIClient:
    """
    API client for Guardian Bot operations.

    This class provides high-level methods for the Guardian Bot
    to interact with the Backend API.
    """

    def __init__(self, api_client: BotAPIClient):
        self._client = api_client

    # =============================================================================
    # Rule Operations
    # =============================================================================

    async def get_moderation_rules(
        self,
        group_id: Optional[int] = None,
        rule_type: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """
        Get moderation rules.

        Args:
            group_id: Filter by group ID (None for global rules)
            rule_type: Filter by rule type

        Returns:
            List of moderation rules
        """
        result = await self._client.get_rules(
            group_id=group_id,
            rule_type=rule_type,
            enabled=True,
        )
        return result.get("data", [])

    async def get_global_rules(self) -> list[dict[str, Any]]:
        """Get global moderation rules (not specific to any group)."""
        return await self.get_moderation_rules(group_id=0)

    async def get_group_rules(self, group_id: int) -> list[dict[str, Any]]:
        """Get rules specific to a group."""
        return await self.get_moderation_rules(group_id=group_id)

    # =============================================================================
    # Whitelist Operations
    # =============================================================================

    async def is_whitelisted(
        self,
        user_id: int,
        whitelist_type: str = "user",
    ) -> bool:
        """
        Check if user is whitelisted.

        Args:
            user_id: User ID to check
            whitelist_type: Type of whitelist (user/domain/path)

        Returns:
            True if whitelisted, False otherwise
        """
        result = await self._client.check_whitelist(
            user_id=user_id,
            whitelist_type=whitelist_type,
        )
        return result.get("data", {}).get("is_whitelisted", False)

    async def add_to_whitelist(
        self,
        whitelist_type: str,
        value: str,
        group_id: Optional[int] = None,
        expires_at: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Add entry to whitelist.

        Args:
            whitelist_type: Type (user/domain/path)
            value: Value to whitelist
            group_id: Group ID if group-specific
            expires_at: Expiration datetime ISO format

        Returns:
            Created whitelist entry
        """
        payload = {
            "whitelist_type": whitelist_type,
            "value": value,
            "group_id": group_id,
        }
        if expires_at:
            payload["expires_at"] = expires_at

        result = await self._client._request("POST", "/api/rules/whitelist", json=payload)
        return result

    # =============================================================================
    # Violation Operations
    # =============================================================================

    async def record_violation(
        self,
        user_id: int,
        group_id: int,
        rule_type: str,
        action: str,
        content: Optional[str] = None,
        duration: Optional[int] = None,
    ) -> dict[str, Any]:
        """
        Record a violation.

        Args:
            user_id: Violating user ID
            group_id: Group ID where violation occurred
            rule_type: Type of rule violated
            action: Action taken (warn/mute/ban/kick)
            content: Content that triggered violation
            duration: Duration in seconds for temporary actions

        Returns:
            Created violation record
        """
        return await self._client.record_punishment(
            user_id=user_id,
            group_id=group_id,
            rule_type=rule_type,
            action=action,
            content=content,
            duration=duration,
        )

    async def get_user_violation_count(
        self,
        user_id: int,
        group_id: Optional[int] = None,
    ) -> int:
        """
        Get user's violation count.

        Args:
            user_id: User ID
            group_id: Optional group ID to filter by

        Returns:
            Number of violations
        """
        result = await self._client.get_user_violations(user_id=user_id, group_id=group_id)
        data = result.get("data", {})
        return len(data.get("violations", []))

    # =============================================================================
    # Verification Operations
    # =============================================================================

    async def create_verification_session(
        self,
        user_id: int,
        chat_id: int,
        verify_type: str = "captcha",
    ) -> dict[str, Any]:
        """
        Create a verification session for new member.

        Args:
            user_id: User ID
            chat_id: Group chat ID
            verify_type: Type of verification (captcha/question)

        Returns:
            Created verification session
        """
        return await self._client.create_verification(
            user_id=user_id,
            chat_id=chat_id,
            verify_type=verify_type,
        )

    async def verify_captcha(
        self,
        session_id: str,
        captcha_code: str,
    ) -> dict[str, Any]:
        """
        Verify captcha answer.

        Args:
            session_id: Verification session ID
            captcha_code: User's captcha answer

        Returns:
            Verification result
        """
        return await self._client.verify_captcha(
            session_id=session_id,
            captcha_code=captcha_code,
        )

    async def verify_answer(
        self,
        session_id: str,
        answer: str,
    ) -> dict[str, Any]:
        """
        Verify question answer.

        Args:
            session_id: Verification session ID
            answer: User's answer

        Returns:
            Verification result
        """
        return await self._client.verify_answer(
            session_id=session_id,
            answer=answer,
        )

    async def get_group_verification_config(
        self,
        group_id: int,
    ) -> Optional[dict[str, Any]]:
        """
        Get group verification configuration.

        Args:
            group_id: Group ID

        Returns:
            Verification config or None
        """
        result = await self._client._request("GET", f"/api/groups/{group_id}/verification")
        return result.get("data")

    # =============================================================================
    # Broadcast Operations
    # =============================================================================

    async def send_broadcast(
        self,
        content: str,
        target_groups: list[int],
        broadcast_type: str = "node_update",
    ) -> dict[str, Any]:
        """
        Send broadcast message to multiple groups.

        Args:
            content: Broadcast message content
            target_groups: List of group IDs to broadcast to
            broadcast_type: Type of broadcast

        Returns:
            Broadcast result
        """
        return await self._client.send_broadcast(
            content=content,
            target_groups=target_groups,
            broadcast_type=broadcast_type,
        )

    # =============================================================================
    # Coupon/Reward Operations
    # =============================================================================

    async def distribute_coupon(
        self,
        user_id: int,
        coupon_type: str,
        trial_hours: Optional[int] = None,
        traffic_gb: Optional[int] = None,
        coupon_code: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Distribute coupon to user.

        Args:
            user_id: User ID
            coupon_type: Type (trial/discount/gift)
            trial_hours: Trial hours if trial type
            traffic_gb: Traffic GB if traffic type
            coupon_code: Coupon code if applicable

        Returns:
            Distribution result
        """
        payload = {
            "user_id": user_id,
            "distribution_type": coupon_type,
        }
        if trial_hours:
            payload["trial_hours"] = trial_hours
        if traffic_gb:
            payload["traffic_gb"] = traffic_gb
        if coupon_code:
            payload["coupon_code"] = coupon_code

        result = await self._client._request("POST", "/api/coupons/distribute", json=payload)
        return result

    # =============================================================================
    # Statistics
    # =============================================================================

    async def record_violation_metric(
        self,
        group_id: int,
        rule_type: str,
        action: str,
    ) -> dict[str, Any]:
        """
        Record violation metric.

        Args:
            group_id: Group ID
            rule_type: Type of rule violated
            action: Action taken

        Returns:
            Record result
        """
        return await self._client.record_metric(
            metric_name="violation",
            value=1,
            tags={
                "module": "guardian",
                "group_id": str(group_id),
                "rule_type": rule_type,
                "action": action,
            },
        )

    async def record_verification_metric(
        self,
        result: str,
        group_id: int,
    ) -> dict[str, Any]:
        """
        Record verification metric.

        Args:
            result: Verification result (passed/failed/expired)
            group_id: Group ID

        Returns:
            Record result
        """
        return await self._client.record_metric(
            metric_name="verification",
            value=1,
            tags={
                "module": "guardian",
                "result": result,
                "group_id": str(group_id),
            },
        )

    # =============================================================================
    # Account Operations
    # =============================================================================

    async def get_account_health_stats(self) -> dict[str, Any]:
        """
        Get account health statistics.

        Returns:
            Health stats dictionary
        """
        return await self._client.get_account_health()

    async def update_account_status(
        self,
        account_id: int,
        status: str,
    ) -> dict[str, Any]:
        """
        Update account status.

        Args:
            account_id: Account ID
            status: New status

        Returns:
            Update result
        """
        return await self._client.update_account_status(account_id, status)
