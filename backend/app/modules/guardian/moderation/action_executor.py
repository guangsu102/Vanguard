"""
Action Executor

Executes moderation actions on Telegram groups.
"""

from dataclasses import dataclass
from typing import Optional

import structlog

from app.modules.guardian.models import ViolationAction

logger = structlog.get_logger()


@dataclass
class ActionResult:
    """Result of action execution."""
    success: bool
    action: ViolationAction
    message: str = ""
    details: Optional[dict] = None


class ActionExecutor:
    """
    Executes moderation actions in Telegram groups.
    
    Provides methods for deleting messages, muting users, banning, etc.
    """
    
    def __init__(self, telegram_client=None):
        """
        Initialize ActionExecutor.
        
        Args:
            telegram_client: Telegram client instance for executing actions
        """
        self._client = telegram_client
        self.logger = logger.bind(module="action_executor")
    
    def set_client(self, client) -> None:
        """Set Telegram client."""
        self._client = client
    
    async def execute(
        self,
        action: ViolationAction,
        chat_id: int,
        user_id: int,
        message_id: Optional[int] = None,
        duration: Optional[int] = None
    ) -> ActionResult:
        """
        Execute a moderation action.
        
        Args:
            action: Action to execute
            chat_id: Chat/Group ID
            user_id: User ID
            message_id: Optional message ID to delete
            duration: Optional duration for mute (seconds)
            
        Returns:
            ActionResult with execution status
        """
        if self._client is None:
            self.logger.warning("telegram_client_not_set")
            return ActionResult(
                success=False,
                action=action,
                message="Telegram client not initialized"
            )
        
        try:
            if message_id and action in [ViolationAction.WARN, ViolationAction.MUTE]:
                await self.delete_message(chat_id, message_id)
            
            if action == ViolationAction.DELETE or (message_id and action == ViolationAction.WARN):
                result = await self.delete_message(chat_id, message_id) if message_id else False
                return ActionResult(
                    success=result,
                    action=action,
                    message="Message deleted" if result else "Failed to delete message"
                )
            
            elif action == ViolationAction.MUTE:
                return await self.mute_user(chat_id, user_id, duration or 300)
            
            elif action == ViolationAction.BAN:
                return await self.ban_user(chat_id, user_id)
            
            elif action == ViolationAction.KICK:
                return await self.kick_user(chat_id, user_id)
            
            elif action == ViolationAction.WARN:
                return ActionResult(
                    success=True,
                    action=action,
                    message="Warning recorded"
                )
            
            else:
                return ActionResult(
                    success=False,
                    action=action,
                    message=f"Unknown action: {action}"
                )
                
        except Exception as e:
            self.logger.error(
                "action_execution_failed",
                action=action.value,
                user_id=user_id,
                chat_id=chat_id,
                error=str(e)
            )
            return ActionResult(
                success=False,
                action=action,
                message=f"Error: {str(e)}"
            )
    
    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        """
        Delete a message.
        
        Args:
            chat_id: Chat ID
            message_id: Message ID to delete
            
        Returns:
            True if successful
        """
        if self._client is None:
            return False
        
        try:
            await self._client.delete_message(chat_id, message_id)
            self.logger.info(
                "message_deleted",
                chat_id=chat_id,
                message_id=message_id
            )
            return True
        except Exception as e:
            self.logger.error(
                "delete_message_failed",
                chat_id=chat_id,
                message_id=message_id,
                error=str(e)
            )
            return False
    
    async def mute_user(
        self,
        chat_id: int,
        user_id: int,
        duration: int
    ) -> ActionResult:
        """
        Mute a user in a chat.
        
        Args:
            chat_id: Chat ID
            user_id: User ID to mute
            duration: Mute duration in seconds
            
        Returns:
            ActionResult
        """
        if self._client is None:
            return ActionResult(success=False, action=ViolationAction.MUTE, message="Client not set")
        
        try:
            until_date = duration if duration > 0 else 30 * 60
            
            await self._client.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                until_date=until_date,
                can_send_messages=False,
                can_send_media_messages=False,
                can_send_other_messages=False,
                can_add_web_page_previews=False
            )
            
            self.logger.info(
                "user_muted",
                chat_id=chat_id,
                user_id=user_id,
                duration=duration
            )
            
            return ActionResult(
                success=True,
                action=ViolationAction.MUTE,
                message=f"User muted for {duration} seconds",
                details={"duration": duration}
            )
        except Exception as e:
            self.logger.error(
                "mute_user_failed",
                chat_id=chat_id,
                user_id=user_id,
                error=str(e)
            )
            return ActionResult(
                success=False,
                action=ViolationAction.MUTE,
                message=f"Failed to mute: {str(e)}"
            )
    
    async def unmute_user(self, chat_id: int, user_id: int) -> ActionResult:
        """
        Unmute a user in a chat.
        
        Args:
            chat_id: Chat ID
            user_id: User ID to unmute
            
        Returns:
            ActionResult
        """
        if self._client is None:
            return ActionResult(success=False, action=ViolationAction.MUTE, message="Client not set")
        
        try:
            await self._client.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True
            )
            
            self.logger.info("user_unmuted", chat_id=chat_id, user_id=user_id)
            
            return ActionResult(
                success=True,
                action=ViolationAction.MUTE,
                message="User unmuted"
            )
        except Exception as e:
            self.logger.error(
                "unmute_user_failed",
                chat_id=chat_id,
                user_id=user_id,
                error=str(e)
            )
            return ActionResult(
                success=False,
                action=ViolationAction.MUTE,
                message=f"Failed to unmute: {str(e)}"
            )
    
    async def kick_user(self, chat_id: int, user_id: int) -> ActionResult:
        """
        Kick a user from a chat.
        
        Args:
            chat_id: Chat ID
            user_id: User ID to kick
            
        Returns:
            ActionResult
        """
        if self._client is None:
            return ActionResult(success=False, action=ViolationAction.KICK, message="Client not set")
        
        try:
            await self._client.ban_chat_member(chat_id, user_id)
            
            await self._client.unban_chat_member(chat_id, user_id)
            
            self.logger.info("user_kicked", chat_id=chat_id, user_id=user_id)
            
            return ActionResult(
                success=True,
                action=ViolationAction.KICK,
                message="User kicked"
            )
        except Exception as e:
            self.logger.error(
                "kick_user_failed",
                chat_id=chat_id,
                user_id=user_id,
                error=str(e)
            )
            return ActionResult(
                success=False,
                action=ViolationAction.KICK,
                message=f"Failed to kick: {str(e)}"
            )
    
    async def ban_user(self, chat_id: int, user_id: int) -> ActionResult:
        """
        Permanently ban a user from a chat.
        
        Args:
            chat_id: Chat ID
            user_id: User ID to ban
            
        Returns:
            ActionResult
        """
        if self._client is None:
            return ActionResult(success=False, action=ViolationAction.BAN, message="Client not set")
        
        try:
            await self._client.ban_chat_member(chat_id, user_id)
            
            self.logger.info("user_banned", chat_id=chat_id, user_id=user_id)
            
            return ActionResult(
                success=True,
                action=ViolationAction.BAN,
                message="User banned"
            )
        except Exception as e:
            self.logger.error(
                "ban_user_failed",
                chat_id=chat_id,
                user_id=user_id,
                error=str(e)
            )
            return ActionResult(
                success=False,
                action=ViolationAction.BAN,
                message=f"Failed to ban: {str(e)}"
            )
    
    async def unban_user(self, chat_id: int, user_id: int) -> ActionResult:
        """
        Unban a user from a chat.
        
        Args:
            chat_id: Chat ID
            user_id: User ID to unban
            
        Returns:
            ActionResult
        """
        if self._client is None:
            return ActionResult(success=False, action=ViolationAction.BAN, message="Client not set")
        
        try:
            await self._client.unban_chat_member(chat_id, user_id)
            
            self.logger.info("user_unbanned", chat_id=chat_id, user_id=user_id)
            
            return ActionResult(
                success=True,
                action=ViolationAction.BAN,
                message="User unbanned"
            )
        except Exception as e:
            self.logger.error(
                "unban_user_failed",
                chat_id=chat_id,
                user_id=user_id,
                error=str(e)
            )
            return ActionResult(
                success=False,
                action=ViolationAction.BAN,
                message=f"Failed to unban: {str(e)}"
            )
    
    async def send_warning(
        self,
        chat_id: int,
        user_id: int,
        username: Optional[str],
        violation_type: str,
        warning_count: int
    ) -> bool:
        """
        Send a warning message to a user.
        
        Args:
            chat_id: Chat ID
            user_id: User ID
            username: Username or display name
            violation_type: Type of violation
            warning_count: Current warning count
            
        Returns:
            True if successful
        """
        if self._client is None:
            return False
        
        display_name = username or f"User {user_id}"
        
        message = f"""⚠️ *警告通知*

{ display_name }，您的消息可能含有违规内容：
• 违规类型：{violation_type}
• 警告次数：{warning_count}/3

请遵守群规，共同维护良好的群聊环境。
多次违规将被禁言或踢出。"""
        
        try:
            await self._client.send_message(chat_id, message, parse_mode="Markdown")
            self.logger.info(
                "warning_sent",
                chat_id=chat_id,
                user_id=user_id,
                warning_count=warning_count
            )
            return True
        except Exception as e:
            self.logger.error(
                "warning_send_failed",
                chat_id=chat_id,
                user_id=user_id,
                error=str(e)
            )
            return False
