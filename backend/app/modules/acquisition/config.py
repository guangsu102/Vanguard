"""
Acquisition Module Configuration

Configuration settings for the Telegram acquisition bot.
"""

from typing import Optional
from pydantic import BaseModel, Field


class SearchConfig(BaseModel):
    """群组搜索配置"""

    max_results_per_keyword: int = Field(default=50, description="每个关键词最大搜索结果数")
    min_group_members: int = Field(default=100, description="最小群组成员数")
    max_group_members: int = Field(default=50000, description="最大群组成员数")
    search_interval_seconds: int = Field(default=300, description="搜索间隔(秒)")
    auto_join_enabled: bool = Field(default=True, description="是否自动加入搜索到的群组")


class SpeakerConfig(BaseModel):
    """发言配置"""

    max_messages_per_hour: int = Field(default=20, description="每账号每小时最大发言数")
    max_messages_per_day: int = Field(default=100, description="每账号每天最大发言数")
    min_interval_seconds: int = Field(default=30, description="同一群组最小发言间隔")
    random_delay_min: int = Field(default=5, description="随机延迟最小值(秒)")
    random_delay_max: int = Field(default=30, description="随机延迟最大值(秒)")
    message_type_weights: dict = Field(
        default_factory=lambda: {
            "interaction": 0.4,
            "share": 0.3,
            "guide": 0.2,
            "qa": 0.1,
        },
        description="消息类型权重"
    )


class TriggerConfig(BaseModel):
    """关键词触发配置"""

    cooldown_seconds: int = Field(default=300, description="触发冷却时间(秒)")
    max_triggers_per_user: int = Field(default=5, description="单用户最大触发次数/天")
    max_triggers_per_group: int = Field(default=10, description="单群最大触发次数/天")
    ai_fallback_threshold: int = Field(default=3, description="AI回复fallback阈值")
    enable_ai_reply: bool = Field(default=False, description="是否启用AI动态回复")
    enable_react: bool = Field(default=True, description="是否启用表情反应")


class GuideConfig(BaseModel):
    """私聊引导配置"""

    flow_timeout_minutes: int = Field(default=60, description="流程超时时间(分钟)")
    reminder_interval_minutes: int = Field(default=30, description="提醒间隔(分钟)")
    max_retries: int = Field(default=3, description="最大重试次数")
    auto_close_hours: int = Field(default=24, description="自动关闭时间(小时)")
    private_message_interval: int = Field(default=5, description="私聊消息间隔(秒)")


class TrackingConfig(BaseModel):
    """追踪配置"""

    base_url: str = Field(default="https://xboard.com", description="XBoard注册页面基础URL")
    code_expiry_days: int = Field(default=7, description="追踪链接有效期(天)")
    encryption_enabled: bool = Field(default=True, description="是否启用链接加密")
    encryption_key: Optional[str] = Field(default=None, description="加密密钥")


class AcquisitionConfig(BaseModel):
    """引流模块全局配置"""

    search: SearchConfig = Field(default_factory=SearchConfig)
    speaker: SpeakerConfig = Field(default_factory=SpeakerConfig)
    trigger: TriggerConfig = Field(default_factory=TriggerConfig)
    guide: GuideConfig = Field(default_factory=GuideConfig)
    tracking: TrackingConfig = Field(default_factory=TrackingConfig)

    class Config:
        extra = "ignore"


# 全局配置实例
acquisition_config = AcquisitionConfig()


def get_acquisition_config() -> AcquisitionConfig:
    """获取引流模块配置"""
    return acquisition_config
