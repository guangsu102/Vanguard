"""
Content Moderation AI Module

Provides AI-powered content moderation for detecting violations.

Features:
- Content analysis
- Competitor detection
- Spam detection
- Confidence scoring
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import structlog

from app.core.ai.llm_client import LLMClient

logger = structlog.get_logger()


class ViolationType(str, Enum):
    """Violation types."""

    COMPETITOR = "competitor"
    SPAM = "spam"
    SCAM = "scam"
    SENSITIVE = "sensitive"
    SAFE = "safe"


class ViolationLevel(str, Enum):
    """Violation severity levels."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class ModerationResult:
    """Content moderation result."""

    is_violation: bool
    violation_type: ViolationType
    level: ViolationLevel
    confidence: float
    reason: str
    matched_patterns: list[str]


class ModerationAI:
    """
    AI-powered content moderation.

    Analyzes messages for violations using LLM and pattern matching.
    """

    MODERATION_PROMPT = """你是一个Telegram群组内容审核助手。判断消息是否违规。

违规类型：
- competitor: 竞品推广（VPN/机场/翻墙相关竞品）
- spam: 垃圾广告
- scam: 诈骗内容
- sensitive: 敏感内容
- safe: 安全内容

判断依据：
1. 语义分析：不仅仅是关键词匹配，要理解上下文
2. 变体识别：能识别"机+场"、"机勾"等变形词
3. 图片分析：描述图片内容是否违规（如果有）

违规等级：
- high: 严重违规（竞品外链、直接推广）
- medium: 一般违规（可疑链接、暗示推广）
- low: 轻微违规（可疑词汇）

请返回JSON格式：
{{"violation": true/false, "type": "违规类型", "level": "等级", "confidence": 0.95, "reason": "理由"}}

消息内容：
{input}"""

    # Competitor keywords for quick detection
    COMPETITOR_KEYWORDS = [
        "机场", "节点", "梯子", "VPN", "加速器", "翻墙",
        "v2ray", "clash", "ssr", "trojan",
    ]

    # Competitor domain patterns
    COMPETITOR_DOMAINS = [
        r"\.(vip|xyz|top|cc)$",
        r"t\.me/\w+",
        r"bit\.ly",
        r"goo\.gl",
    ]

    def __init__(self, llm_client: Optional[LLMClient] = None):
        """
        Initialize ModerationAI.

        Args:
            llm_client: Optional LLM client
        """
        self.llm = llm_client
        self.logger = logger.bind(module="moderation_ai")

    async def analyze(
        self,
        content: str,
        context: Optional[list[str]] = None,
    ) -> ModerationResult:
        """
        Analyze content for violations.

        Args:
            content: Message content
            context: Optional context messages

        Returns:
            ModerationResult
        """
        quick_result = self._quick_check(content)
        if quick_result and quick_result.level == ViolationLevel.HIGH:
            return quick_result

        if not self.llm:
            return quick_result or ModerationResult(
                is_violation=False,
                violation_type=ViolationType.SAFE,
                level=ViolationLevel.LOW,
                confidence=0.5,
                reason="未检测到违规内容",
                matched_patterns=[],
            )

        try:
            prompt = self.MODERATION_PROMPT.format(input=content)
            if context:
                prompt += "\n\n前几条消息参考：\n" + "\n".join(context[-3:])

            response = await self.llm.generate(prompt, model="gpt-4o")

            import json
            result = json.loads(response)

            return ModerationResult(
                is_violation=result["violation"],
                violation_type=ViolationType(result["type"]),
                level=ViolationLevel(result["level"]),
                confidence=float(result["confidence"]),
                reason=result["reason"],
                matched_patterns=self._get_matched_keywords(content),
            )

        except Exception as e:
            self.logger.error("moderation_error", error=str(e))
            return quick_result or ModerationResult(
                is_violation=False,
                violation_type=ViolationType.SAFE,
                level=ViolationLevel.LOW,
                confidence=0.3,
                reason="审核出错，使用默认结果",
                matched_patterns=[],
            )

    def _quick_check(self, content: str) -> Optional[ModerationResult]:
        """
        Quick pattern-based check.

        Args:
            content: Message content

        Returns:
            ModerationResult or None
        """
        matched_keywords = self._get_matched_keywords(content)
        matched_domains = self._get_matched_domains(content)

        if not matched_keywords and not matched_domains:
            return None

        if matched_domains:
            return ModerationResult(
                is_violation=True,
                violation_type=ViolationType.COMPETITOR,
                level=ViolationLevel.HIGH,
                confidence=0.95,
                reason="检测到可疑外链",
                matched_patterns=matched_domains,
            )

        competitor_count = len([k for k in matched_keywords if k in self.COMPETITOR_KEYWORDS])

        if competitor_count >= 2:
            return ModerationResult(
                is_violation=True,
                violation_type=ViolationType.COMPETITOR,
                level=ViolationLevel.HIGH,
                confidence=0.9,
                reason=f"检测到{competitor_count}个竞品关键词",
                matched_patterns=matched_keywords,
            )

        return ModerationResult(
            is_violation=True,
            violation_type=ViolationType.SPAM,
            level=ViolationLevel.MEDIUM,
            confidence=0.7,
            reason="检测到可疑词汇",
            matched_patterns=matched_keywords,
        )

    def _get_matched_keywords(self, content: str) -> list[str]:
        """Get matched competitor keywords."""
        text = content.lower()
        matched = []
        for keyword in self.COMPETITOR_KEYWORDS:
            if keyword in text:
                matched.append(keyword)
        return matched

    def _get_matched_domains(self, content: str) -> list[str]:
        """Get matched competitor domain patterns."""
        import re
        matched = []
        for pattern in self.COMPETITOR_DOMAINS:
            if re.search(pattern, content, re.IGNORECASE):
                matched.append(pattern)
        return matched

    async def batch_analyze(self, contents: list[str]) -> list[ModerationResult]:
        """
        Analyze multiple messages.

        Args:
            contents: List of message contents

        Returns:
            List of ModerationResult
        """
        import asyncio

        tasks = [self.analyze(content) for content in contents]
        return await asyncio.gather(*tasks)

    def is_competitor_mention(self, content: str) -> bool:
        """
        Quick check if content mentions competitors.

        Args:
            content: Message content

        Returns:
            True if competitor mentioned
        """
        return len(self._get_matched_keywords(content)) > 0

    def get_action_for_violation(self, result: ModerationResult) -> str:
        """
        Get recommended action for violation.

        Args:
            result: ModerationResult

        Returns:
            Action name
        """
        if result.level == ViolationLevel.HIGH:
            if result.violation_type == ViolationType.COMPETITOR:
                return "ban"
            return "mute"
        elif result.level == ViolationLevel.MEDIUM:
            return "warn"
        else:
            return "ignore"


