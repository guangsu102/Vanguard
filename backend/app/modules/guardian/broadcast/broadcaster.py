"""
Guardian Broadcaster

Broadcasts messages to Telegram groups.
"""

import asyncio
from dataclasses import dataclass
from typing import Optional

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.system_identity import bot_risk_identity
from app.modules.guardian.broadcast.templates import BroadcastTemplate, NodeStatus, PromoData

logger = structlog.get_logger()


@dataclass
class BroadcastResult:
    """Result of broadcast operation."""
    success: int
    failed: int
    failed_groups: list[int]


@dataclass
class PinnedMessageResult:
    """Result of sending and pinning a single message."""
    success: bool
    message_id: Optional[int] = None
    error: Optional[str] = None


class GuardianBroadcaster:
    """
    Broadcasts messages to Telegram groups.
    
    Handles sending notifications, promotions, and alerts to configured groups.
    """
    
    def __init__(
        self,
        db: AsyncSession,
        telegram_client=None
    ):
        """
        Initialize GuardianBroadcaster.
        
        Args:
            db: Database session
            telegram_client: Telegram client for sending messages
        """
        self.db = db
        self._client = telegram_client
        self._template = BroadcastTemplate()
        self._lock = asyncio.Lock()
        self.logger = logger.bind(module="guardian_broadcaster")
    
    def set_client(self, client) -> None:
        """Set Telegram client."""
        self._client = client
        self._risk_guard = getattr(client, "risk_guard", None)
        self._risk_account = getattr(client, "risk_account", bot_risk_identity("guardian_broadcast"))
    
    async def broadcast_node_status(
        self,
        group_ids: list[int],
        node_status: NodeStatus
    ) -> BroadcastResult:
        """
        Broadcast node status to groups.
        
        Args:
            group_ids: List of group IDs
            node_status: Node status data
            
        Returns:
            BroadcastResult
        """
        if node_status.status == "online":
            message = self._template.get_node_online(node_status.node_name)
        else:
            message = self._template.get_node_offline(
                node_name=node_status.node_name,
                reason=node_status.reason or "维护中",
                eta=node_status.eta
            )
        
        return await self._broadcast(group_ids, message)
    
    async def broadcast_promo(
        self,
        group_ids: list[int],
        promo: PromoData
    ) -> BroadcastResult:
        """
        Broadcast promotion to groups.
        
        Args:
            group_ids: List of group IDs
            promo: Promotion data
            
        Returns:
            BroadcastResult
        """
        message = self._template.get_promo_trial(
            campaign_name=promo.campaign_name,
            description=promo.description,
            validity=promo.validity,
            claim_url=promo.claim_url,
            bonus=promo.bonus
        )
        
        return await self._broadcast(group_ids, message)
    
    async def broadcast_welcome(
        self,
        chat_id: int,
        user_id: int,
        username: str,
        needs_verification: bool = True
    ) -> bool:
        """
        Send welcome message to a user.
        
        Args:
            chat_id: Group chat ID
            user_id: User ID
            username: Username
            needs_verification: Whether verification is required
            
        Returns:
            True if sent successfully
        """
        message = self._template.get_welcome(username, needs_verification)
        
        return await self._send_to_chat(chat_id, message)
    
    async def broadcast_system_alert(
        self,
        group_ids: list[int],
        alert_type: str,
        message: str
    ) -> BroadcastResult:
        """
        Broadcast system alert to groups.
        
        Args:
            group_ids: List of group IDs
            alert_type: Type of alert
            message: Alert message
            
        Returns:
            BroadcastResult
        """
        full_message = self._template.get_system_alert(message)
        return await self._broadcast(group_ids, full_message)
    
    async def broadcast_custom(
        self,
        group_ids: list[int],
        message: str,
        parse_mode: str = "Markdown",
        reply_markup: Optional[dict] = None,
    ) -> BroadcastResult:
        """
        Broadcast custom message to groups.
        
        Args:
            group_ids: List of group IDs
            message: Message to send
            parse_mode: Message parse mode
            
        Returns:
            BroadcastResult
        """
        return await self._broadcast(group_ids, message, parse_mode, reply_markup=reply_markup)

    async def send_pinned_message(
        self,
        chat_id: int,
        message: str,
        parse_mode: str = "Markdown",
        disable_web_page_preview: bool = False,
        disable_notification: bool = True,
    ) -> PinnedMessageResult:
        """
        Send a message to a managed group and pin it.

        The guardian bot must be an admin in the target group with permission
        to pin messages.
        """
        if not self._client:
            return PinnedMessageResult(success=False, error="telegram_client_not_configured")

        try:
            sent_message = await self._client.send_message(
                chat_id,
                message,
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview,
            )
            await self._client.pin_chat_message(
                chat_id,
                sent_message.message_id,
                disable_notification=disable_notification,
            )
            return PinnedMessageResult(success=True, message_id=sent_message.message_id)
        except Exception as e:
            self.logger.error(
                "send_pinned_message_failed",
                chat_id=chat_id,
                error=str(e),
            )
            return PinnedMessageResult(success=False, error=str(e))

    async def broadcast_daily_stats(
        self,
        group_ids: list[int],
        new_users: int,
        messages: int,
        online_users: int
    ) -> BroadcastResult:
        """
        Broadcast daily statistics.
        
        Args:
            group_ids: List of group IDs
            new_users: Number of new users
            messages: Number of messages
            online_users: Number of online users
            
        Returns:
            BroadcastResult
        """
        message = self._template.get_daily_stats(new_users, messages, online_users)
        return await self._broadcast(group_ids, message)
    
    async def _broadcast(
        self,
        group_ids: list[int],
        message: str,
        parse_mode: str = "Markdown",
        reply_markup: Optional[dict] = None,
    ) -> BroadcastResult:
        """
        Internal broadcast method.
        
        Args:
            group_ids: List of group IDs
            message: Message to send
            parse_mode: Message parse mode
            
        Returns:
            BroadcastResult
        """
        if not self._client:
            self.logger.warning("telegram_client_not_set")
            return BroadcastResult(success=0, failed=len(group_ids), failed_groups=group_ids)
        
        if not message:
            self.logger.warning("empty_message")
            return BroadcastResult(success=0, failed=len(group_ids), failed_groups=group_ids)
        
        success = 0
        failed_groups = []
        
        for chat_id in group_ids:
            try:
                await self._client.send_message(chat_id, message, parse_mode=parse_mode, reply_markup=reply_markup)
                success += 1
                
                await asyncio.sleep(0.1)
                
            except Exception as e:
                self.logger.error(
                    "broadcast_failed",
                    chat_id=chat_id,
                    error=str(e)
                )
                failed_groups.append(chat_id)
        
        self.logger.info(
            "broadcast_completed",
            success=success,
            failed=len(failed_groups)
        )
        
        return BroadcastResult(
            success=success,
            failed=len(failed_groups),
            failed_groups=failed_groups
        )
    
    async def _send_to_chat(
        self,
        chat_id: int,
        message: str,
        parse_mode: str = "Markdown"
    ) -> bool:
        """
        Send message to a single chat.
        
        Args:
            chat_id: Chat ID
            message: Message to send
            parse_mode: Message parse mode
            
        Returns:
            True if sent successfully
        """
        if not self._client:
            return False
        
        try:
            await self._client.send_message(chat_id, message, parse_mode=parse_mode)
            return True
        except Exception as e:
            self.logger.error(
                "send_to_chat_failed",
                chat_id=chat_id,
                error=str(e)
            )
            return False
    
    async def get_active_groups(self) -> list[int]:
        """
        Get list of active group IDs.
        
        Returns:
            List of group IDs
        """
        from app.core.group.models import Group
        
        result = await self.db.execute(
            select(Group.group_id).where(Group.level != "unrated")
        )
        return [row[0] for row in result.fetchall()]
