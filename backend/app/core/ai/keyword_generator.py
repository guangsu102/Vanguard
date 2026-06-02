"""
Keyword Generator Module

AI-powered keyword generation for Telegram group marketing.

Features:
- Category-based keyword generation
- Batch generation
- Deduplication
"""

from typing import Optional

import structlog

from app.core.ai.llm_client import LLMClient
from app.core.keyword.models import Keyword, KeywordType, KeywordStatus

logger = structlog.get_logger()


class KeywordGenerator:
    """
    AI-powered keyword generator.

    Generates keywords for Telegram group marketing using LLM.
    """

    CATEGORY_PROMPTS = {
        "demand": """生成Telegram群组引流用的需求类关键词，用户想要购买VPN/机场服务时会搜索的词。

要求：
1. 与VPN/机场/翻墙相关
2. 涵盖不同搜索场景
3. 每行一个关键词
4. 生成20个关键词""",

        "inquiry": """生成Telegram群组引流用的咨询类关键词，用户询问产品功能时会使用的词。

要求：
1. 与VPN/机场功能相关
2. 涵盖使用方法、节点、速度等问题
3. 每行一个关键词
4. 生成15个关键词""",

        "price": """生成Telegram群组引流用的价格类关键词，用户比较价格时会搜索的词。

要求：
1. 与VPN/机场价格相关
2. 涵盖优惠、折扣、免费等
3. 每行一个关键词
4. 生成10个关键词""",

        "competitor": """生成竞品类关键词，用于检测竞品推广。

要求：
1. 包含常见竞品名称变体
2. 包含行业通用竞品词
3. 格式如"XX机场"、"XX加速器"
4. 生成15个关键词""",
    }

    def __init__(self, llm_client: Optional[LLMClient] = None):
        """
        Initialize KeywordGenerator.

        Args:
            llm_client: Optional LLM client
        """
        self.llm = llm_client
        self.logger = logger.bind(module="keyword_generator")

    async def generate(
        self,
        category: str = "demand",
        count: int = 20,
    ) -> list[Keyword]:
        """
        Generate keywords for a category.

        Args:
            category: Keyword category
            count: Number of keywords to generate

        Returns:
            List of Keyword objects
        """
        prompt = self.CATEGORY_PROMPTS.get(category, self.CATEGORY_PROMPTS["demand"])
        prompt += f"\n\n请生成{count}个关键词，每行一个："

        if not self.llm:
            return self._generate_fallback(category, count)

        try:
            response = await self.llm.generate(prompt, model="gpt-4o")

            keywords = []
            for line in response.split("\n"):
                line = line.strip()
                if line and len(line) > 1:
                    line = line.lstrip("0123456789.-*、 ")
                    keywords.append(
                        Keyword(
                            text=line,
                            type=KeywordType(category),
                            status=KeywordStatus.PENDING,
                        )
                    )

            self.logger.info(
                "keywords_generated",
                category=category,
                count=len(keywords),
            )

            return keywords[:count]

        except Exception as e:
            self.logger.error("keyword_generation_error", error=str(e))
            return self._generate_fallback(category, count)

    def _generate_fallback(
        self,
        category: str,
        count: int,
    ) -> list[Keyword]:
        """Fallback keyword generation without LLM."""
        fallback_keywords = {
            "demand": ["VPN推荐", "翻墙工具", "机场服务", "稳定节点", "高速梯子"],
            "inquiry": ["怎么翻墙", "节点怎么用", "支持哪些设备", "速度如何", "有试用吗"],
            "price": ["VPN价格", "机场多少钱", "月费多少", "有优惠吗", "新人折扣"],
            "competitor": ["XX机场", "XXVPN", "XX加速器", "XX梯子", "XX节点"],
        }

        texts = fallback_keywords.get(category, fallback_keywords["demand"])
        return [
            Keyword(
                text=text,
                type=KeywordType(category),
                status=KeywordStatus.PENDING,
            )
            for text in texts[:count]
        ]

    async def generate_all(
        self,
        counts: dict[str, int] = None,
    ) -> dict[str, list[Keyword]]:
        """
        Generate keywords for all categories.

        Args:
            counts: Dict mapping category to count

        Returns:
            Dict mapping category to list of keywords
        """
        import asyncio

        if counts is None:
            counts = {
                "demand": 20,
                "inquiry": 15,
                "price": 10,
                "competitor": 15,
            }

        tasks = {
            category: self.generate(category, count)
            for category, count in counts.items()
        }

        results = await asyncio.gather(*tasks.values())

        return dict(zip(tasks.keys(), results))

    async def improve_keywords(
        self,
        keywords: list[str],
        feedback: str,
    ) -> list[str]:
        """
        Improve keywords based on feedback.

        Args:
            keywords: Existing keywords
            feedback: Feedback on what to improve

        Returns:
            Improved keywords
        """
        prompt = f"""基于以下关键词和反馈，生成改进后的关键词列表。

现有关键词：
{chr(10).join(keywords)}

反馈：{feedback}

请生成改进后的关键词，每行一个："""

        if not self.llm:
            return keywords

        try:
            response = await self.llm.generate(prompt)
            improved = [
                line.strip()
                for line in response.split("\n")
                if line.strip()
            ]
            return improved[:len(keywords)]

        except Exception as e:
            self.logger.error("keyword_improvement_error", error=str(e))
            return keywords
