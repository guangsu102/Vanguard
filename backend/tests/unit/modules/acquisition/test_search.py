"""
Tests for Search Module
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

from app.modules.acquisition.search.group_finder import GroupFinder, DiscoveredGroup
from app.modules.acquisition.search.filters import GroupFilter, GroupFilterCriteria
from app.modules.acquisition.search.searcher import Searcher, SearchCampaign, SearchResult


class TestDiscoveredGroup:
    """Tests for DiscoveredGroup dataclass."""

    def test_create_discovered_group(self):
        """Test creating a DiscoveredGroup instance."""
        group = DiscoveredGroup(
            group_id=123456789,
            title="Test Group",
            username="testgroup",
            member_count=500,
            is_private=False,
            source_keyword="vpn",
        )

        assert group.group_id == 123456789
        assert group.title == "Test Group"
        assert group.username == "testgroup"
        assert group.member_count == 500
        assert group.is_private is False
        assert group.source_keyword == "vpn"

    def test_discovered_group_defaults(self):
        """Test DiscoveredGroup with default values."""
        group = DiscoveredGroup(
            group_id=123,
            title="Test",
            username=None,
            member_count=0,
            is_private=True,
        )

        assert group.source_keyword is None


class TestGroupFilter:
    """Tests for GroupFilter."""

    def setup_method(self):
        """Set up test fixtures."""
        self.filter = GroupFilter()

    def test_should_join_pass(self):
        """Test should_join passes valid groups."""
        group = DiscoveredGroup(
            group_id=123,
            title="VPN Users",
            username="vpngroup",
            member_count=500,
            is_private=False,
        )

        should_join, reason = self.filter.should_join(group)
        assert should_join is True
        assert "通过" in reason

    def test_should_join_member_too_few(self):
        """Test should_join rejects groups with too few members."""
        group = DiscoveredGroup(
            group_id=123,
            title="Small Group",
            username="small",
            member_count=50,  # Below default min of 100
            is_private=False,
        )

        should_join, reason = self.filter.should_join(group)
        assert should_join is False
        assert "成员数不足" in reason

    def test_should_join_member_too_many(self):
        """Test should_join rejects groups with too many members."""
        group = DiscoveredGroup(
            group_id=123,
            title="Huge Group",
            username="huge",
            member_count=100000,  # Above default max of 50000
            is_private=False,
        )

        should_join, reason = self.filter.should_join(group)
        assert should_join is False
        assert "成员数过多" in reason

    def test_should_join_private_excluded(self):
        """Test should_join excludes private groups when configured."""
        self.filter.criteria.exclude_private = True

        group = DiscoveredGroup(
            group_id=123,
            title="Private Group",
            username=None,
            member_count=500,
            is_private=True,
        )

        should_join, reason = self.filter.should_join(group)
        assert should_join is False
        assert "私密群组" in reason

    def test_should_join_blacklist_keyword(self):
        """Test should_join rejects groups with blacklisted keywords."""
        self.filter.criteria.keywords_blacklist = ["竞品", "广告"]

        group = DiscoveredGroup(
            group_id=123,
            title="竞品推广群",
            username="comp",
            member_count=500,
            is_private=False,
        )

        should_join, reason = self.filter.should_join(group)
        assert should_join is False
        assert "黑名单关键词" in reason

    def test_score_group(self):
        """Test group scoring."""
        group = DiscoveredGroup(
            group_id=123,
            title="Test Group",
            username="test",
            member_count=1000,
            is_private=False,
        )

        score = self.filter.score_group(group)
        assert 0 <= score <= 100
        assert score > 50  # Should have some bonus for having username

    def test_score_group_premium_for_username(self):
        """Test that groups with username get higher scores."""
        group_with_username = DiscoveredGroup(
            group_id=1,
            title="Group A",
            username="groupa",
            member_count=1000,
            is_private=False,
        )

        group_without_username = DiscoveredGroup(
            group_id=2,
            title="Group B",
            username=None,
            member_count=1000,
            is_private=False,
        )

        score_with = self.filter.score_group(group_with_username)
        score_without = self.filter.score_group(group_without_username)

        assert score_with > score_without

    def test_filter_groups(self):
        """Test filtering and scoring multiple groups."""
        groups = [
            DiscoveredGroup(123, "Group 1", "g1", 1000, False),
            DiscoveredGroup(124, "Group 2", "g2", 500, False),
            DiscoveredGroup(125, "Private", None, 500, True),  # Will be excluded
            DiscoveredGroup(126, "Too Small", "small", 50, False),  # Will be excluded
        ]

        # Set criteria to exclude private
        self.filter.criteria.exclude_private = True
        self.filter.criteria.min_members = 100

        results = self.filter.filter_groups(groups)

        # Should pass 2 groups
        assert len(results) == 2

        # Should be sorted by score
        assert results[0][1] >= results[1][1]

    def test_update_criteria(self):
        """Test updating filter criteria."""
        self.filter.update_criteria(min_members=200, max_members=10000)

        assert self.filter.criteria.min_members == 200
        assert self.filter.criteria.max_members == 10000


class TestSearchCampaign:
    """Tests for SearchCampaign."""

    def test_create_search_campaign(self):
        """Test creating a SearchCampaign."""
        campaign = SearchCampaign(
            keywords=["vpn", "机场"],
            target_groups=[123, 456],
            max_results_per_keyword=10,
            campaign_name="test_campaign",
        )

        assert campaign.keywords == ["vpn", "机场"]
        assert campaign.target_groups == [123, 456]
        assert campaign.max_results_per_keyword == 10
        assert campaign.campaign_name == "test_campaign"

    def test_search_campaign_defaults(self):
        """Test SearchCampaign default values."""
        campaign = SearchCampaign()

        assert campaign.keywords == []
        assert campaign.target_groups == []
        assert campaign.max_results_per_keyword == 20
        assert campaign.filter_criteria is None


class TestSearchResult:
    """Tests for SearchResult."""

    def test_create_search_result(self):
        """Test creating a SearchResult."""
        result = SearchResult(
            total_found=100,
            total_passed=50,
            groups_added=30,
            groups_failed=5,
        )

        assert result.total_found == 100
        assert result.total_passed == 50
        assert result.groups_added == 30
        assert result.groups_failed == 5
        assert result.started_at is not None
        assert result.completed_at is None

    def test_search_result_completion(self):
        """Test SearchResult completion tracking."""
        result = SearchResult()
        result.completed_at = datetime.utcnow()

        assert result.completed_at is not None
