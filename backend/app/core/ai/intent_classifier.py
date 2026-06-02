"""
Intent Classifier Module

Classifies user message intent for response routing.

Features:
- Multi-class intent classification
- Confidence scoring
- Response strategy mapping
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import structlog

from app.core.ai.llm_client import LLMClient

logger = structlog.get_logger()


class IntentType(str, Enum):
    """User intent types."""

    DEMAND = "demand"
    INQUIRY = "inquiry"
    PRICE = "price"
    COMPARISON = "comparison"
    COMPLAINT = "complaint"
    CHITCHAT = "chitchat"
    OTHER = "other"


@dataclass
class IntentResult:
    """Intent classification result."""

    intent: IntentType
    confidence: float
    reason: str
    keywords: list[str]


@dataclass
class ResponseStrategy:
    """Response strategy configuration."""

    action: str
    priority: str
    template: Optional[str] = None
    include_coupon: bool = False
    include_link: bool = False
    notify_admin: bool = False


class IntentClassifier:
    """
    Intent classifier using LLM.

    Classifies user messages into intent types and provides
    response strategies.
    """

    CLASSIFIER_PROMPT = """你是一个Telegram群组消息意图分类器。根据用户消息，判断其意图类型。

消息类型定义：
- demand: 表达购买意向，如"想要试试"、"有套餐吗"、"推荐一个"
- inquiry: 询问产品功能/使用方法，如"怎么用"、"支持哪些节点"
- price: 询问价格，如"多少钱"、"价格多少"
- comparison: 对比竞品，如"比XX好用吗"、"和其他的比怎么样"
- complaint: 投诉或负面反馈
- chitchat: 无意义的闲聊或表情
- other: 无法归类

请分析以下消息并返回JSON格式：
{{"intent": "类型", "confidence": 0.95, "reason": "简短理由", "keywords": ["关键词1", "关键词2"]}}

消息内容：
{input}"""

    def __init__(self, llm_client: Optional[LLMClient] = None):
        """
        Initialize IntentClassifier.

        Args:
            llm_client: Optional LLM client for classification
        """
        self.llm = llm_client
        self.llm_client = llm_client
        self.logger = logger.bind(module="intent_classifier")

    async def classify(self, message: str) -> IntentResult:
        """
        Classify message intent.

        Args:
            message: Message text to classify

        Returns:
            IntentResult with classification
        """
        if not self.llm:
            return self._rule_based_classify(message)

        try:
            prompt = self.CLASSIFIER_PROMPT.format(input=message)
            response = await self.llm.generate(prompt, model="gpt-4o-mini")

            import json
            result = json.loads(response)

            return IntentResult(
                intent=IntentType(result["intent"]),
                confidence=float(result["confidence"]),
                reason=result["reason"],
                keywords=result.get("keywords", []),
            )

        except Exception as e:
            self.logger.error("classification_error", error=str(e))
            return self._rule_based_classify(message)

    def _rule_based_classify(self, message: str) -> IntentResult:
        """
        Rule-based intent classification fallback.

        Args:
            message: Message to classify

        Returns:
            IntentResult
        """
        text = message.lower()

        if any(kw in text for kw in ["买", "想要", "试试", "套餐", "推荐"]):
            return IntentResult(
                intent=IntentType.DEMAND,
                confidence=0.8,
                reason="购买意向关键词",
                keywords=self._extract_keywords(text),
            )

        if any(kw in text for kw in ["怎么", "如何", "什么", "是否", "能"]):
            return IntentResult(
                intent=IntentType.INQUIRY,
                confidence=0.7,
                reason="询问功能关键词",
                keywords=self._extract_keywords(text),
            )

        if any(kw in text for kw in ["价格", "多少", "钱", "收费"]):
            return IntentResult(
                intent=IntentType.PRICE,
                confidence=0.9,
                reason="价格咨询关键词",
                keywords=self._extract_keywords(text),
            )

        if any(kw in text for kw in ["比", "对比", "其他", "区别"]):
            return IntentResult(
                intent=IntentType.COMPARISON,
                confidence=0.8,
                reason="竞品对比关键词",
                keywords=self._extract_keywords(text),
            )

        if any(kw in text for kw in ["不好", "垃圾", "退款", "投诉", "差"]):
            return IntentResult(
                intent=IntentType.COMPLAINT,
                confidence=0.9,
                reason="投诉反馈关键词",
                keywords=self._extract_keywords(text),
            )

        if any(kw in text for kw in ["谢谢", "好的", "ok", "👋", "👍"]):
            return IntentResult(
                intent=IntentType.CHITCHAT,
                confidence=0.8,
                reason="闲聊关键词",
                keywords=[],
            )

        return IntentResult(
            intent=IntentType.OTHER,
            confidence=0.5,
            reason="无法归类",
            keywords=[],
        )

    def _extract_keywords(self, text: str) -> list[str]:
        """Extract keywords from text."""
        keywords = []
        for kw in ["机场", "节点", "VPN", "梯子", "套餐", "流量", "速度", "价格"]:
            if kw in text:
                keywords.append(kw)
        return keywords[:3]

    def get_strategy(self, intent: IntentType) -> ResponseStrategy:
        """
        Get response strategy for intent.

        Args:
            intent: Classified intent

        Returns:
            ResponseStrategy
        """
        strategies = {
            IntentType.DEMAND: ResponseStrategy(
                action="private_chat",
                priority="high",
                template="邀请注册",
                include_coupon=True,
            ),
            IntentType.INQUIRY: ResponseStrategy(
                action="group_reply",
                priority="medium",
                template="产品介绍",
                include_link=True,
            ),
            IntentType.PRICE: ResponseStrategy(
                action="private_chat",
                priority="high",
                template="价格说明",
                include_coupon=True,
            ),
            IntentType.COMPARISON: ResponseStrategy(
                action="group_reply",
                priority="medium",
                template="优势对比",
                include_link=True,
            ),
            IntentType.COMPLAINT: ResponseStrategy(
                action="escalate",
                priority="high",
                template="道歉安抚",
                notify_admin=True,
            ),
            IntentType.CHITCHAT: ResponseStrategy(
                action="ignore",
                priority="low",
            ),
            IntentType.OTHER: ResponseStrategy(
                action="keyword_fallback",
                priority="medium",
            ),
        }

        return strategies.get(intent, strategies[IntentType.OTHER])

    async def classify_and_get_strategy(self, message: str) -> tuple[IntentResult, ResponseStrategy]:
        """
        Classify message and get response strategy.

        Args:
            message: Message to classify

        Returns:
            Tuple of (IntentResult, ResponseStrategy)
        """
        result = await self.classify(message)
        strategy = self.get_strategy(result.intent)

        self.logger.debug(
            "intent_classified",
            intent=result.intent.value,
            confidence=result.confidence,
            action=strategy.action,
        )

        return result, strategy
