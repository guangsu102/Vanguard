"""
Guardian Module Configuration

Configuration settings for the Telegram guardian bot.
"""

from typing import Optional
from pydantic import BaseModel, Field


class GuardianConfig(BaseModel):
    """守护 Bot 全局配置"""

    # 验证配置
    verification_timeout_minutes: int = Field(default=5, description="验证超时时间(分钟)")
    max_verification_attempts: int = Field(default=3, description="最大验证尝试次数")
    whitelist_bypass_verification: bool = Field(default=True, description="白名单用户跳过验证")

    # 惩罚配置
    warning_threshold: int = Field(default=3, description="警告次数阈值，超过后禁言")
    mute_duration_seconds: int = Field(default=300, description="默认禁言时长(秒)")
    ban_threshold: int = Field(default=5, description="禁言次数阈值，超过后永久封禁")
    
    # 低危惩罚策略（禁言时长）
    low_violation_mute_seconds: int = Field(default=300, description="低危违规禁言时长")
    medium_violation_mute_seconds: int = Field(default=1800, description="中危违规禁言时长")
    high_violation_mute_seconds: int = Field(default=3600, description="高危违规禁言时长")

    # 反垃圾配置
    max_messages_per_minute: int = Field(default=5, description="每分钟最大消息数")
    max_repeated_messages: int = Field(default=3, description="最大重复消息数")
    link_spam_threshold: int = Field(default=3, description="链接垃圾检测阈值")

    # 播报配置
    node_status_broadcast_interval: int = Field(default=300, description="节点状态播报间隔(秒)")
    alert_chat_id: Optional[int] = Field(default=None, description="告警通知群组ID")

    # 优惠券配置
    auto_distribute_trial: bool = Field(default=True, description="自动发放试用")
    auto_distribute_discount: bool = Field(default=False, description="自动发放折扣")

    class Config:
        extra = "ignore"


class GroupVerificationConfigModel(BaseModel):
    """群组验证配置"""

    enable_verification: bool = Field(default=False, description="是否启用入群验证")
    verification_type: str = Field(default="captcha", description="验证类型: captcha/question")
    
    # 问答配置（JSON字符串）
    questions: Optional[str] = Field(default=None, description="问答配置JSON")
    
    # 欢迎消息
    welcome_message: Optional[str] = Field(
        default="欢迎 {username} 加入群聊！",
        description="欢迎消息模板"
    )
    
    # 超时配置
    timeout_minutes: int = Field(default=5, description="验证超时时间(分钟)")
    
    # 白名单跳过
    whitelist_bypass: bool = Field(default=True, description="白名单用户跳过验证")

    class Config:
        extra = "ignore"


# 全局配置实例
guardian_config = GuardianConfig()


def get_guardian_config() -> GuardianConfig:
    """获取守护 Bot 配置"""
    return guardian_config