# =============================================================================
# Sensitive Keyword Generation (AI-assisted)
# =============================================================================

@dataclass
class SensitiveKeywordSuggestion:
    """AI-generated sensitive keyword suggestion."""

    keyword: str
    category: str
    confidence: float
    source_sample: str


class SensitiveKeywordGenerator:
    """
    AI-assisted sensitive keyword generator.

    Analyzes violation samples to generate keyword suggestions.
    This is NOT used for real-time detection (to save costs).
    """

    KEYWORD_GENERATION_PROMPT = """你是一个Telegram群组内容审核专家。根据以下违规样本，分析并生成候选敏感词。

任务：
1. 分析每个违规样本，找出其中的敏感内容
2. 提取可能用于检测的关键词/短语
3. 识别常见的变体形式（如谐音、拼音首字母等）

违规类型：{category}

样本列表：
{samples}

输出要求：
- 每行一个关键词/短语
- 优先提取明确的竞品名称、服务名称
- 提取具有检测价值的短语（长度2-10字）
- 只输出关键词，不要其他说明

请列出候选敏感词（每行一个）："""

    def __init__(self, llm_client: Optional[LLMClient] = None):
        """Initialize generator."""
        self.llm = llm_client
        self.logger = logger.bind(module="sensitive_keyword_generator")

    async def generate_sensitive_keywords(
        self,
        samples: list[str],
        category: str = "competitor",
        max_keywords: int = 20,
    ) -> list[SensitiveKeywordSuggestion]:
        """
        Generate sensitive keyword suggestions from violation samples.

        This is called manually (not in real-time) to save API costs.

        Args:
            samples: List of violation sample texts
            category: Violation category (competitor, spam, scam, sensitive)
            max_keywords: Maximum number of keywords to generate

        Returns:
            List of SensitiveKeywordSuggestion
        """
        if not self.llm:
            self.logger.warning("llm_not_configured")
            return self._fallback_generate(samples, category, max_keywords)

        if not samples:
            return []

        # Prepare samples text
        samples_text = "\n".join([f"[{i+1}] {s}" for i, s in enumerate(samples[:50])])

        prompt = self.KEYWORD_GENERATION_PROMPT.format(
            category=category,
            samples=samples_text,
        )

        try:
            response = await self.llm.generate(
                prompt,
                model=self.llm.model_for("fast"),
                temperature=0.7,
                max_tokens=500,
            )

            # Parse response
            keywords = self._parse_keywords(response)

            # Create suggestions
            suggestions = []
            for kw in keywords[:max_keywords]:
                suggestions.append(
                    SensitiveKeywordSuggestion(
                        keyword=kw.strip(),
                        category=category,
                        confidence=0.8,  # Default confidence
                        source_sample=samples[0][:100],  # First sample as reference
                    )
                )

            self.logger.info(
                "keywords_generated",
                category=category,
                count=len(suggestions),
            )

            return suggestions

        except Exception as e:
            self.logger.error("keyword_generation_error", error=str(e))
            return self._fallback_generate(samples, category, max_keywords)

    def _parse_keywords(self, response: str) -> list[str]:
        """Parse keywords from LLM response."""
        keywords = []
        for line in response.strip().split("\n"):
            line = line.strip()
            # Skip empty lines and numbered prefixes
            if not line:
                continue
            # Remove common prefixes
            line = line.lstrip("0123456789.-*、  ")
            if line and len(line) >= 2:
                keywords.append(line)
        return keywords

    def _fallback_generate(
        self,
        samples: list[str],
        category: str,
        max_keywords: int,
    ) -> list[SensitiveKeywordSuggestion]:
        """Fallback generation when LLM is not available."""
        # Extract common patterns from samples as simple fallback
        import re

        all_text = " ".join(samples)
        # Find common VPN-related terms
        patterns = [
            r'\b[\u4e00-\u9fa5]{2,6}(机场|节点|梯子|VPN|加速器)\b',
            r'\b(v2ray|clash|ssr|trojan)\b',
            r'\b\w+\.(vip|xyz|top|cc)\b',
        ]

        found_keywords = set()
        for pattern in patterns:
            matches = re.findall(pattern, all_text, re.IGNORECASE)
            found_keywords.update(matches)

        return [
            SensitiveKeywordSuggestion(
                keyword=kw,
                category=category,
                confidence=0.5,
                source_sample=samples[0][:100] if samples else "",
            )
            for kw in list(found_keywords)[:max_keywords]
        ]


# Backward compatibility alias
async def generate_sensitive_keywords(
    samples: list[str],
    category: str = "competitor",
    llm_client: Optional[LLMClient] = None,
) -> list[SensitiveKeywordSuggestion]:
    """
    Standalone function for generating sensitive keywords.

    This is the function called by the moderation API.
    """
    generator = SensitiveKeywordGenerator(llm_client)
    return await generator.generate_sensitive_keywords(samples, category)
