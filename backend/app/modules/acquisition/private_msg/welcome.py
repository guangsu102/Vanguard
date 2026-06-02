"""
Welcome Message Generator Module

Generates welcome messages for new users.
"""

from typing import Optional

import structlog

from app.modules.acquisition.constants import DEFAULT_WELCOME_TEMPLATE
from app.modules.acquisition.tracking.url_builder import URLBuilder

logger = structlog.get_logger()


class WelcomeGenerator:
    """
    Generates personalized welcome messages.

    Creates welcome messages with user-specific content
    and tracking links.
    """

    def __init__(self, url_builder: Optional[URLBuilder] = None):
        """Initialize WelcomeGenerator."""
        self.url_builder = url_builder or URLBuilder()
        self.logger = logger.bind(module="welcome_generator")

    async def generate_welcome(
        self,
        user_id: int,
        source_info: Optional[dict] = None,
    ) -> str:
        """
        Generate welcome message.

        Args:
            user_id: User ID
            source_info: Optional source information

        Returns:
            Welcome message text
        """
        # 构建追踪链接
        tracking_link = await self._build_tracking_link(user_id, source_info)

        # 渲染模板
        message = DEFAULT_WELCOME_TEMPLATE.format(
            user_name=source_info.get("user_name") if source_info else "朋友",
            register_link=tracking_link,
        )

        return message.strip()

    async def generate_intro_message(
        self,
        user_id: int,
    ) -> str:
        """
        Generate product introduction message.

        Args:
            user_id: User ID

        Returns:
            Introduction message text
        """
        tracking_link = await self._build_tracking_link(user_id)

        message = f"""
XBoard 是一款专业的网络加速服务：

• 全球节点覆盖，高速稳定
• 支持多设备同时使用
• 简单易用，一键连接
• 7x24 客服支持

新用户注册即享免费试用，点击链接体验：
{tracking_link}
"""
        return message.strip()

    async def generate_promo_message(
        self,
        user_id: int,
        promo_info: Optional[dict] = None,
    ) -> str:
        """
        Generate promotional message.

        Args:
            user_id: User ID
            promo_info: Optional promotional info

        Returns:
            Promotional message text
        """
        tracking_link = await self._build_tracking_link(user_id)

        promo_text = ""
        if promo_info:
            promo_text = promo_info.get("text", "")

        message = f"""
🎁 {promo_text}

限时优惠，立即体验：
{tracking_link}
"""
        return message.strip()

    async def generate_follow_up_message(
        self,
        user_id: int,
        step: str,
    ) -> str:
        """
        Generate follow-up message for guide flow.

        Args:
            user_id: User ID
            step: Current step in guide flow

        Returns:
            Follow-up message text
        """
        tracking_link = await self._build_tracking_link(user_id)

        messages = {
            "welcome": "您好！很高兴认识您~",
            "introduce": "让我简单介绍一下 XBoard...",
            "invite": f"点击链接注册体验：{tracking_link}",
            "confirm": "注册成功后即可享受新用户福利~",
        }

        return messages.get(step, "感谢您的关注！")

    async def _build_tracking_link(
        self,
        user_id: int,
        source_info: Optional[dict] = None,
    ) -> str:
        """Build tracking link with user context."""
        source_info = source_info or {}
        tracking_code = source_info.get("tracking_code") or source_info.get("ref")
        if not tracking_code:
            tracking_code = f"inv_{user_id}_{source_info.get('source', 'tg_private')}"

        campaign = source_info.get("campaign")
        group_id = source_info.get("group_id")
        keyword = source_info.get("keyword")
        bot_id = source_info.get("bot_id")
        source_type = source_info.get("source", "tg_private")

        if self.url_builder.encryption_enabled:
            return await self.url_builder.build_encrypted_url(
                tracking_code=tracking_code,
                source_type=source_type,
                campaign=campaign,
                group_id=group_id,
                keyword=keyword,
                bot_id=bot_id,
            )

        return await self.url_builder.build_tracking_url(
            tracking_code=tracking_code,
            source_type=source_type,
            campaign=campaign,
            group_id=group_id,
            keyword=keyword,
            bot_id=bot_id,
        )

    def get_quick_buttons(self) -> list[dict]:
        """
        Get quick reply buttons for welcome message.

        Returns:
            List of button definitions
        """
        return [
            {"text": "了解更多", "action": "learn_more"},
            {"text": "立即注册", "action": "register"},
            {"text": "查看价格", "action": "pricing"},
        ]
