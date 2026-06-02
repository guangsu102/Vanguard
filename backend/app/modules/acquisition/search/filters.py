"""
Group Filters Module

Filters for evaluating and qualifying Telegram groups.
"""

from dataclasses import dataclass
from typing import Optional

import structlog

from app.modules.acquisition.search.group_finder import DiscoveredGroup

logger = structlog.get_logger()


@dataclass
class GroupFilterCriteria:
    """Criteria for filtering groups."""
    min_members: int = 100
    max_members: int = 50000
    exclude_private: bool = False
    require_username: bool = False
    languages: Optional[list[str]] = None
    keywords_blacklist: Optional[list[str]] = None
    keywords_whitelist: Optional[list[str]] = None


class GroupFilter:
    """
    Group filtering and qualification.

    Evaluates groups against criteria to determine if they
    should be added to the acquisition pool.
    """

    def __init__(self, criteria: Optional[GroupFilterCriteria] = None):
        """
        Initialize GroupFilter.

        Args:
            criteria: Filter criteria, uses defaults if not provided
        """
        self.criteria = criteria or GroupFilterCriteria()
        self.logger = logger.bind(module="group_filter")

    def should_join(self, group: DiscoveredGroup) -> tuple[bool, str]:
        """
        Determine if a group should be joined.

        Args:
            group: Group to evaluate

        Returns:
            Tuple of (should_join, reason)
        """
        # 检查成员数
        if group.member_count < self.criteria.min_members:
            return False, f"成员数不足 ({group.member_count} < {self.criteria.min_members})"

        if group.member_count > self.criteria.max_members:
            return False, f"成员数过多 ({group.member_count} > {self.criteria.max_members})"

        # 检查私密群组
        if self.criteria.exclude_private and group.is_private:
            return False, "私密群组被排除"

        # 检查用户名要求
        if self.criteria.require_username and not group.username:
            return False, "群组没有公开用户名"

        # 检查黑名单关键词
        if self.criteria.keywords_blacklist:
            title_lower = group.title.lower()
            for keyword in self.criteria.keywords_blacklist:
                if keyword.lower() in title_lower:
                    return False, f"标题包含黑名单关键词: {keyword}"

        # 检查白名单关键词（如果设置，必须匹配）
        if self.criteria.keywords_whitelist:
            title_lower = group.title.lower()
            matched = any(kw.lower() in title_lower for kw in self.criteria.keywords_whitelist)
            if not matched:
                return False, "标题不包含任何白名单关键词"

        return True, "通过筛选"

    def score_group(self, group: DiscoveredGroup) -> float:
        """
        Score a group for priority ranking.

        Args:
            group: Group to score

        Returns:
            Score between 0 and 100
        """
        score = 50.0  # 基础分

        # 成员数评分（中间值最优）
        optimal_size = (self.criteria.min_members + self.criteria.max_members) / 2
        member_distance = abs(group.member_count - optimal_size)
        member_range = (self.criteria.max_members - self.criteria.min_members) / 2
        member_score = max(0, 30 - (member_distance / member_range) * 30)
        score += member_score

        # 公开群组加分
        if not group.is_private and group.username:
            score += 10

        # 有用户名加分
        if group.username:
            score += 10

        return min(100, max(0, score))

    def filter_groups(
        self,
        groups: list[DiscoveredGroup],
    ) -> list[tuple[DiscoveredGroup, float]]:
        """
        Filter and score a list of groups.

        Args:
            groups: Groups to filter

        Returns:
            List of (group, score) tuples for groups that passed filter
        """
        results = []

        for group in groups:
            should_join, reason = self.should_join(group)
            if should_join:
                score = self.score_group(group)
                results.append((group, score))
                self.logger.debug("group_passed_filter", group_id=group.group_id, score=score)
            else:
                self.logger.debug("group_filtered_out", group_id=group.group_id, reason=reason)

        # 按分数降序排序
        results.sort(key=lambda x: x[1], reverse=True)
        return results

    def update_criteria(self, **kwargs) -> None:
        """Update filter criteria."""
        for key, value in kwargs.items():
            if hasattr(self.criteria, key):
                setattr(self.criteria, key, value)
                self.logger.info("criteria_updated", key=key, value=value)
