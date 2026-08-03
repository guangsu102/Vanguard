"""
Keyword Generator Module

AI-powered keyword generation for Telegram group marketing.

Features:
- Category-based keyword generation
- Batch generation
- Deduplication
"""

import re

import structlog

from app.core.ai.llm_client import LLMClient
from app.core.keyword.models import Keyword, KeywordStatus, KeywordType

logger = structlog.get_logger()


def normalize_keyword_text(text: str) -> str:
    """Normalize keyword text for stable duplicate checks."""
    return re.sub(r"\s+", " ", text.strip()).casefold()


FORBIDDEN_KEYWORD_TERMS = {
    "vpn",
    "机场",
    "节点",
    "梯子",
    "翻墙",
    "科学上网",
    "clash",
    "v2ray",
    "trojan",
    "ssr",
    "shadowrocket",
    "小火箭",
}

QUESTION_OR_TUTORIAL_TERMS = {
    "怎么",
    "如何",
    "教程",
    "多少钱",
    "怎么办",
    "怎么用",
    "使用",
    "连接",
    "设置",
    "过期",
    "支持吗",
    "稳定吗",
}

TOO_GENERIC_SEARCH_TERMS = {
    "日区",
    "美区",
    "港区",
    "支付",
    "变现",
    "测品",
    "账号",
    "广告",
    "投放",
    "获客",
    "引流",
    "运营",
    "海外",
    "美国",
    "日本",
    "韩国",
    "香港",
    "台湾",
    "新加坡",
    "东南亚",
    "欧洲",
    "英国",
    "德国",
    "法国",
    "加拿大",
    "澳洲",
    "加国",
    "中东",
    "拉美",
    "巴西",
    "印度",
    "越南",
    "泰国",
    "印尼",
    "台区",
    "奈飞",
    "美服",
    "Steam",
    "steam",
    "询单",
    "自动化",
    "爆品",
    "素材库",
    "代运营",
    "代投",
    "开户",
    "跑量",
    "封号",
    "起号",
    "社媒",
    "联盟营销",
    "支付群",
    "变现群",
    "获客群",
    "引流群",
    "投放群",
    "账号群",
    "养号群",
    "封号群",
    "风控群",
}

CJK_GROUP_HINT_SUFFIXES = {
    "群",
    "圈",
    "社群",
    "交流",
}

LOW_CONFIDENCE_SEARCH_SUFFIXES = {
    "会",
    "社",
    "帮",
    "局",
    "营",
    "课",
    "服",
    "号",
}

CJK_STANDALONE_ALLOWED_TERMS = {
    "跨境电商",
    "独立站",
    "亚马逊",
    "速卖通",
    "供应链",
}

ASCII_STANDALONE_ALLOWED_TERMS = {
    "amazon",
    "shopify",
    "tiktok",
    "youtube",
    "instagram",
    "facebook",
    "chatgpt",
    "openai",
    "claude",
    "discord",
    "stripe",
    "paypal",
    "shopee",
    "temu",
    "etsy",
    "ebay",
    "googleads",
    "metaads",
}

CJK_RE = re.compile(r"^[\u4e00-\u9fff]+$")
HAS_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
SEARCH_KEYWORD_CHARS_RE = re.compile(r"^[A-Za-z0-9\u4e00-\u9fff._+-]+$")


def keyword_length_limit(text: str) -> int:
    """Return max length for Chinese-only vs mixed/ASCII search seeds."""
    compact = re.sub(r"\s+", "", text.strip())
    return 4 if CJK_RE.fullmatch(compact) else 16


