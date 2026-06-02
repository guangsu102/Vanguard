"""
Acquisition Bot API Integration

Provides integration layer between Acquisition Bot and Backend API.
"""

from typing import Any, Optional
from app.modules.integrations.api_client import BotAPIClient
import structlog

logger = structlog.get_logger()


class AcquisitionAPIClient:
    """
    API client for Acquisition Bot operations.

    This class provides high-level methods for the Acquisition Bot
    to interact with the Backend API.
    """

    def __init__(self, api_client: BotAPIClient):
        self._client = api_client

    # =============================================================================
    # Group Operations
    # =============================================================================

    async def get_target_groups(
        self,
        min_level: Optional[int] = None,
        min_members: Optional[int] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """
        Get target groups for acquisition.

        Args:
            min_level: Minimum group level
            min_members: Minimum member count
            limit: Maximum number of groups to return

        Returns:
            List of target group dictionaries
        """
        result = await self._client.get_target_groups(
            min_level=min_level,
            min_members=min_members,
            limit=limit,
        )
        return result.get("data", [])

    async def record_group_search(
        self,
        keyword: str,
        group_id: int,
        group_title: Optional[str] = None,
        member_count: Optional[int] = None,
    ) -> dict[str, Any]:
        """
        Record a group search operation.

        Args:
            keyword: Search keyword used
            group_id: Found group ID
            group_title: Group title
            member_count: Member count

        Returns:
            Created record info
        """
        payload = {
            "keyword": keyword,
            "group_id": group_id,
            "group_title": group_title,
            "member_count": member_count,
        }
        result = await self._client._request("POST", "/api/acquisition/search", json=payload)
        return result

    # =============================================================================
    # Message Operations
    # =============================================================================

    async def record_message_sent(
        self,
        account_id: int,
        group_id: int,
        content: str,
        message_type: str = "interaction",
        telegram_message_id: Optional[int] = None,
    ) -> dict[str, Any]:
        """
        Record a message sent by the bot.

        Args:
            account_id: Bot account ID
            group_id: Target group ID
            content: Message content
            message_type: Type of message
            telegram_message_id: Telegram message ID

        Returns:
            Created record info
        """
        payload = {
            "account_id": account_id,
            "group_id": group_id,
            "content": content,
            "message_type": message_type,
            "message_id": telegram_message_id,
        }
        result = await self._client._request("POST", "/api/acquisition/messages", json=payload)
        return result

    # =============================================================================
    # Keyword Operations
    # =============================================================================

    async def get_active_triggers(self) -> list[dict[str, Any]]:
        """
        Get active keyword triggers.

        Returns:
            List of active keyword triggers
        """
        result = await self._client.get_keywords(enabled=True)
        return result.get("data", [])

    async def record_trigger(
        self,
        trigger_id: int,
        user_id: int,
        group_id: int,
        matched_keyword: str,
        action_taken: str,
        reply_content: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Record a trigger event.

        Args:
            trigger_id: Trigger ID
            user_id: User who triggered
            group_id: Group where trigger occurred
            matched_keyword: Matched keyword
            action_taken: Action that was taken
            reply_content: Reply content if applicable

        Returns:
            Created record info
        """
        payload = {
            "trigger_id": trigger_id,
            "user_id": user_id,
            "group_id": group_id,
            "matched_keyword": matched_keyword,
            "action_taken": action_taken,
            "reply_content": reply_content,
        }
        result = await self._client._request("POST", "/api/acquisition/triggers", json=payload)
        return result

    # =============================================================================
    # User Tracking
    # =============================================================================

    async def track_user_registration(
        self,
        user_id: int,
        username: str,
        source_group_id: Optional[int] = None,
        source_keyword: Optional[str] = None,
        tracking_code: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Track user registration.

        Args:
            user_id: Telegram user ID
            username: Username
            source_group_id: Source group ID
            source_keyword: Keyword that triggered registration
            tracking_code: Tracking code

        Returns:
            Tracking record info
        """
        result = await self._client.register_user(
            user_id=user_id,
            username=username,
            source="telegram",
            source_group_id=source_group_id,
            tracking_code=tracking_code,
        )

        if source_keyword:
            await self._client.track_user_action(
                user_id=user_id,
                action="register",
                metadata={"keyword": source_keyword},
            )

        return result

    async def track_user_action(
        self,
        user_id: int,
        action: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """
        Track user action.

        Args:
            user_id: Telegram user ID
            action: Action type
            metadata: Additional metadata

        Returns:
            Tracking result
        """
        return await self._client.track_user_action(
            user_id=user_id,
            action=action,
            metadata=metadata,
        )

    # =============================================================================
    # Campaign Operations
    # =============================================================================

    async def get_active_campaigns(self) -> list[dict[str, Any]]:
        """
        Get active acquisition campaigns.

        Returns:
            List of active campaigns
        """
        result = await self._client.get_active_campaigns()
        return result.get("data", [])

    async def record_campaign_interaction(
        self,
        campaign_id: int,
        user_id: int,
        action: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """
        Record campaign interaction.

        Args:
            campaign_id: Campaign ID
            user_id: User ID
            action: Action type
            metadata: Additional metadata

        Returns:
            Record result
        """
        return await self._client.record_campaign_action(
            campaign_id=campaign_id,
            user_id=user_id,
            action=action,
            metadata=metadata,
        )

    # =============================================================================
    # Guide Flow Operations
    # =============================================================================

    async def update_guide_flow(
        self,
        user_id: int,
        state: str,
        step: Optional[int] = None,
        completed_steps: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """
        Update user guide flow state.

        Args:
            user_id: User ID
            state: New state
            step: Current step
            completed_steps: Completed steps list

        Returns:
            Update result
        """
        payload: dict[str, Any] = {"user_id": user_id, "state": state}
        if step is not None:
            payload["step"] = step
        if completed_steps is not None:
            payload["steps_completed"] = completed_steps

        result = await self._client._request("PUT", "/api/acquisition/guide-flow", json=payload)
        return result

    # =============================================================================
    # Statistics
    # =============================================================================

    async def record_acquisition_metric(
        self,
        metric_name: str,
        value: float,
        tags: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """
        Record acquisition metric.

        Args:
            metric_name: Name of the metric
            value: Metric value
            tags: Metric tags

        Returns:
            Record result
        """
        tags = tags or {}
        tags["module"] = "acquisition"
        return await self._client.record_metric(
            metric_name=metric_name,
            value=value,
            tags=tags,
        )
