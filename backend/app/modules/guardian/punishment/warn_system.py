"""
Warn System

Manages warning messages and escalation.
"""

from typing import Optional

import structlog

from app.modules.guardian.models import ViolationLevel
from app.modules.guardian.moderation.action_executor import ActionExecutor

logger = structlog.get_logger()


class WarnSystem:
    """
    Warning system for user notifications.
    
    Sends warning messages to users when they violate rules.
    """
    
    WARN_MESSAGES = {
        ViolationLevel.LOW: """⚠️ *温馨提示*

{username}，您的消息可能含有不当内容，请注意。

请遵守群规，共同维护良好的群聊环境。""",
        
        ViolationLevel.MEDIUM: """⚠️ *警告通知*

{username}，检测到违规内容，已记录。

📋 违规类型：{violation_type}
🔢 当前警告：{warning_count}/3

请遵守群规，多次违规将被禁言。""",
        
        ViolationLevel.HIGH: """🚫 *严重警告*

{username}，您的消息含有严重违规内容！

📋 违规类型：{violation_type}
🔢 当前警告：{warning_count}/3

已被临时禁言，请联系管理员。""",
    }
    
    ESCALATION_MESSAGES = {
        "mute_warning": """⚠️ *即将禁言*

{username}，您已收到 {count} 次警告。

再违规将被禁言 {duration} 分钟，请注意！""",
        
        "final_warning": """🚨 *最后一次警告*

{username}，这是您最后一次警告机会！

违规将直接被移出群聊。""",
    }
    
    def __init__(self, action_executor: ActionExecutor):
        """
        Initialize WarnSystem.
        
        Args:
            action_executor: Action executor for sending messages
        """
        self._executor = action_executor
        self.logger = logger.bind(module="warn_system")
    
    async def send_warning(
        self,
        chat_id: int,
        user_id: int,
        username: Optional[str],
        level: ViolationLevel,
        violation_type: str,
        warning_count: int
    ) -> bool:
        """
        Send a warning message to a user.
        
        Args:
            chat_id: Chat ID
            user_id: User ID
            username: Username or display name
            level: Violation severity level
            violation_type: Type of violation
            warning_count: Current warning count
            
        Returns:
            True if sent successfully
        """
        display_name = username or f"User {user_id}"
        
        message = self.WARN_MESSAGES.get(level, self.WARN_MESSAGES[ViolationLevel.LOW]).format(
            username=display_name,
            violation_type=violation_type,
            warning_count=warning_count
        )
        
        try:
            await self._executor._client.send_message(
                chat_id,
                message,
                parse_mode="Markdown"
            )
            
            self.logger.info(
                "warning_sent",
                chat_id=chat_id,
                user_id=user_id,
                level=level.value,
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
    
    async def send_escalation_warning(
        self,
        chat_id: int,
        user_id: int,
        username: Optional[str],
        warning_count: int,
        mute_duration: int
    ) -> bool:
        """
        Send an escalation warning.
        
        Args:
            chat_id: Chat ID
            user_id: User ID
            username: Username or display name
            warning_count: Current warning count
            mute_duration: Mute duration in minutes
            
        Returns:
            True if sent successfully
        """
        display_name = username or f"User {user_id}"
        
        if warning_count >= 2:
            message = self.ESCALATION_MESSAGES["final_warning"].format(
                username=display_name
            )
        else:
            message = self.ESCALATION_MESSAGES["mute_warning"].format(
                username=display_name,
                count=warning_count,
                duration=mute_duration // 60
            )
        
        try:
            await self._executor._client.send_message(
                chat_id,
                message,
                parse_mode="Markdown"
            )
            
            return True
            
        except Exception as e:
            self.logger.error(
                "escalation_warning_failed",
                chat_id=chat_id,
                user_id=user_id,
                error=str(e)
            )
            return False
    
    async def send_mute_notification(
        self,
        chat_id: int,
        user_id: int,
        username: Optional[str],
        duration_seconds: int
    ) -> bool:
        """
        Send notification that user has been muted.
        
        Args:
            chat_id: Chat ID
            user_id: User ID
            username: Username or display name
            duration_seconds: Mute duration in seconds
            
        Returns:
            True if sent successfully
        """
        display_name = username or f"User {user_id}"
        
        if duration_seconds >= 3600:
            duration_text = f"{duration_seconds // 3600} 小时"
        elif duration_seconds >= 60:
            duration_text = f"{duration_seconds // 60} 分钟"
        else:
            duration_text = f"{duration_seconds} 秒"
        
        message = f"""🔇 *已被禁言*

{display_name}，您因违反群规已被禁言 {duration_text}。

禁言结束后请遵守群规。"""
        
        try:
            await self._executor._client.send_message(
                chat_id,
                message,
                parse_mode="Markdown"
            )
            
            return True
            
        except Exception as e:
            self.logger.error(
                "mute_notification_failed",
                chat_id=chat_id,
                user_id=user_id,
                error=str(e)
            )
            return False
    
    async def send_ban_notification(
        self,
        chat_id: int,
        user_id: int,
        username: Optional[str]
    ) -> bool:
        """
        Send notification that user has been banned.
        
        Args:
            chat_id: Chat ID
            user_id: User ID (no longer accessible if banned)
            username: Username or display name
            
        Returns:
            True if sent successfully (to admin log)
        """
        display_name = username or f"User {user_id}"
        
        message = f"""🚫 *用户已被封禁*

{display_name} (ID: {user_id})

因多次严重违规，已被永久移出群聊。"""
        
        try:
            await self._executor._client.send_message(
                chat_id,
                message,
                parse_mode="Markdown"
            )
            
            self.logger.info(
                "ban_notification_sent",
                chat_id=chat_id,
                user_id=user_id
            )
            
            return True
            
        except Exception as e:
            self.logger.error(
                "ban_notification_failed",
                chat_id=chat_id,
                user_id=user_id,
                error=str(e)
            )
            return False
    
    def format_warning_message(
        self,
        username: str,
        level: ViolationLevel,
        violation_type: str,
        warning_count: int
    ) -> str:
        """
        Format a warning message.
        
        Args:
            username: Username or display name
            level: Violation level
            violation_type: Type of violation
            warning_count: Current warning count
            
        Returns:
            Formatted message
        """
        template = self.WARN_MESSAGES.get(level, self.WARN_MESSAGES[ViolationLevel.LOW])
        return template.format(
            username=username,
            violation_type=violation_type,
            warning_count=warning_count
        )
    
    def get_remaining_warnings(self, warning_count: int) -> int:
        """
        Get remaining warnings before mute.
        
        Args:
            warning_count: Current warning count
            
        Returns:
            Number of remaining warnings
        """
        return max(0, 3 - warning_count)