def validate_search_keyword_text(text: str) -> tuple[bool, str | None]:
    """Validate whether a generated keyword is suitable for Telegram group search."""
    compact = re.sub(r"\s+", "", text.strip())
    if not compact:
        return False, "empty"
    if len(compact) > keyword_length_limit(compact):
        return False, "too_long"
    if len(compact) < 2:
        return False, "too_short"
    if not SEARCH_KEYWORD_CHARS_RE.fullmatch(compact):
        return False, "invalid_chars"

    normalized = normalize_keyword_text(compact)
    if any(term in normalized for term in FORBIDDEN_KEYWORD_TERMS):
        return False, "forbidden_domain"
    if any(term in compact for term in QUESTION_OR_TUTORIAL_TERMS):
        return False, "question_or_tutorial"
    if "?" in compact or "？" in compact:
        return False, "question_or_tutorial"
    if compact in TOO_GENERIC_SEARCH_TERMS:
        return False, "too_generic"
    if CJK_RE.fullmatch(compact) and compact.endswith(tuple(LOW_CONFIDENCE_SEARCH_SUFFIXES)):
        return False, "low_confidence_suffix"
    return True, None


class KeywordGenerator:
    """
    AI-powered keyword generator.

    Generates keywords for Telegram group marketing using LLM.
    """

    CATEGORY_PROMPTS = {
        "demand": """生成 Telegram 公开群搜索用的“真实群名短片段”。

目标：发现全行业中文用户社区，而不是寻找VPN/机场同行群。

方向示例：华人群、留学群、招聘群、兼职群、资源群、互助群、外贸群、卖家群、货代群、工厂群、餐饮群、美妆群、房产群、汽配群。

硬性要求：
1. 只输出搜索词，每行一个
2. 纯中文不超过4个字
3. 英文或中英文混合不超过16位
4. 覆盖尽可能多的行业，不要局限电商或VPN领域
5. 禁止出现 VPN、机场、节点、梯子、翻墙、科学上网
6. 禁止问句、教程、客服咨询句
7. 可以生成行业、人群、职业、生活服务词
8. 禁止生成“学习会、兴趣社、交流帮、资源帮、同城社”这类低命中组合
9. 优先生成 Telegram 公开群更常见的词：华人群、留学群、招聘群、兼职群、资源群、互助群、租房群、二手群""",

        "inquiry": """生成 Telegram 公开群搜索用的“平台社群短片段”。

目标：发现围绕平台、工具、软件、内容创作、技术开发的中文用户社区。

方向示例：AI群、ChatGPT、GPT群、Python群、程序员、前端群、设计群、剪辑群、Shopify、Amazon、TikTok、PayPal。

硬性要求：
1. 只输出搜索词，每行一个
2. 纯中文不超过4个字
3. 英文或中英文混合不超过16位
4. 平台名可用英文原名
5. 禁止出现 VPN、机场、节点、梯子、翻墙、科学上网
6. 禁止问句、教程、客服咨询句
7. 优先覆盖公开群搜索常见平台、技能、职业词，不要生成“工具会、设计社、资源帮”这类低命中组合""",

        "price": """生成 Telegram 公开群搜索用的“业务场景/需求群名短片段”。

目标：发现围绕工作、赚钱、学习、资源交换、接单、招聘、交易、生活服务的中文用户社区。

方向示例：兼职群、招聘群、求职群、接单群、资源群、互助群、资料群、租房群、二手群、拼车群、留学群。

硬性要求：
1. 只输出搜索词，每行一个
2. 纯中文不超过4个字
3. 英文或中英文混合不超过16位
4. 词要像真实群名片段，不要像文章标题
5. 禁止出现 VPN、机场、节点、梯子、翻墙、科学上网
6. 禁止问句、教程、客服咨询句
7. 泛需求词必须组合成群名片段，例如“资源群”“接单群”“兼职群”
8. 禁止生成“学习会、互助社、资源帮”这类低命中组合""",

        "competitor": """生成 Telegram 公开群搜索用的“泛社群/人群/地区短片段”。

目标：发现不依赖具体行业的中文活人群，例如同城、兴趣、学习、资源、互助、华人、留学、家长、宝妈、老乡、圈子类社区。

方向示例：华人群、留学群、租房群、二手群、兼职群、招聘群、同城群、资源群、互助群、宝妈群、老乡群。

硬性要求：
1. 只输出搜索词，每行一个
2. 纯中文不超过4个字
3. 英文或中英文混合不超过16位
4. 地区、市场、人群、娱乐生态都可以，但要像群名片段
5. 禁止出现 VPN、机场、节点、梯子、翻墙、科学上网
6. 禁止问句、教程、客服咨询句
7. 必须覆盖没有具体行业属性的群名片段，不要只生成国家/地区词
8. 禁止生成“学习会、兴趣社、闲聊帮、同城社”这类低命中组合""",
    }

    FALLBACK_PARTS = {
        "demand": {
            "seeds": [
                "跨境群",
                "外贸群",
                "出海群",
                "电商群",
                "卖家群",
                "零售群",
                "餐饮群",
                "美食群",
                "茶叶群",
                "酒水群",
                "服装群",
                "鞋包群",
                "美妆群",
                "母婴群",
                "宠物群",
                "家居群",
                "装修群",
                "建材群",
                "房产群",
                "物业群",
                "汽车群",
                "汽配群",
                "汽修群",
                "旅游群",
                "民宿群",
                "酒店群",
                "教育群",
                "家教群",
                "考研群",
                "医疗群",
                "医美群",
                "健身群",
                "瑜伽群",
                "摄影群",
                "设计群",
                "开发群",
                "程序员",
                "产品群",
                "运营群",
                "财税群",
                "会计群",
                "法务群",
                "律师群",
                "保险群",
                "理财群",
                "股票群",
                "招聘群",
                "求职群",
                "工程群",
                "机械群",
                "五金群",
                "化工群",
                "农业群",
                "农产品",
                "渔业群",
                "能源群",
                "新能源",
                "光伏群",
                "储能群",
                "环保群",
                "独立站",
                "亚马逊",
                "速卖通",
                "Temu",
                "Shopee",
                "Shopify",
                "Etsy",
                "eBay",
                "货代群",
                "物流群",
                "仓储群",
                "海运群",
                "报关群",
                "采购群",
                "批发群",
                "供应链",
                "工厂群",
                "SOHO圈",
                "创业群",
                "副业群",
                "接单群",
                "远程群",
            ],
            "bases": [
                "跨境",
                "外贸",
                "出海",
                "电商",
                "卖家",
                "货代",
                "采购",
                "工厂",
                "餐饮",
                "服装",
                "美妆",
                "母婴",
                "宠物",
                "装修",
                "房产",
                "汽车",
                "旅游",
                "教育",
                "医疗",
                "设计",
                "开发",
                "财税",
                "法务",
                "农业",
                "能源",
                "创业",
            ],
            "modifiers": ["群", "圈"],
        },
        "inquiry": {
            "seeds": [
                "AI群",
                "AIGC群",
                "绘图群",
                "建站群",
                "剪辑群",
                "设计群",
                "摄影群",
                "视频群",
                "直播群",
                "短剧群",
                "游戏群",
                "手游群",
                "动漫群",
                "音乐群",
                "电音群",
                "SaaS群",
                "CRM群",
                "ERP群",
                "爬虫群",
                "Python群",
                "前端群",
                "后端群",
                "数据群",
                "量化群",
                "TikTok群",
                "YouTube群",
                "Instagram群",
                "Facebook群",
                "Twitter群",
                "Telegram群",
                "WhatsApp群",
                "Discord群",
                "Reddit群",
                "LinkedIn群",
                "ChatGPT群",
                "Claude群",
                "OpenAI群",
                "Midjourney群",
                "Canva群",
                "Notion群",
                "Shopify",
                "Amazon",
                "GoogleAds",
                "MetaAds",
                "PayPal群",
                "Stripe群",
                "AWS群",
                "Azure群",
                "GitHub群",
                "WordPress",
                "WooCommerce",
                "Figma群",
                "Canva群",
            ],
            "bases": [
                "AI",
                "GPT",
                "绘图",
                "建站",
                "剪辑",
                "设计",
                "直播",
                "短剧",
                "游戏",
                "Python",
                "前端",
                "数据",
                "Shopify",
                "Amazon",
                "PayPal",
            ],
            "modifiers": ["群", "圈", "工具", "交流", "社群"],
        },
        "price": {
            "seeds": [
                "接单群",
                "兼职群",
                "招聘群",
                "求职群",
                "远程群",
                "资料群",
                "资源群",
                "互助群",
                "学习群",
                "分享群",
                "活动群",
                "拼单群",
                "二手群",
                "租房群",
                "家长群",
                "宝妈群",
                "老板群",
                "校友群",
                "老乡群",
                "获客群",
                "引流群",
                "投放群",
                "广告圈",
                "账号群",
                "矩阵群",
                "养号群",
                "封号群",
                "风控群",
                "素材群",
                "直播群",
                "短剧群",
                "变现群",
                "建站群",
                "收款群",
                "支付群",
                "结汇群",
                "店群",
                "铺货群",
                "选品群",
                "测评群",
                "私域群",
                "社群",
                "裂变群",
                "增长群",
            ],
            "bases": [
                "接单",
                "兼职",
                "招聘",
                "求职",
                "资源",
                "互助",
                "学习",
                "分享",
                "活动",
                "拼单",
                "租房",
                "获客",
                "引流",
                "投放",
                "素材",
                "建站",
                "收款",
                "选品",
                "私域",
                "增长",
            ],
            "modifiers": ["群", "圈"],
        },
        "competitor": {
            "seeds": [
                "交流群",
                "同城群",
                "资源群",
                "互助群",
                "闲聊群",
                "兴趣群",
                "学习群",
                "分享群",
                "资料群",
                "活动群",
                "搭子群",
                "拼单群",
                "宝妈群",
                "家长群",
                "校友群",
                "老乡群",
                "华人群",
                "留学群",
                "移民群",
                "租房群",
                "二手群",
                "交友群",
                "圈子",
                "社群",
                "美国群",
                "日本群",
                "韩国群",
                "香港群",
                "台湾群",
                "新加坡群",
                "东南亚群",
                "欧洲群",
                "英国群",
                "德国群",
                "法国群",
                "加拿大群",
                "澳洲群",
                "中东群",
                "拉美群",
                "巴西群",
                "印度群",
                "越南群",
                "泰国群",
                "印尼群",
                "海外群",
                "华人群",
                "留学群",
                "移民群",
                "奈飞群",
                "Steam群",
                "美服群",
            ],
            "bases": [
                "交流",
                "同城",
                "资源",
                "互助",
                "闲聊",
                "兴趣",
                "学习",
                "分享",
                "搭子",
                "拼单",
                "宝妈",
                "家长",
                "校友",
                "老乡",
                "华人",
                "留学",
                "移民",
                "租房",
                "二手",
                "美国",
                "日本",
                "香港",
                "海外",
                "Steam",
            ],
            "modifiers": ["群", "圈"],
        },
    }

    def __init__(self, llm_client: LLMClient | None = None):
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
        avoid_keywords: list[str] | None = None,
        learning_hints: dict[str, list[str]] | None = None,
    ) -> list[Keyword]:
        """
        Generate keywords for a category.

        Args:
            category: Keyword category
            count: Number of keywords to generate
            avoid_keywords: Existing keywords that must not be generated
            learning_hints: Historical high/low performance examples for better generation

        Returns:
            List of Keyword objects
        """
        category = self._normalize_category(category)
        avoid_set = {
            normalized
            for keyword in avoid_keywords or []
            if (normalized := normalize_keyword_text(keyword))
        }
        prompt = self.CATEGORY_PROMPTS.get(category, self.CATEGORY_PROMPTS["demand"])
        if avoid_keywords:
            avoid_lines = "\n".join(f"- {keyword}" for keyword in list(avoid_keywords)[:200])
            prompt += f"""

数据库中已存在以下搜群关键词，禁止重复生成这些词，也不要生成仅大小写、空格或标点不同的近似重复：
{avoid_lines}
"""
        positive_keywords = (learning_hints or {}).get("positive_keywords") or []
        negative_keywords = (learning_hints or {}).get("negative_keywords") or []
        if positive_keywords:
            positive_lines = "\n".join(f"- {keyword}" for keyword in positive_keywords[:80])
            prompt += f"""

历史高命中/可入群样本如下。请学习这些词的群名结构、人群粒度和常见后缀，只生成相似命中逻辑的新词，不要重复原词：
{positive_lines}
"""
        if negative_keywords:
            negative_lines = "\n".join(f"- {keyword}" for keyword in negative_keywords[:120])
            prompt += f"""

历史低命中/无候选样本如下。请避开这些词及其相同构词套路，不要继续生成同类低命中词：
{negative_lines}
"""
        prompt += f"\n\n请生成{count}个全新的关键词，每行一个，只输出关键词："

        if not self.llm:
            return self._generate_fallback(category, count, avoid_set)

        try:
            response = await self.llm.generate(prompt)
            keywords = self._parse_generated_text(response, category, avoid_set)
            if not keywords:
                return self._generate_fallback(category, count, avoid_set)

            self.logger.info(
                "keywords_generated",
                category=category,
                count=len(keywords),
            )

            return keywords[:count]

        except Exception as e:
            self.logger.error("keyword_generation_error", error=str(e))
            return self._generate_fallback(category, count, avoid_set)

    def _generate_fallback(
        self,
        category: str,
        count: int,
        avoid_set: set[str] | None = None,
    ) -> list[Keyword]:
        """Fallback keyword generation without LLM."""
        avoid_set = avoid_set or set()
        candidates = [
            text
            for text in self._fallback_candidates(category)
            if normalize_keyword_text(text) not in avoid_set
            and validate_search_keyword_text(text)[0]
        ]
        return [
            Keyword(
                text=text,
                type=KeywordType(category),
                status=KeywordStatus.PENDING,
            )
            for text in candidates[:count]
        ]

    def _normalize_category(self, category: str) -> str:
        """Normalize unknown categories to demand so fallback generation is safe."""
        try:
            return KeywordType(category).value
        except ValueError:
            return KeywordType.DEMAND.value

    def _parse_generated_text(
        self,
        response: str,
        category: str,
        avoid_set: set[str] | None = None,
    ) -> list[Keyword]:
        """Parse a line-oriented LLM response into unique keyword models."""
        lower_response = response.lower()
        if "api not configured" in lower_response or "please set api key" in lower_response:
            return []

        keyword_type = KeywordType(category)
        avoid_set = avoid_set or set()
        seen: set[str] = set()
        keywords: list[Keyword] = []

        for line in response.splitlines():
            text = self._clean_generated_line(line)
            if not text:
                continue
            is_valid, _reason = validate_search_keyword_text(text)
            if not is_valid:
                continue
            normalized = normalize_keyword_text(text)
            if normalized in avoid_set or normalized in seen:
                continue
            if text.startswith(("以下", "这里", "关键词")) and len(text) > 12:
                continue
            seen.add(normalized)
            keywords.append(
                Keyword(
                    text=text,
                    type=keyword_type,
                    status=KeywordStatus.PENDING,
                )
            )

        return keywords

    def _clean_generated_line(self, line: str) -> str:
        """Extract a keyword from a model line and drop surrounding explanation."""
        text = line.strip()
        text = text.lstrip("0123456789.-*、)）(（ ")
        text = text.strip("`'\"“”‘’，,;； ")
        text = re.split(r"\s*[：:，,；;|]\s*", text, maxsplit=1)[0]
        text = re.split(r"\s+[-–—]\s+", text, maxsplit=1)[0]
        text = re.sub(r"\s+", "", text)
        return text.strip()

    def _fallback_candidates(self, category: str) -> list[str]:
        """Build enough deterministic fallback keywords for the requested category."""
        parts = self.FALLBACK_PARTS.get(category, self.FALLBACK_PARTS["demand"])
        candidates: list[str] = []
        seen: set[str] = set()

        def add(text: str) -> None:
            normalized = text.strip()
            normalized_key = normalize_keyword_text(normalized)
            if normalized and normalized_key not in seen:
                seen.add(normalized_key)
                candidates.append(normalized)

        for text in parts["seeds"]:
            add(text)
        for base in parts["bases"]:
            for modifier in parts["modifiers"]:
                add(f"{base}{modifier}")

        return candidates

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

        return dict(zip(tasks.keys(), results, strict=True))

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
