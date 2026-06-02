"""
AI Copywriter Module

AI-powered marketing copy generation for Telegram messages.

Features:
- Message template generation
- Response generation
- Coupon message optimization
"""

from dataclasses import dataclass
from typing import Optional

import structlog

from app.core.ai.llm_client import LLMClient
from app.core.ai.intent_classifier import IntentType

logger = structlog.get_logger()


@dataclass
class CopyResult:
    """Copy generation result."""

    content: str
    tone: str
    length: int
    includes_link: bool = False
    includes_coupon: bool = False


class AICopywriter:
    """
    AI-powered marketing copywriter.

    Generates marketing messages for Telegram group interactions.
    """

    COPY_TEMPLATES = {
        "invitation": (
            "想要稳定快速的翻墙服务吗？"
            "我们提供优质节点，新用户立享试用！"
            "{link}"
        ),
        "guide": (
            "如果你需要科学上网，可以看看这里："
            "{link}"
            "新手教程也准备好了哦~"
        ),
        "qa": (
            "关于VPN的问题，我来帮你解答："
            "{answer}"
        ),
        "share": (
            "用了好久的机场，分享给大家："
            "{link}"
            "速度真的很稳！"
        ),
        "promo": (
            "🎉 新用户专属优惠！\n"
            "立即注册即送试用：{link}\n"
            "名额有限，先到先得~"
        ),
    }

    def __init__(self, llm_client: Optional[LLMClient] = None):
        """
        Initialize AICopywriter.

        Args:
            llm_client: Optional LLM client
        """
        self.llm = llm_client
        self.logger = logger.bind(module="ai_copywriter")

    async def generate_response(
        self,
        intent: IntentType,
        context: Optional[dict] = None,
    ) -> str:
        """
        Generate response for intent.

        Args:
            intent: Classified intent
            context: Context data (link, coupon, etc.)

        Returns:
            Generated response text
        """
        context = context or {}

        if not self.llm:
            return self._generate_template_response(intent, context)

        prompts = {
            IntentType.DEMAND: self._demand_prompt(context),
            IntentType.INQUIRY: self._inquiry_prompt(context),
            IntentType.PRICE: self._price_prompt(context),
            IntentType.COMPARISON: self._comparison_prompt(context),
            IntentType.COMPLAINT: self._complaint_prompt(context),
        }

        prompt = prompts.get(intent)
        if not prompt:
            return self._generate_template_response(intent, context)

        try:
            response = await self.llm.generate(
                prompt,
                model="gpt-4o",
                temperature=0.8,
                max_tokens=200,
            )
            return response.strip()

        except Exception as e:
            self.logger.error("copy_generation_error", error=str(e))
            return self._generate_template_response(intent, context)

    def _demand_prompt(self, context: dict) -> str:
        """Generate demand intent response prompt."""
        return f"""生成一条回复用户购买意向的消息。

产品信息：{context.get('product_info', '优质VPN服务')}
活动信息：{context.get('campaign_info', '新用户试用')}
注册链接：{context.get('register_link', 'https://example.com/register')}

要求：
- 语气亲切但专业
- 突出服务优势
- 引导点击注册
- 长度控制在100字以内
- 可以提到优惠活动"""

    def _inquiry_prompt(self, context: dict) -> str:
        """Generate inquiry intent response prompt."""
        return f"""生成一条回复用户咨询的消息。

问题类型：{context.get('question_type', '产品功能')}
回复内容：{context.get('answer', '我们支持多平台，有试用套餐')}

要求：
- 简洁明了回答问题
- 适当引导到注册
- 语气友好
- 长度控制在80字以内"""

    def _price_prompt(self, context: dict) -> str:
        """Generate price inquiry response prompt."""
        return f"""生成一条回复用户价格咨询的消息。

价格信息：{context.get('price_info', '月付¥20起')}
优惠信息：{context.get('promo_info', '新用户8折')}

要求：
- 突出性价比
- 提到优惠
- 引导注册
- 长度控制在80字以内"""

    def _comparison_prompt(self, context: dict) -> str:
        """Generate comparison response prompt."""
        return """生成一条回复用户竞品对比的消息。

要求：
- 客观介绍优势
- 不贬低竞品
- 突出我们特点
- 引导注册体验
- 长度控制在100字以内"""

    def _complaint_prompt(self, context: dict) -> str:
        """Generate complaint response prompt."""
        return """生成一条回复用户投诉的消息。

要求：
- 先表示歉意和理解
- 询问具体情况
- 表示会改进
- 如需要可转人工
- 语气诚恳
- 长度控制在100字以内"""

    def _generate_template_response(
        self,
        intent: IntentType,
        context: dict,
    ) -> str:
        """Generate response from template."""
        link = context.get("register_link", "[注册链接]")

        templates = {
            IntentType.DEMAND: f"欢迎了解我们的服务！注册即享试用：{link}",
            IntentType.INQUIRY: f"我们支持多平台使用，详情：{link}",
            IntentType.PRICE: f"月付¥20起，新用户有优惠：{link}",
            IntentType.COMPARISON: f"我们专注服务质量和稳定性，试试就知道：{link}",
            IntentType.COMPLAINT: "非常抱歉给您带来不便，请告诉我们具体情况~",
            IntentType.CHITCHAT: "谢谢！有什么可以帮您的吗？",
            IntentType.OTHER: f"了解，欢迎体验：{link}",
        }

        return templates.get(intent, templates[IntentType.OTHER])

    async def generate_group_messages(
        self,
        message_type: str = "interaction",
        count: int = 5,
    ) -> list[str]:
        """
        Generate group speaking messages.

        Args:
            message_type: Type of message (interaction, share, guide, qa)
            count: Number of messages to generate

        Returns:
            List of generated messages
        """
        prompts = {
            "interaction": """生成5条Telegram群组互动型发言：
- 模拟用户提问或讨论
- 自然不生硬
- 每条50字以内
- 格式：每条一行""",

            "share": """生成5条Telegram群组分享型发言：
- 分享使用经验或推荐
- 像朋友推荐
- 不要太像广告
- 每条50字以内
- 格式：每条一行""",

            "guide": """生成5条Telegram群组引导型发言：
- 带注册链接
- 引导点击
- 不要太像广告
- 每条60字以内
- 格式：每条一行""",

            "qa": """生成5条Telegram群组问答型发言：
- 回答常见问题
- 简短有用
- 结尾可带链接
- 每条50字以内
- 格式：每条一行""",
        }

        prompt = prompts.get(message_type, prompts["interaction"])

        if not self.llm:
            return self._fallback_messages(message_type, count)

        try:
            response = await self.llm.generate(
                prompt,
                model="gpt-4o",
                temperature=0.8,
                max_tokens=500,
            )

            messages = [
                line.strip()
                for line in response.split("\n")
                if line.strip()
            ]
            return messages[:count]

        except Exception as e:
            self.logger.error("group_message_generation_error", error=str(e))
            return self._fallback_messages(message_type, count)

    def _fallback_messages(self, message_type: str, count: int) -> list[str]:
        """Fallback messages without LLM."""
        templates = {
            "interaction": [
                "大家平时都用什么节点比较稳定啊？",
                "有没有适合新手的配置教程？",
                "感觉最近速度有点慢是为什么？",
            ],
            "share": [
                "用了一段时间，速度确实不错，推荐给大家",
                "这个服务用了半年了，一直很稳定",
                "节点切换很方便，体验很好",
            ],
            "guide": [
                "需要翻墙的朋友可以试试这个，注册送试用",
                "整理了一份使用教程，需要的可以看看",
                "新手入门可以先从这里开始",
            ],
            "qa": [
                "支持Windows/Mac/Android/iOS，点击了解详情",
                "是的，我们有24小时在线客服",
                "节点列表每天更新，确保速度",
            ],
        }

        return templates.get(message_type, templates["interaction"])[:count]

    async def optimize_coupon_message(
        self,
        coupon_info: dict,
    ) -> str:
        """
        Optimize coupon message.

        Args:
            coupon_info: Coupon details

        Returns:
            Optimized message
        """
        prompt = f"""将以下优惠券信息优化成吸引人的文案：

{coupon_info}

要求：
- 营造紧迫感（限时优惠）
- 突出价值
- 包含有效期
- 长度控制在60字以内"""

        if not self.llm:
            return self._default_coupon_message(coupon_info)

        try:
            return await self.llm.generate(
                prompt,
                model="gpt-4o",
                temperature=0.8,
                max_tokens=100,
            )

        except Exception as e:
            self.logger.error("coupon_optimization_error", error=str(e))
            return self._default_coupon_message(coupon_info)

    def _default_coupon_message(self, coupon_info: dict) -> str:
        """Default coupon message."""
        return (
            f"🎁 {coupon_info.get('name', '优惠券')}来啦！"
            f"价值{coupon_info.get('value', '惊喜')}，"
            f"立即领取：{coupon_info.get('link', '链接')}"
        )
