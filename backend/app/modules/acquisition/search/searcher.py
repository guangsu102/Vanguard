"""
Searcher Module

Search task scheduler and execution for group acquisition.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import asyncio

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account.pool import AccountPool
from app.core.keyword.engine import KeywordEngine
from app.core.group.manager import GroupManager
from app.modules.acquisition.search.group_finder import GroupFinder, DiscoveredGroup
from app.modules.acquisition.search.filters import GroupFilter, GroupFilterCriteria
from app.modules.acquisition.config import AcquisitionConfig
from app.modules.acquisition.models import GroupSearchRecord

logger = structlog.get_logger()


@dataclass
class SearchCampaign:
    """Search campaign configuration."""
    keywords: list[str] = field(default_factory=list)
    target_groups: list[int] = field(default_factory=list)
    max_results_per_keyword: int = 20
    filter_criteria: Optional[GroupFilterCriteria] = None
    campaign_name: Optional[str] = None


@dataclass
class SearchResult:
    """Result of a search operation."""
    total_found: int = 0
    total_passed: int = 0
    groups_added: int = 0
    groups_failed: int = 0
    errors: list[str] = field(default_factory=list)
    started_at: datetime = field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None


class Searcher:
    """
    Search task scheduler and executor.

    Coordinates group search operations across multiple keywords,
    manages search schedules, and handles result persistence.
    """

    def __init__(
        self,
        db: AsyncSession,
        account_pool: AccountPool,
        group_manager: GroupManager,
        keyword_engine: KeywordEngine,
        config: Optional[AcquisitionConfig] = None,
    ):
        """
        Initialize Searcher.

        Args:
            db: Database session
            account_pool: Account pool for Telegram API calls
            group_manager: Group manager for storing results
            keyword_engine: Keyword engine for active keywords
            config: Optional configuration override
        """
        self.db = db
        self.account_pool = account_pool
        self.group_manager = group_manager
        self.keyword_engine = keyword_engine
        self.config = config or AcquisitionConfig()
        self.group_finder = GroupFinder(account_pool)
        self.group_filter = GroupFilter(self.config.search)
        self.logger = logger.bind(module="searcher")
        self._search_lock = asyncio.Lock()

    async def run_campaign(self, campaign: SearchCampaign) -> SearchResult:
        """
        Execute a search campaign.

        Args:
            campaign: Search campaign configuration

        Returns:
            SearchResult with statistics
        """
        self.logger.info("campaign_started", campaign=campaign.campaign_name or "unnamed")

        result = SearchResult()

        async with self._search_lock:
            for keyword in campaign.keywords:
                try:
                    keyword_result = await self._search_keyword(keyword, campaign)
                    result.total_found += keyword_result["found"]
                    result.total_passed += keyword_result["passed"]
                    result.groups_added += keyword_result["added"]
                except Exception as e:
                    self.logger.error("keyword_search_failed", keyword=keyword, error=str(e))
                    result.errors.append(f"Keyword '{keyword}': {str(e)}")

        result.completed_at = datetime.utcnow()
        self.logger.info(
            "campaign_completed",
            total_found=result.total_found,
            total_passed=result.total_passed,
            groups_added=result.groups_added,
        )
        return result

    async def _search_keyword(
        self,
        keyword: str,
        campaign: SearchCampaign,
    ) -> dict:
        """
        Search for a single keyword.

        Args:
            keyword: Search keyword
            campaign: Parent campaign config

        Returns:
            Dictionary with keyword search results
        """
        self.logger.debug("searching_keyword", keyword=keyword)

        # 搜索群组
        discovered = await self.group_finder.search_by_keyword(
            keyword=keyword,
            limit=campaign.max_results_per_keyword or self.config.search.max_results_per_keyword,
        )

        # 记录搜索
        await self._record_search(keyword, discovered)

        # 过滤群组
        filter_criteria = campaign.filter_criteria or self.config.search
        if isinstance(filter_criteria, GroupFilterCriteria):
            self.group_filter.criteria = filter_criteria
        else:
            self.group_filter.criteria = GroupFilterCriteria(
                min_members=filter_criteria.min_group_members,
                max_members=filter_criteria.max_group_members,
            )

        filtered_groups = self.group_filter.filter_groups(discovered)

        result = {
            "found": len(discovered),
            "passed": len(filtered_groups),
            "added": 0,
        }

        # 添加到群组管理
        for group, score in filtered_groups:
            try:
                await self._add_group(group, keyword)
                result["added"] += 1
            except Exception as e:
                self.logger.error("add_group_failed", group_id=group.group_id, error=str(e))

        return result

    async def _record_search(
        self,
        keyword: str,
        groups: list[DiscoveredGroup],
    ) -> None:
        """
        Record search results to database.

        Args:
            keyword: Search keyword
            groups: Discovered groups
        """
        for group in groups:
            record = GroupSearchRecord(
                keyword=keyword,
                group_id=group.group_id,
                group_title=group.title,
                member_count=group.member_count,
            )
            self.db.add(record)

        await self.db.commit()

    async def _add_group(
        self,
        group: DiscoveredGroup,
        source_keyword: Optional[str] = None,
    ) -> None:
        """
        Add a group to the group manager.

        Args:
            group: Discovered group
            source_keyword: Keyword that found this group
        """
        # 检查是否已存在
        existing = await self.group_manager.get_group_by_id(group.group_id)
        if existing:
            self.logger.debug("group_already_exists", group_id=group.group_id)
            return

        # 添加新群组
        await self.group_manager.add_group(
            group_id=group.group_id,
            title=group.title,
            username=group.username,
            member_count=group.member_count,
            source_keyword=source_keyword,
            discovery_source="keyword_search",
        )
        self.logger.info("group_added", group_id=group.group_id, title=group.title)

    async def get_auto_join_targets(self) -> list[DiscoveredGroup]:
        """
        Get groups that should be auto-joined.

        Returns:
            List of groups to join
        """
        # 获取所有活跃关键词
        keywords = await self.keyword_engine.list_keywords(limit=100)
        keyword_texts = [kw.text for kw in keywords]

        # 搜索所有关键词
        all_groups = []
        for keyword in keyword_texts:
            groups = await self.group_finder.search_by_keyword(keyword, limit=10)
            all_groups.extend(groups)

        # 去重
        seen_ids = set()
        unique_groups = []
        for group in all_groups:
            if group.group_id not in seen_ids:
                seen_ids.add(group.group_id)
                unique_groups.append(group)

        # 过滤
        filtered = self.group_filter.filter_groups(unique_groups)

        # 只返回前N个
        return [g for g, _ in filtered[:10]]

    async def search_and_join(self, keywords: Optional[list[str]] = None) -> SearchResult:
        """
        Search for groups and auto-join qualified ones.

        Args:
            keywords: Optional keyword list, uses all active keywords if not provided

        Returns:
            SearchResult with statistics
        """
        if keywords is None:
            kw_list = await self.keyword_engine.list_keywords(limit=100)
            keywords = [kw.text for kw in kw_list]

        campaign = SearchCampaign(
            keywords=keywords,
            max_results_per_keyword=10,
            campaign_name="auto_join",
        )

        return await self.run_campaign(campaign)
