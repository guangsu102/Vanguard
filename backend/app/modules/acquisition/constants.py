"""
Acquisition Module Constants

Constants and enums used across the acquisition module.
"""

from enum import Enum


class ResponseMode(str, Enum):
    """Response mode for keyword triggers."""
    PRIVATE = "private"       # 私聊响应
    GROUP = "group"           # 群内响应
    AI = "ai"                # AI生成回复
    TEMPLATE = "template"    # 模板回复
    IGNORE = "ignore"        # 忽略


class SourceType(str, Enum):
    """User source type for tracking."""
    TG_GROUP = "tg_group"         # Telegram群组
    TG_PRIVATE = "tg_private"     # Telegram私聊
    SEARCH = "search"             # 搜索引擎
    OTHER = "other"              # 其他来源


class GuideStep(str, Enum):
    """Guide flow step identifiers."""
    WELCOME = "welcome"           # 欢迎
    INTRODUCE = "introduce"       # 介绍
    INVITE_REGISTER = "invite_register"  # 引导注册
    CONFIRM = "confirm"          # 确认


# 引导流程超时时间配置（秒）
GUIDE_STEP_TIMEOUTS = {
    GuideStep.WELCOME: 300,          # 5分钟
    GuideStep.INTRODUCE: 600,        # 10分钟
    GuideStep.INVITE_REGISTER: 1800,  # 30分钟
    GuideStep.CONFIRM: 3600,         # 60分钟
}

# 消息内容类型权重
MESSAGE_TYPE_WEIGHTS = {
    "interaction": 0.4,  # 互动型
    "share": 0.3,       # 分享型
    "guide": 0.2,       # 引导型
    "qa": 0.1,          # 问答型
}

# 意图类型到响应策略的映射
INTENT_RESPONSE_MAP = {
    "demand": ResponseMode.PRIVATE,
    "inquiry": ResponseMode.GROUP,
    "price": ResponseMode.PRIVATE,
    "comparison": ResponseMode.GROUP,
    "complaint": ResponseMode.IGNORE,
    "chitchat": ResponseMode.IGNORE,
}

# 默认欢迎消息模板
DEFAULT_WELCOME_TEMPLATE = """
你好 {user_name}！

欢迎了解 XBoard，我们提供高速稳定的网络加速服务 🚀

有需要可以随时问我，或者直接点击下面的链接注册体验：
{register_link}

有任何问题欢迎私信咨询~
"""

# 默认引导消息模板
DEFAULT_GUIDE_MESSAGES = {
    GuideStep.WELCOME: "很高兴认识你！有什么可以帮助你的吗？",
    GuideStep.INTRODUCE: "XBoard 提供全球节点覆盖的高速网络加速服务，支持多设备同时使用。",
    GuideStep.INVITE_REGISTER: "新用户有免费试用机会，点击这里注册：{register_link}",
    GuideStep.CONFIRM: "注册成功后会获得试用时长，快去体验吧！",
}

# 意图分类关键词（用于规则匹配）
INTENT_KEYWORDS = {
    "demand": ["买", "想要", "试试", "套餐", "推荐", "哪个好", "怎么买"],
    "inquiry": ["怎么", "如何", "是什么", "支持", "哪些节点"],
    "price": ["价格", "多少", "钱", "收费", "套餐价格"],
    "comparison": ["比", "对比", "比较", "区别", "其他"],
    "complaint": ["不好", "垃圾", "退款", "投诉", "差"],
    "chitchat": ["谢谢", "好的", "ok", "👋", "👍", "哈哈"],
}

# 限流 Redis Key 前缀
RATE_LIMIT_KEYS = {
    "user_trigger": "acq:trigger:user:{user_id}",
    "group_trigger": "acq:trigger:group:{group_id}",
    "account_speak": "acq:speak:account:{account_id}",
    "group_speak": "acq:speak:group:{group_id}",
    "private_message": "acq:private:user:{user_id}",
}
