"""关键词管理器 - 支持动态增删改查"""
from typing import List, Dict, Optional
from datetime import datetime
from dataclasses import dataclass, field
import json

from loguru import logger


@dataclass
class Keyword:
    """关键词"""
    type: str  # target / trigger / exclude
    keyword: str
    created_at: datetime = field(default_factory=datetime.now)
    created_by: Optional[int] = None
    is_active: bool = True


class KeywordManager:
    """动态关键词管理器"""

    # 默认关键词
    DEFAULT_KEYWORDS = {
        "target": [
            "翻墙", "机场", "VPN", "节点", "梯子",
            "代理", "科学上网", "加速器", "翻墙工具",
            "节点推荐", "机场推荐", "VPS"
        ],
        "trigger": [
            "哪里买", "怎么购买", "多少钱", "价格", "套餐",
            "怎么用", "如何使用", "教程", "怎么连接", "使用说明",
            "试用", "免费", "优惠", "折扣", "促销",
            "能用吗", "稳吗", "速度快吗", "有推荐吗",
            "哪个好", "怎么选", "下载地址"
        ],
        "exclude": [
            "菠菜", "赌博", "色情", "诈骗", "博彩",
            "黑彩", "跑分", "洗钱", "黑产"
        ]
    }

    def __init__(self, redis_client=None):
        self.redis = redis_client
        self._keywords: Dict[str, List[Keyword]] = {
            "target": [],
            "trigger": [],
            "exclude": []
        }

    async def initialize(self):
        """从数据库/Redis加载关键词"""
        if self.redis:
            # 从 Redis 加载
            for keyword_type in ["target", "trigger", "exclude"]:
                key = f"keywords:{keyword_type}"
                data = await self.redis.get(key)
                if data:
                    try:
                        keywords_list = json.loads(data)
                        for kw in keywords_list:
                            self._keywords[keyword_type].append(
                                Keyword(type=keyword_type, keyword=kw)
                            )
                    except Exception as e:
                        logger.error(f"加载关键词失败: {e}")
        else:
            # 使用默认关键词
            for keyword_type, keywords in self.DEFAULT_KEYWORDS.items():
                for kw in keywords:
                    self._keywords[keyword_type].append(
                        Keyword(type=keyword_type, keyword=kw)
                    )

        logger.info(f"关键词加载完成: {self.count()} 个")

    async def _save_to_storage(self, keyword_type: str):
        """保存到存储"""
        if self.redis:
            key = f"keywords:{keyword_type}"
            keywords = [kw.keyword for kw in self._keywords[keyword_type]]
            await self.redis.set(key, json.dumps(keywords))

    def add(self, keyword_type: str, keyword: str, created_by: int = None) -> bool:
        """添加关键词"""
        if keyword_type not in self._keywords:
            logger.error(f"无效的关键词类型: {keyword_type}")
            return False

        # 检查是否已存在
        keyword = keyword.strip()
        for kw in self._keywords[keyword_type]:
            if kw.keyword == keyword and kw.is_active:
                logger.warning(f"关键词已存在: {keyword}")
                return False

        # 添加
        new_keyword = Keyword(
            type=keyword_type,
            keyword=keyword,
            created_by=created_by
        )
        self._keywords[keyword_type].append(new_keyword)

        # 异步保存
        import asyncio
        asyncio.create_task(self._save_to_storage(keyword_type))

        logger.info(f"添加关键词: [{keyword_type}] {keyword}")
        return True

    def bulk_add(self, keyword_type: str, keywords: List[str], created_by: int = None) -> Dict:
        """批量添加关键词"""
        success = 0
        failed = 0
        existed = 0

        for kw in keywords:
            kw = kw.strip()
            if not kw:
                continue
            if self.add(keyword_type, kw, created_by):
                success += 1
            else:
                # 检查是否已存在
                existed += 1

        return {
            "success": success,
            "failed": failed,
            "existed": existed
        }

    def remove(self, keyword_type: str, keyword: str) -> bool:
        """删除关键词（软删除）"""
        if keyword_type not in self._keywords:
            return False

        for kw in self._keywords[keyword_type]:
            if kw.keyword == keyword:
                kw.is_active = False
                import asyncio
                asyncio.create_task(self._save_to_storage(keyword_type))
                logger.info(f"删除关键词: [{keyword_type}] {keyword}")
                return True

        return False

    def list(self, keyword_type: str = None, active_only: bool = True) -> List[Keyword]:
        """获取关键词列表"""
        if keyword_type:
            keywords = self._keywords.get(keyword_type, [])
        else:
            keywords = []
            for kws in self._keywords.values():
                keywords.extend(kws)

        if active_only:
            keywords = [kw for kw in keywords if kw.is_active]

        return keywords

    def list_keywords(self, keyword_type: str = None) -> List[str]:
        """获取关键词字符串列表"""
        keywords = self.list(keyword_type)
        return [kw.keyword for kw in keywords]

    def get_target_keywords(self) -> List[str]:
        """获取目标关键词"""
        return self.list_keywords("target")

    def get_trigger_keywords(self) -> List[str]:
        """获取触发关键词"""
        return self.list_keywords("trigger")

    def get_exclude_keywords(self) -> List[str]:
        """获取排除关键词"""
        return self.list_keywords("exclude")

    def match(self, text: str) -> Dict[str, List[str]]:
        """匹配文本中的关键词"""
        text_lower = text.lower()
        result = {
            "target": [],
            "trigger": [],
            "exclude": []
        }

        for keyword_type in ["target", "trigger", "exclude"]:
            for kw in self.list(keyword_type):
                if kw.keyword.lower() in text_lower:
                    result[keyword_type].append(kw.keyword)

        return result

    def should_exclude(self, text: str) -> bool:
        """检查是否应该排除"""
        matches = self.match(text)
        return len(matches["exclude"]) > 0

    def count(self) -> int:
        """统计关键词总数"""
        return sum(len(kws) for kws in self._keywords.values())

    def export(self) -> str:
        """导出为 JSON"""
        data = {}
        for keyword_type, keywords in self._keywords.items():
            data[keyword_type] = [kw.keyword for kw in keywords if kw.is_active]
        return json.dumps(data, ensure_ascii=False, indent=2)

    def import_from(self, json_str: str) -> Dict:
        """从 JSON 导入"""
        try:
            data = json.loads(json_str)
            results = {
                "target": {"added": 0, "failed": 0},
                "trigger": {"added": 0, "failed": 0},
                "exclude": {"added": 0, "failed": 0}
            }

            for keyword_type in ["target", "trigger", "exclude"]:
                if keyword_type in data:
                    result = self.bulk_add(keyword_type, data[keyword_type])
                    results[keyword_type] = result

            return results

        except Exception as e:
            logger.error(f"导入关键词失败: {e}")
            return {"error": str(e)}
