"""
Broadcast Templates

Message templates for broadcasting to groups.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Any

import structlog

logger = structlog.get_logger()


@dataclass
class NodeStatus:
    """Node status data."""
    node_name: str
    status: str
    timestamp: datetime
    reason: Optional[str] = None
    eta: Optional[str] = None


@dataclass
class PromoData:
    """Promotion data."""
    campaign_name: str
    description: str
    validity: str
    claim_url: str
    bonus: Optional[str] = None


class BroadcastTemplate:
    """
    Message templates for broadcasting.
    
    Provides templates for various broadcast types:
    - Node status updates
    - Welcome messages
    - Promotions
    - System alerts
    """
    
    TEMPLATES = {
        "node_online": """🟢 *节点恢复在线*

📍 节点：`{node_name}`
⏰ 时间：{timestamp}

请重新连接使用~
""",
        
        "node_offline": """🔴 *节点临时下线*

📍 节点：`{node_name}`
⏰ 时间：{timestamp}
📝 原因：{reason}

预计恢复时间：{eta}
""",
        
        "welcome": """👋 欢迎 *{username}* 加入！

请先完成入群验证~
{verification_instruction}

有任何问题可以私聊管理员。
""",
        
        "welcome_no_verify": """👋 欢迎 *{username}* 加入！

请遵守群规，共同维护良好的群聊环境。

有任何问题可以私聊管理员。
""",
        
        "promo_trial": """🎉 *新用户专属福利*

✨ *{campaign_name}*
📝 {description}
⏰ 有效期：{validity}

🎁 *{bonus}*

点击领取：{claim_url}
""",
        
        "promo_discount": """🎫 *限时优惠*

✨ *{campaign_name}*
📝 {description}
⏰ 有效期：{validity}

点击领取：{claim_url}
""",
        
        "warning": """⚠️ *群规提醒*

{username}，您的消息可能含有不当内容：
> {violation_preview}

请遵守群规，共同维护良好环境。
""",
        
        "system_alert": """📢 *系统通知*

{message}

⏰ {timestamp}
""",
        
        "daily_stats": """📊 *每日数据报告*

✅ 今日注册：{new_users}
💬 今日消息：{messages}
👥 在线人数：{online_users}

⏰ {timestamp}
""",
        
        "verification_hint": """🔐 请点击下方按钮完成验证"""
    }
    
    def __init__(self):
        """Initialize BroadcastTemplate."""
        self.logger = logger.bind(module="broadcast_template")
    
    def render(self, template_name: str, **kwargs) -> str:
        """
        Render a template with given variables.
        
        Args:
            template_name: Name of the template
            **kwargs: Template variables
            
        Returns:
            Rendered message
        """
        template = self.TEMPLATES.get(template_name)
        
        if not template:
            self.logger.warning("template_not_found", name=template_name)
            return ""
        
        try:
            return template.format(**kwargs)
        except KeyError as e:
            self.logger.error("template_render_error", template=template_name, missing_key=str(e))
            return template
    
    def get_node_online(self, node_name: str, timestamp: Optional[datetime] = None) -> str:
        """Get node online message."""
        ts = timestamp or datetime.now()
        return self.render(
            "node_online",
            node_name=node_name,
            timestamp=ts.strftime("%Y-%m-%d %H:%M:%S")
        )
    
    def get_node_offline(
        self,
        node_name: str,
        reason: str,
        eta: Optional[str] = None,
        timestamp: Optional[datetime] = None
    ) -> str:
        """Get node offline message."""
        ts = timestamp or datetime.now()
        return self.render(
            "node_offline",
            node_name=node_name,
            timestamp=ts.strftime("%Y-%m-%d %H:%M:%S"),
            reason=reason,
            eta=eta or "待定"
        )
    
    def get_welcome(
        self,
        username: str,
        needs_verification: bool = True
    ) -> str:
        """Get welcome message."""
        if needs_verification:
            return self.render(
                "welcome",
                username=username,
                verification_instruction=self.TEMPLATES["verification_hint"]
            )
        else:
            return self.render("welcome_no_verify", username=username)
    
    def get_promo_trial(
        self,
        campaign_name: str,
        description: str,
        validity: str,
        claim_url: str,
        bonus: Optional[str] = None
    ) -> str:
        """Get trial promotion message."""
        return self.render(
            "promo_trial",
            campaign_name=campaign_name,
            description=description,
            validity=validity,
            claim_url=claim_url,
            bonus=bonus or "免费试用"
        )
    
    def get_promo_discount(
        self,
        campaign_name: str,
        description: str,
        validity: str,
        claim_url: str
    ) -> str:
        """Get discount promotion message."""
        return self.render(
            "promo_discount",
            campaign_name=campaign_name,
            description=description,
            validity=validity,
            claim_url=claim_url
        )
    
    def get_warning(
        self,
        username: str,
        violation_preview: str
    ) -> str:
        """Get warning message."""
        return self.render(
            "warning",
            username=username,
            violation_preview=violation_preview[:50]
        )
    
    def get_system_alert(self, message: str) -> str:
        """Get system alert message."""
        return self.render(
            "system_alert",
            message=message,
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
    
    def get_daily_stats(
        self,
        new_users: int,
        messages: int,
        online_users: int
    ) -> str:
        """Get daily statistics message."""
        return self.render(
            "daily_stats",
            new_users=new_users,
            messages=messages,
            online_users=online_users,
            timestamp=datetime.now().strftime("%Y-%m-%d")
        )
