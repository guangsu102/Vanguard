"""
Tests for Search Module
"""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

import app.modules.acquisition.automation as acquisition_automation
from app.core.account.models import (
    AccountAssetTier,
    AccountOperationConfig,
    AccountStatus,
    AccountType,
    TelegramAccount,
)
from app.core.account.telegram_execution import TelegramExecutionService
from app.core.ai.keyword_generator import KeywordGenerator
from app.core.group.models import Group, GroupAccountMembership, GroupLevel, GroupLevelConfig
from app.core.keyword.models import KeywordType
from app.core.runtime_settings import DEFAULT_ACCOUNT_ASSET_POLICY_SETTINGS
from app.modules.acquisition.automation import (
    AcquisitionAutomationService,
    JoinVerificationDecision,
    JoinVerificationSettings,
)
from app.modules.acquisition.dynamic_frequency import AccountDynamicFrequencyService
from app.modules.acquisition.models import (
    AccountAdBinding,
    AdCampaign,
    AdCreative,
    AdDeliveryLog,
    AdSendMode,
    AutoJoinAttempt,
    DeliveryStatus,
    GroupAdPolicyMode,
    GroupAdProfile,
    GroupAdTier,
    GroupSearchKeyword,
    GroupSearchRecord,
    SearchKeywordSource,
    SearchKeywordStatus,
)
from app.modules.acquisition.search.filters import GroupFilter
from app.modules.acquisition.search.group_finder import DiscoveredGroup, GroupFinder
from app.modules.acquisition.search.searcher import SearchCampaign, SearchResult


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
            member_count=49,  # Below default min of 50
            is_private=False,
        )

        should_join, reason = self.filter.should_join(group)
        assert should_join is False
        assert "成员数不足" in reason

    def test_should_join_member_minimum_boundary(self):
        """Test should_join accepts groups at the minimum member threshold."""
        group = DiscoveredGroup(
            group_id=123,
            title="Small Qualified Group",
            username="small_ok",
            member_count=50,
            is_private=False,
        )

        should_join, reason = self.filter.should_join(group)
        assert should_join is True
        assert "通过" in reason

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


class TestGroupFinder:
    """Tests for GroupFinder API fallbacks."""

    @pytest.mark.asyncio
    async def test_get_dialogs_fallback_filters_without_keyword_argument(self):
        """Telethon get_dialogs accepts limit, not keyword plus limit."""

        class FakeClient:
            def __init__(self):
                self.calls = []

            async def get_dialogs(self, limit=None):
                self.calls.append(limit)
                return [
                    SimpleNamespace(title="Etsy Sellers Hub", username="etsyhub", id=1),
                    SimpleNamespace(title="General Chat", username="general", id=2),
                ]

        client = FakeClient()
        finder = GroupFinder(account_pool=MagicMock())

        result = await finder._search_via_api(SimpleNamespace(client=client), "etsy", 2)

        assert client.calls == [20]
        assert len(result) == 1
        assert result[0].title == "Etsy Sellers Hub"

    @pytest.mark.asyncio
    async def test_search_by_keyword_filters_broadcast_channels(self):
        """Auto-join search must keep groups/supergroups and drop channels."""

        class FakeClient:
            async def search_public_groups(self, keyword, limit=20):
                return [
                    SimpleNamespace(
                        id=1,
                        title="Etsy News Channel",
                        username="etsynews",
                        participants_count=5000,
                        broadcast=True,
                        megagroup=False,
                    ),
                    SimpleNamespace(
                        id=2,
                        title="Etsy Sellers Group",
                        username="etsysellers",
                        participants_count=1200,
                        broadcast=False,
                        megagroup=True,
                    ),
                ]

        account = SimpleNamespace(client=FakeClient())
        account_pool = MagicMock()
        account_pool.acquire = AsyncMock(return_value=account)
        account_pool.release = AsyncMock()
        finder = GroupFinder(account_pool=account_pool)

        result = await finder.search_by_keyword("etsy", limit=5)

        assert [group.group_id for group in result] == [2]
        assert result[0].title == "Etsy Sellers Group"

    @pytest.mark.asyncio
    async def test_join_group_rejects_broadcast_channel_before_join(self):
        """Joining has a final guard in case a channel bypasses search filters."""
        pytest.importorskip("telethon")
        calls = []

        class FakeClient:
            async def get_entity(self, target):
                return SimpleNamespace(
                    id=100,
                    title="Etsy News Channel",
                    username=target,
                    participants_count=5000,
                    broadcast=True,
                    megagroup=False,
                )

            async def __call__(self, request):
                calls.append(request)

        account = SimpleNamespace(client=FakeClient())
        account_pool = MagicMock()
        account_pool.acquire_by_id = AsyncMock(return_value=account)
        account_pool.release = AsyncMock()
        service = AcquisitionAutomationService(db=MagicMock(), account_pool=account_pool)
        service.telegram_execution = TelegramExecutionService(None)

        with pytest.raises(RuntimeError, match="only groups"):
            await service._join_group(
                1,
                DiscoveredGroup(
                    group_id=100,
                    title="Etsy News Channel",
                    username="etsynews",
                    member_count=5000,
                    is_private=False,
                ),
            )

        assert calls == []
        account_pool.release.assert_awaited_once_with(account)


class TestAutoJoinAudit:
    def _service_with_client(self, client):
        account = SimpleNamespace(client=client)
        account_pool = MagicMock()
        account_pool.acquire_by_id = AsyncMock(return_value=account)
        account_pool.release = AsyncMock()
        service = AcquisitionAutomationService(db=MagicMock(), account_pool=account_pool)
        service.telegram_execution = TelegramExecutionService(None)
        service._join_verification_settings = AsyncMock(
            return_value=JoinVerificationSettings(ai_enabled=False)
        )
        service.group_manager = SimpleNamespace(
            update_group=AsyncMock(),
            update_scores=AsyncMock(),
        )
        return service, account_pool

    @staticmethod
    def _group():
        return SimpleNamespace(
            id=7,
            group_id=7001,
            username="paypal_money_hub",
            source_keyword="PayPal",
        )

    @pytest.mark.asyncio
    async def test_english_group_title_with_chinese_messages_passes(self):
        client = FakeAuditClient(
            title="PayPal Money: Freelance Hub",
            messages=[
                "最近做独立站和外贸收款的人多吗",
                "PayPal 风控之后大家都怎么处理",
                "有没有适合新手的选品方向",
                "我这边主要做美区店铺",
                "支付通道稳定性最近怎么样",
                "广告账户被限制后怎么申诉",
                "求一个靠谱的建站工具推荐",
                "这个类目现在转化还不错",
                "物流和客服成本要提前算好",
                "有人做 Etsy 手工品吗",
                "我想找同城卖家交流一下",
                "新号前期别太猛发广告",
            ],
        )
        service, _account_pool = self._service_with_client(client)

        audit = await service._evaluate_joined_group(1, self._group())

        assert audit.passed is True
        assert audit.reason is None
        assert audit.language.chinese_message_ratio == 1
        service.group_manager.update_scores.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_non_chinese_messages_are_rejected(self):
        client = FakeAuditClient(
            title="PayPal Money: Freelance Hub",
            messages=[
                "Selling on Etsy is easier with better payment flow",
                "Does anyone know a good freight forwarder",
                "PayPal account limits are common this month",
                "I need a designer for product photos",
                "Amazon sellers can share sourcing tips here",
                "Which marketplace has better conversion now",
                "The latest Shopify theme is pretty fast",
                "Cross border sellers should compare fees",
                "Any advice for payment disputes",
                "Looking for partners in the US market",
                "Freelancers can post gigs here",
                "Keep messages short and useful",
            ],
        )
        service, _account_pool = self._service_with_client(client)

        audit = await service._evaluate_joined_group(1, self._group())

        assert audit.passed is False
        assert audit.reason == "non_chinese_chat"
        service.group_manager.update_scores.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_restricted_group_is_rejected(self):
        client = FakeAuditClient(
            title="Chinese Sellers",
            default_send_blocked=True,
            messages=[
                "最近做独立站和外贸收款的人多吗",
                "PayPal 风控之后大家都怎么处理",
                "有没有适合新手的选品方向",
                "我这边主要做美区店铺",
                "支付通道稳定性最近怎么样",
                "广告账户被限制后怎么申诉",
                "求一个靠谱的建站工具推荐",
                "这个类目现在转化还不错",
                "物流和客服成本要提前算好",
                "有人做 Etsy 手工品吗",
                "我想找同城卖家交流一下",
                "新号前期别太猛发广告",
            ],
        )
        service, _account_pool = self._service_with_client(client)

        audit = await service._evaluate_joined_group(1, self._group())

        assert audit.passed is False
        assert audit.reason == "cannot_send_messages"
        assert audit.permission_reason == "default_send_restricted"
        service.group_manager.update_scores.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_group_announcement_disallows_ads_without_rejecting_join(self):
        client = FakeAuditClient(
            title="Chinese Sellers",
            about="群公告：本群禁止广告推广和任何外链，违者踢出。",
            messages=CHINESE_AUDIT_MESSAGES,
        )
        service, _account_pool = self._service_with_client(client)

        audit = await service._evaluate_joined_group(1, self._group())

        assert audit.passed is True
        assert audit.reason is None
        assert audit.ad_allowed is False
        assert audit.ad_rule_reason == "group_rules_disallow_ads"
        assert audit.ad_rule_details["deny_matches"][0]["source"] == "about"
        service.group_manager.update_scores.assert_awaited_once()
        assert service.group_manager.update_scores.await_args.kwargs["rule_score"] == 30

    @pytest.mark.asyncio
    async def test_group_ad_rule_decision_blocks_membership_and_profile(self, test_db):
        account = TelegramAccount(
            phone="+15550001901",
            identifier="+15550001901",
            session_name="ad_rule_block_account",
            account_type=AccountType.PROMOTER,
            status=AccountStatus.ONLINE,
            is_active=True,
        )
        group = Group(group_id=719001, title="No Ads Group", level=GroupLevel.B, status="active")
        test_db.add_all([account, group])
        await test_db.flush()
        membership = GroupAccountMembership(
            group_id=group.id,
            telegram_group_id=group.group_id,
            account_id=account.id,
            status="joined",
            join_method="auto_keyword_search",
            warmup_status="joined_pending_test",
            probe_status="not_started",
            ad_status="warming",
        )
        test_db.add(membership)
        await test_db.commit()

        service = AcquisitionAutomationService(db=test_db)
        service._leave_group = AsyncMock(return_value=None)
        audit = acquisition_automation.JoinedGroupAuditResult(
            passed=True,
            ad_allowed=False,
            ad_rule_reason="group_rules_disallow_ads",
            ad_rule_details={
                "deny_matches": [
                    {"source": "about", "text": "本群禁止广告推广"},
                ],
            },
        )

        await service._apply_join_audit_ad_rule_decision(group, membership, audit)
        await test_db.refresh(group)
        await test_db.refresh(membership)
        profile = (
            await test_db.execute(select(GroupAdProfile).where(GroupAdProfile.group_id == group.id))
        ).scalar_one()

        assert group.status == "ad_blocked"
        service._leave_group.assert_awaited_once()
        assert service._leave_group.await_args.args[0] == account.id
        assert service._leave_group.await_args.args[1].group_id == group.group_id
        assert membership.status == "left"
        assert membership.left_at is not None
        assert membership.warmup_status == "blocked"
        assert membership.probe_status == "skipped"
        assert membership.ad_status == "blocked"
        assert "group_rules_ad_blocked_and_left" in (membership.note or "")
        assert profile.ad_tier == GroupAdTier.BLOCKED.value
        assert profile.daily_capacity == 0

    @pytest.mark.asyncio
    async def test_verification_button_is_clicked_before_audit(self):
        client = FakeVerificationButtonAuditClient()
        service, _account_pool = self._service_with_client(client)
        service._join_verification_settings = AsyncMock(return_value=JoinVerificationSettings(
            ai_enabled=False,
            post_action_wait_seconds=0,
        ))

        audit = await service._evaluate_joined_group(1, self._group())

        assert audit.passed is True
        assert audit.verification_action == "click_button"
        assert client.clicked_buttons == ["我已阅读"]
        service.group_manager.update_scores.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_verification_button_delayed_unlock_rechecks_before_rejecting(self):
        client = FakeDelayedVerificationButtonAuditClient(rechecks_before_unlock=1)
        service, _account_pool = self._service_with_client(client)
        service._join_verification_settings = AsyncMock(return_value=JoinVerificationSettings(
            ai_enabled=False,
            post_action_wait_seconds=0,
            post_action_recheck_attempts=3,
            post_action_extra_wait_seconds=0,
        ))

        audit = await service._evaluate_joined_group(1, self._group())

        assert audit.passed is True
        assert audit.verification_action == "click_button"
        assert client.clicked_buttons == ["我已阅读"]
        assert len(audit.verification_details["post_action_rechecks"]) == 2
        assert audit.verification_details["post_action_final_can_send"] is True
        service.group_manager.update_scores.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_successful_button_click_keeps_restricted_chinese_group_pending(self):
        client = FakePermanentRestrictedButtonAuditClient()
        service, _account_pool = self._service_with_client(client)
        service._join_verification_settings = AsyncMock(return_value=JoinVerificationSettings(
            ai_enabled=False,
            post_action_wait_seconds=0,
            post_action_recheck_attempts=2,
            post_action_extra_wait_seconds=0,
        ))

        audit = await service._evaluate_joined_group(1, self._group())

        assert audit.passed is False
        assert audit.reason == "verification_pending_recheck"
        assert audit.should_leave is False
        assert audit.verification_action == "click_button"
        assert audit.verification_details["button_text"] == "我已阅读"
        assert len(audit.verification_details["post_action_rechecks"]) == 2
        assert service._membership_status_after_failed_audit(audit) == "pending"
        service.group_manager.update_scores.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ai_verification_answer_is_sent_before_audit(self):
        client = FakeVerificationQuestionAuditClient()
        service, _account_pool = self._service_with_client(client)
        service._join_verification_settings = AsyncMock(return_value=JoinVerificationSettings(
            post_action_wait_seconds=0,
            confidence_threshold=0.5,
        ))
        service._ask_join_verification_ai = AsyncMock(
            return_value=JoinVerificationDecision(
                challenge_type="question",
                action="send_answer",
                confidence=0.92,
                answer="中文用户，学习交流和找资料。",
                reason="simple verification question",
            )
        )

        audit = await service._evaluate_joined_group(1, self._group())

        assert audit.passed is True
        assert audit.verification_action == "send_answer"
        assert client.sent_messages == ["中文用户，学习交流和找资料。"]
        service.group_manager.update_scores.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_captcha_verification_is_not_auto_solved(self):
        client = FakeCaptchaAuditClient()
        service, _account_pool = self._service_with_client(client)
        service._join_verification_settings = AsyncMock(return_value=JoinVerificationSettings(
            post_action_wait_seconds=0,
            unknown_challenge_action="leave",
        ))
        service._ask_join_verification_ai = AsyncMock(return_value=None)

        audit = await service._evaluate_joined_group(1, self._group())

        assert audit.passed is False
        assert audit.reason == "verification_manual_required"
        assert audit.verification_action == "manual"
        assert client.sent_messages == []
        service._ask_join_verification_ai.assert_awaited_once()
        service.group_manager.update_scores.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ai_can_override_local_manual_verification_with_safe_answer(self):
        client = FakeCaptchaAuditClient()
        service, _account_pool = self._service_with_client(client)
        service._join_verification_settings = AsyncMock(return_value=JoinVerificationSettings(
            post_action_wait_seconds=0,
            confidence_threshold=0.5,
        ))
        service._ask_join_verification_ai = AsyncMock(
            return_value=JoinVerificationDecision(
                challenge_type="question",
                action="send_answer",
                confidence=0.91,
                answer="中文用户，学习交流和找资料。",
                reason="text verification question",
            )
        )

        audit = await service._evaluate_joined_group(1, self._group())

        assert audit.passed is True
        assert audit.verification_action == "send_answer"
        assert client.sent_messages == ["中文用户，学习交流和找资料。"]
        service.group_manager.update_scores.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_verification_answer_timeout_is_not_left_pending(self):
        client = FakeSlowVerificationQuestionAuditClient()
        service, _account_pool = self._service_with_client(client)

        result = await service._execute_join_verification_decision(
            client,
            SimpleNamespace(id=7001),
            [],
            JoinVerificationDecision(
                challenge_type="question",
                action="send_answer",
                confidence=0.95,
                answer="中文用户，学习交流和找资料。",
            ),
            JoinVerificationSettings(
                confidence_threshold=0.5,
                action_timeout_seconds=0.01,
            ),
        )

        assert result.success is False
        assert result.reason == "answer_send_timeout"
        assert result.should_leave is True

    @pytest.mark.asyncio
    async def test_group_membership_banned_after_verification_is_synced(self):
        client = FakeBannedAfterAnswerAuditClient()
        service, _account_pool = self._service_with_client(client)
        service._join_verification_settings = AsyncMock(return_value=JoinVerificationSettings(
            post_action_wait_seconds=0,
            confidence_threshold=0.5,
        ))
        service._ask_join_verification_ai = AsyncMock(
            return_value=JoinVerificationDecision(
                challenge_type="question",
                action="send_answer",
                confidence=0.92,
                answer="中文用户，学习交流和找资料。",
            )
        )

        audit = await service._evaluate_joined_group(1, self._group())

        assert audit.passed is False
        assert audit.reason == "group_membership_banned"
        assert audit.permission_reason == "group_membership_banned"
        assert audit.should_leave is False
        assert audit.verification_action == "send_answer"
        assert service._membership_status_after_failed_audit(audit) == "banned"
        service.group_manager.update_scores.assert_not_awaited()


class TestAutoJoinStateHandling:
    @pytest.mark.asyncio
    async def test_join_verification_ai_timeout_is_capped_at_45_seconds(self, monkeypatch):
        async def fake_auto_join_settings(_db):
            return {"join_verification": {"ai_timeout_seconds": 90}}

        monkeypatch.setattr(
            acquisition_automation,
            "get_auto_join_scheduler_settings",
            fake_auto_join_settings,
        )
        service = AcquisitionAutomationService(db=MagicMock())

        settings = await service._join_verification_settings()

        assert settings.ai_timeout_seconds == 45.0

    def test_join_request_success_message_is_pending(self):
        service = AcquisitionAutomationService(db=MagicMock())

        reason = service._classify_join_error(
            RuntimeError("You have successfully requested to join this chat or channel")
        )

        assert reason == "join_request_pending"

    def test_private_banned_evaluation_error_is_group_membership_banned(self):
        service = AcquisitionAutomationService(db=MagicMock())

        reason, should_leave = service._classify_group_evaluation_error(
            RuntimeError(
                "The channel specified is private and you lack permission to access it. "
                "Another reason may be that you were banned from it"
            )
        )

        assert reason == "group_membership_banned"
        assert should_leave is False

    @pytest.mark.asyncio
    async def test_pending_membership_does_not_set_left_at(self, test_db):
        group = Group(
            group_id=99001,
            title="Pending Join Group",
            username="pending_join_group",
            member_count=500,
            status="pending",
            discovery_source="auto_keyword_search",
            source_keyword="TikTok群",
        )
        test_db.add(group)
        await test_db.commit()
        await test_db.refresh(group)
        service = AcquisitionAutomationService(db=test_db)

        membership = await service._upsert_account_membership(
            group,
            account_id=1,
            status="pending",
            join_method="auto_keyword_search",
            source_keyword="TikTok群",
        )

        assert membership.status == "pending"
        assert membership.joined_at is not None
        assert membership.left_at is None

    @pytest.mark.asyncio
    async def test_auto_join_persists_all_search_results_then_joins_one(self, test_db):
        account = TelegramAccount(
            phone="+10000000001",
            identifier="+10000000001",
            session_name="auto_join_queue_test",
            account_type=AccountType.PROMOTER,
            status=AccountStatus.ONLINE,
            is_active=True,
        )
        test_db.add(account)
        await test_db.flush()
        config = AccountOperationConfig(
            account_id=account.id,
            auto_join_enabled=True,
            keyword_auto_replenish_enabled=False,
            join_interval_min_seconds=60,
            join_interval_max_seconds=60,
        )
        keyword = GroupSearchKeyword(
            text="华人群",
            normalized_text="华人群",
            keyword_type=KeywordType.DEMAND.value,
            status=SearchKeywordStatus.APPROVED,
            source=SearchKeywordSource.MANUAL,
            enabled=True,
        )
        test_db.add_all([config, keyword])
        await test_db.commit()
        await test_db.refresh(config)
        config.account = account

        service = AcquisitionAutomationService(db=test_db)
        service._auto_join_dynamic_daily_limit = AsyncMock(return_value=1)
        service.group_finder.search_by_keyword = AsyncMock(
            return_value=[
                DiscoveredGroup(91001, "Group 1", "group_1", 0, False),
                DiscoveredGroup(91002, "Group 2", "group_2", 500, False),
                DiscoveredGroup(91003, "Group 3", "group_3", 800, False),
            ]
        )
        service._attempt_join_queued_group = AsyncMock(return_value=acquisition_automation.AutomationRunResult(succeeded=1))

        result = await service._run_auto_join_for_account_config(
            config,
            now=datetime.utcnow(),
            keywords_per_account=1,
            max_groups_per_keyword=50,
            dry_run=False,
        )

        groups = (await test_db.execute(select(Group).order_by(Group.group_id))).scalars().all()
        records = (await test_db.execute(select(GroupSearchRecord).order_by(GroupSearchRecord.group_id))).scalars().all()

        assert result.succeeded == 1
        assert service._attempt_join_queued_group.await_count == 1
        assert [group.group_id for group in groups] == [91001, 91002, 91003]
        assert {group.status for group in groups} == {"pending_join"}
        assert [record.group_id for record in records] == [91001, 91002, 91003]


class FakeAuditClient:
    def __init__(
        self,
        *,
        title: str,
        messages: list[str],
        default_send_blocked: bool = False,
        about: str = "",
    ):
        self.title = title
        self.messages = messages
        self.default_send_blocked = default_send_blocked
        self.about = about
        self.participant_banned = False
        self.participant_left = False

    async def get_entity(self, target):
        return SimpleNamespace(
            id=7001,
            title=self.title,
            username=target,
            about=getattr(self, "about", ""),
            participants_count=1500,
            broadcast=False,
            megagroup=True,
            default_banned_rights=SimpleNamespace(
                send_messages=self.default_send_blocked,
                send_plain=False,
            ),
        )

    async def get_me(self):
        return SimpleNamespace(id=1)

    async def get_permissions(self, entity, user=None):
        if user is None:
            return SimpleNamespace(
                send_messages=self.default_send_blocked,
                send_plain=False,
            )
        return SimpleNamespace(
            is_creator=False,
            is_admin=False,
            has_left=self.participant_left,
            is_banned=self.participant_banned,
            participant=SimpleNamespace(
                banned_rights=SimpleNamespace(
                    send_messages=False,
                    send_plain=False,
                )
            ),
        )

    def iter_messages(self, entity, limit=50):
        async def _iter():
            for index, text in enumerate(self.messages[:limit]):
                yield SimpleNamespace(message=text, sender_id=(index % 6) + 1)

        return _iter()


CHINESE_AUDIT_MESSAGES = [
    "最近做独立站和外贸收款的人多吗",
    "PayPal 风控之后大家都怎么处理",
    "有没有适合新手的选品方向",
    "我这边主要做美区店铺",
    "支付通道稳定性最近怎么样",
    "广告账户被限制后怎么申诉",
    "求一个靠谱的建站工具推荐",
    "这个类目现在转化还不错",
    "物流和客服成本要提前算好",
    "有人做 Etsy 手工品吗",
    "我想找同城卖家交流一下",
    "新号前期别太猛发广告",
]


class FakeButtonMessage:
    def __init__(self, client):
        self.client = client
        self.id = 501
        self.sender_id = 99
        self.message = "入群验证：请阅读群规后点击我已阅读"
        self.buttons = [[SimpleNamespace(text="我已阅读")]]

    async def click(self, text=None):
        self.client.clicked_buttons.append(text)
        self.client.default_send_blocked = False
        self.client.messages = CHINESE_AUDIT_MESSAGES


class FakeVerificationButtonAuditClient(FakeAuditClient):
    def __init__(self):
        self.clicked_buttons: list[str] = []
        self.messages: list[object] = [FakeButtonMessage(self)]
        self.default_send_blocked = True
        self.title = "Chinese Sellers"

    def iter_messages(self, entity, limit=50):
        async def _iter():
            for index, item in enumerate(self.messages[:limit]):
                if isinstance(item, str):
                    yield SimpleNamespace(message=item, sender_id=(index % 6) + 1)
                else:
                    yield item

        return _iter()


class FakeDelayedVerificationButtonAuditClient(FakeVerificationButtonAuditClient):
    def __init__(self, *, rechecks_before_unlock: int):
        super().__init__()
        self.rechecks_before_unlock = rechecks_before_unlock

    async def get_permissions(self, entity, user=None):
        if user is None and self.clicked_buttons and self.rechecks_before_unlock > 0:
            self.rechecks_before_unlock -= 1
            return SimpleNamespace(
                send_messages=True,
                send_plain=False,
            )
        return await super().get_permissions(entity, user)


class FakePermanentRestrictedButtonMessage(FakeButtonMessage):
    async def click(self, text=None):
        self.client.clicked_buttons.append(text)
        self.client.default_send_blocked = True
        self.client.messages = CHINESE_AUDIT_MESSAGES


class FakePermanentRestrictedButtonAuditClient(FakeVerificationButtonAuditClient):
    def __init__(self):
        self.clicked_buttons: list[str] = []
        self.messages: list[object] = [FakePermanentRestrictedButtonMessage(self)]
        self.default_send_blocked = True
        self.title = "Chinese Sellers"


class FakeVerificationQuestionAuditClient(FakeAuditClient):
    def __init__(self):
        super().__init__(
            title="Chinese Sellers",
            default_send_blocked=True,
            messages=["入群验证：请简单介绍你自己"],
        )
        self.sent_messages: list[str] = []

    async def send_message(self, entity, message):
        self.sent_messages.append(message)
        self.default_send_blocked = False
        self.messages = CHINESE_AUDIT_MESSAGES


class FakeCaptchaAuditClient(FakeVerificationQuestionAuditClient):
    def __init__(self):
        super().__init__()
        self.messages = ["入群验证：请输入图片验证码后继续"]


class FakeSlowVerificationQuestionAuditClient(FakeVerificationQuestionAuditClient):
    async def send_message(self, entity, message):
        await asyncio.sleep(1)


class FakeBannedAfterAnswerAuditClient(FakeVerificationQuestionAuditClient):
    async def send_message(self, entity, message):
        self.sent_messages.append(message)
        self.participant_banned = True
        self.default_send_blocked = True


class TestAutoJoinKeywordSelection:
    @pytest.mark.asyncio
    async def test_get_search_keywords_excludes_used_and_generic_terms(self, test_db):
        used = GroupSearchKeyword(
            text="外贸群",
            keyword_type=KeywordType.DEMAND.value,
            status=SearchKeywordStatus.APPROVED,
            source=SearchKeywordSource.AI,
            used_at=datetime.utcnow(),
            use_count=1,
        )
        generic = GroupSearchKeyword(
            text="日区",
            keyword_type=KeywordType.DEMAND.value,
            status=SearchKeywordStatus.APPROVED,
            source=SearchKeywordSource.AI,
        )
        fresh = GroupSearchKeyword(
            text="卖家群",
            keyword_type=KeywordType.DEMAND.value,
            status=SearchKeywordStatus.APPROVED,
            source=SearchKeywordSource.AI,
        )
        test_db.add_all([used, generic, fresh])
        await test_db.commit()

        service = AcquisitionAutomationService(db=test_db)
        config = SimpleNamespace(keyword_types=None)

        keywords = await service._get_search_keywords(config, limit=5)

        assert [item.text for item in keywords] == ["卖家群"]

    @pytest.mark.asyncio
    async def test_get_search_keywords_interleaves_keyword_types(self, test_db):
        records = [
            GroupSearchKeyword(
                text="餐饮群",
                keyword_type=KeywordType.DEMAND.value,
                status=SearchKeywordStatus.APPROVED,
                source=SearchKeywordSource.AI,
            ),
            GroupSearchKeyword(
                text="AI群",
                keyword_type=KeywordType.INQUIRY.value,
                status=SearchKeywordStatus.APPROVED,
                source=SearchKeywordSource.AI,
            ),
            GroupSearchKeyword(
                text="接单群",
                keyword_type=KeywordType.PRICE.value,
                status=SearchKeywordStatus.APPROVED,
                source=SearchKeywordSource.AI,
            ),
            GroupSearchKeyword(
                text="交流群",
                keyword_type=KeywordType.COMPETITOR.value,
                status=SearchKeywordStatus.APPROVED,
                source=SearchKeywordSource.AI,
            ),
        ]
        test_db.add_all(records)
        await test_db.commit()

        service = AcquisitionAutomationService(db=test_db)
        config = SimpleNamespace(keyword_types=None)

        keywords = await service._get_search_keywords(config, limit=4)

        assert [item.text for item in keywords] == ["餐饮群", "AI群", "接单群", "交流群"]

    @pytest.mark.asyncio
    async def test_get_search_keywords_prefers_group_like_terms(self, test_db):
        weak = GroupSearchKeyword(
            text="电商会",
            keyword_type=KeywordType.DEMAND.value,
            status=SearchKeywordStatus.APPROVED,
            source=SearchKeywordSource.AI,
        )
        strong = GroupSearchKeyword(
            text="餐饮群",
            keyword_type=KeywordType.DEMAND.value,
            status=SearchKeywordStatus.APPROVED,
            source=SearchKeywordSource.AI,
        )
        bad_suffix = GroupSearchKeyword(
            text="电商局",
            keyword_type=KeywordType.DEMAND.value,
            status=SearchKeywordStatus.APPROVED,
            source=SearchKeywordSource.AI,
        )
        test_db.add_all([bad_suffix, weak, strong])
        await test_db.commit()

        service = AcquisitionAutomationService(db=test_db)
        config = SimpleNamespace(keyword_types='["demand"]')

        keywords = await service._get_search_keywords(config, limit=3)

        assert [item.text for item in keywords] == ["餐饮群"]

    @pytest.mark.asyncio
    async def test_zero_result_keyword_is_discarded_after_repeated_misses(self, test_db):
        keyword = GroupSearchKeyword(
            text="低命中群",
            keyword_type=KeywordType.DEMAND.value,
            status=SearchKeywordStatus.APPROVED,
            source=SearchKeywordSource.AI,
        )
        test_db.add(keyword)
        await test_db.commit()
        await test_db.refresh(keyword)

        service = AcquisitionAutomationService(db=test_db)

        await service._record_search_keyword_feedback(keyword, found_count=0, candidate_count=0)

        assert keyword.status == SearchKeywordStatus.APPROVED
        assert keyword.enabled is True
        assert keyword.used_at is None
        assert keyword.use_count == 1

        await service._record_search_keyword_feedback(keyword, found_count=0, candidate_count=0)

        assert keyword.status == SearchKeywordStatus.DISCARDED
        assert keyword.enabled is False
        assert keyword.used_at is not None
        assert keyword.use_count == 2

    @pytest.mark.asyncio
    async def test_found_without_joinable_candidates_is_discarded(self, test_db):
        keyword = GroupSearchKeyword(
            text="无候选群",
            keyword_type=KeywordType.DEMAND.value,
            status=SearchKeywordStatus.APPROVED,
            source=SearchKeywordSource.AI,
        )
        test_db.add(keyword)
        await test_db.commit()
        await test_db.refresh(keyword)

        service = AcquisitionAutomationService(db=test_db)

        await service._record_search_keyword_feedback(keyword, found_count=8, candidate_count=0)

        assert keyword.status == SearchKeywordStatus.DISCARDED
        assert keyword.enabled is False
        assert keyword.used_at is not None
        assert keyword.use_count == 1
        assert keyword.trigger_count == 0

    @pytest.mark.asyncio
    async def test_keyword_learning_hints_include_positive_and_negative_terms(self, test_db):
        positive = GroupSearchKeyword(
            text="华人群",
            keyword_type=KeywordType.DEMAND.value,
            status=SearchKeywordStatus.DISCARDED,
            source=SearchKeywordSource.AI,
            use_count=2,
            trigger_count=5,
        )
        negative = GroupSearchKeyword(
            text="陪读群",
            keyword_type=KeywordType.DEMAND.value,
            status=SearchKeywordStatus.DISCARDED,
            source=SearchKeywordSource.AI,
            use_count=2,
            trigger_count=0,
        )
        test_db.add_all([positive, negative])
        await test_db.commit()

        service = AcquisitionAutomationService(db=test_db)

        hints = await service._build_keyword_learning_hints_by_type(
            recent_negative_keywords=["美工群"],
        )

        assert "华人群" in hints[KeywordType.DEMAND.value]["positive_keywords"]
        assert "陪读群" in hints[KeywordType.DEMAND.value]["negative_keywords"]
        assert "美工群" in hints[KeywordType.DEMAND.value]["negative_keywords"]

    @pytest.mark.asyncio
    async def test_keyword_generator_prompt_uses_learning_hints(self):
        class FakeLLM:
            def __init__(self):
                self.prompt = ""

            async def generate(self, prompt):
                self.prompt = prompt
                return "招聘群\n资源群"

        llm = FakeLLM()
        generator = KeywordGenerator(llm)

        keywords = await generator.generate(
            category=KeywordType.DEMAND.value,
            count=2,
            avoid_keywords=["外贸群"],
            learning_hints={
                "positive_keywords": ["华人群"],
                "negative_keywords": ["陪读群"],
            },
        )

        assert [keyword.text for keyword in keywords] == ["招聘群", "资源群"]
        assert "历史高命中/可入群样本" in llm.prompt
        assert "华人群" in llm.prompt
        assert "历史低命中/无候选样本" in llm.prompt
        assert "陪读群" in llm.prompt

    @pytest.mark.asyncio
    async def test_low_hit_rate_triggers_auto_approved_replenishment(self):
        service = AcquisitionAutomationService(db=MagicMock())
        service._count_searchable_keywords = AsyncMock(return_value=20)
        service._build_keyword_learning_hints_by_type = AsyncMock(
            return_value={
                KeywordType.DEMAND.value: {
                    "positive_keywords": ["华人群"],
                    "negative_keywords": ["陪读群"],
                },
                KeywordType.INQUIRY.value: {
                    "positive_keywords": ["AI群"],
                    "negative_keywords": ["美工群"],
                },
            }
        )
        service.replenish_keywords = AsyncMock(return_value={"created": 12})
        config = SimpleNamespace(
            account_id=1,
            keyword_auto_replenish_enabled=True,
            keyword_replenish_requires_review=False,
            keyword_types='["demand", "inquiry"]',
        )

        detail = await service._ensure_keywords_after_low_hit_rate(
            config,
            search_feedback={
                "searched": 5,
                "found": 0,
                "candidates": 0,
                "zero_result": 5,
                "negative_keywords": ["陪读群"],
            },
        )

        assert detail["action"] == "keyword_replenished_after_low_hit_rate"
        assert detail["created"] == 12
        assert detail["auto_approved"] is True
        assert detail["negative_keyword_examples"] == ["陪读群"]
        assert detail["positive_keyword_examples"] == ["华人群"]
        assert service.replenish_keywords.await_args.kwargs["learning_hints_by_type"][
            KeywordType.DEMAND.value
        ]["negative_keywords"] == ["陪读群"]
        service.replenish_keywords.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_low_candidate_rate_triggers_replenishment_even_with_found_results(self):
        service = AcquisitionAutomationService(db=MagicMock())
        service._count_searchable_keywords = AsyncMock(return_value=20)
        service._build_keyword_learning_hints_by_type = AsyncMock(
            return_value={
                KeywordType.DEMAND.value: {
                    "positive_keywords": ["华人群"],
                    "negative_keywords": ["商单群"],
                }
            }
        )
        service.replenish_keywords = AsyncMock(return_value={"created": 12})
        config = SimpleNamespace(
            account_id=1,
            keyword_auto_replenish_enabled=True,
            keyword_replenish_requires_review=False,
            keyword_types='["demand"]',
        )

        detail = await service._ensure_keywords_after_low_hit_rate(
            config,
            search_feedback={"searched": 5, "found": 40, "candidates": 0, "zero_result": 0},
        )

        assert detail["action"] == "keyword_replenished_after_low_hit_rate"
        assert detail["candidate_groups"] == 0
        service.replenish_keywords.assert_awaited_once()


class TestAccountAssetTierPolicy:
    def test_asset_tier_multiplier_uses_configured_policy(self):
        account = TelegramAccount(
            phone="+15550001888",
            identifier="+15550001888",
            session_name="asset_tier_weight_account",
            account_type=AccountType.PROMOTER,
            status=AccountStatus.ONLINE,
            is_active=True,
            asset_tier=AccountAssetTier.YEAR_3_PLUS.value,
        )

        multiplier = AccountDynamicFrequencyService.account_asset_multiplier(
            DEFAULT_ACCOUNT_ASSET_POLICY_SETTINGS,
            account,
            "ad_multiplier",
        )

        assert multiplier == 1.35

    def test_asset_tier_age_floor_prevents_old_import_from_new_segment(self):
        now = datetime.utcnow()
        account = TelegramAccount(
            phone="+15550001889",
            identifier="+15550001889",
            session_name="asset_tier_age_account",
            account_type=AccountType.PROMOTER,
            status=AccountStatus.ONLINE,
            is_active=True,
            created_at=now,
            asset_tier=AccountAssetTier.YEAR_2.value,
        )
        service = AccountDynamicFrequencyService(db=MagicMock())

        segment = service.lifecycle_segment(
            account,
            now,
            health_score=80,
            join_metrics={
                "writable_rate": 1.0,
                "probe_success_rate_24h": 1.0,
                "ad_success_rate_24h": 1.0,
                "average_group_quality_score": 70,
                "probe_success_24h": 0,
            },
            join_attempts={"peer_flood": 0},
            asset_policy=DEFAULT_ACCOUNT_ASSET_POLICY_SETTINGS,
        )

        assert segment == "normal"


class TestAdDeliveryFailureHandling:
    @pytest.mark.asyncio
    async def test_group_control_failure_marks_unjoined_membership_left(self, test_db, monkeypatch):
        account = TelegramAccount(
            phone="+15550000001",
            identifier="+15550000001",
            session_name="ad_failure_account",
            account_type=AccountType.PROMOTER,
            status=AccountStatus.ONLINE,
            is_active=True,
        )
        group = Group(group_id=1946699880, title="No Write Group", level=GroupLevel.B, status="active")
        test_db.add_all([account, group])
        await test_db.flush()
        membership = GroupAccountMembership(
            group_id=group.id,
            telegram_group_id=group.group_id,
            account_id=account.id,
            status="joined",
            join_method="manual",
        )
        test_db.add(membership)
        await test_db.commit()

        service = AcquisitionAutomationService(db=test_db)
        service._leave_group = AsyncMock(return_value="The target user is not a member of the specified megagroup")
        monkeypatch.setattr(
            acquisition_automation,
            "get_ad_failure_policy_settings",
            AsyncMock(
                return_value={
                    "enabled": True,
                    "leave_on_group_control_failure": True,
                    "group_control_failure_limit": 1,
                    "group_control_failure_window_hours": 24,
                    "levels": ["B"],
                }
            ),
        )
        test_db.add(
            AdDeliveryLog(
                account_id=account.id,
                group_id=group.id,
                telegram_group_id=group.group_id,
                ad_campaign_id=1,
                status=DeliveryStatus.FAILED.value,
                error=f"{acquisition_automation.AD_GROUP_CONTROL_ERROR_PREFIX}can't write",
            )
        )
        await test_db.commit()

        await service._handle_group_control_ad_failure(
            account.id,
            group,
            f"{acquisition_automation.AD_GROUP_CONTROL_ERROR_PREFIX}can't write",
        )

        await test_db.refresh(membership)
        assert membership.status == "left"
        assert membership.left_at is not None
        assert "ad_group_control_membership_leave" in membership.note
        assert "leave_error" in membership.note

    @pytest.mark.asyncio
    async def test_group_control_failure_waits_for_configured_threshold(self, test_db, monkeypatch):
        account = TelegramAccount(
            phone="+15550000011",
            identifier="+15550000011",
            session_name="ad_failure_threshold_account",
            account_type=AccountType.PROMOTER,
            status=AccountStatus.ONLINE,
            is_active=True,
        )
        group = Group(group_id=1946699890, title="Threshold Group", level=GroupLevel.B, status="active")
        test_db.add_all([account, group])
        await test_db.flush()
        membership = GroupAccountMembership(
            group_id=group.id,
            telegram_group_id=group.group_id,
            account_id=account.id,
            status="joined",
            join_method="manual",
        )
        test_db.add_all(
            [
                membership,
                AdDeliveryLog(
                    account_id=account.id,
                    group_id=group.id,
                    telegram_group_id=group.group_id,
                    ad_campaign_id=1,
                    status=DeliveryStatus.FAILED.value,
                    error=f"{acquisition_automation.AD_GROUP_CONTROL_ERROR_PREFIX}can't write",
                ),
            ]
        )
        await test_db.commit()

        service = AcquisitionAutomationService(db=test_db)
        service._leave_group = AsyncMock(return_value=None)
        monkeypatch.setattr(
            acquisition_automation,
            "get_ad_failure_policy_settings",
            AsyncMock(
                return_value={
                    "enabled": True,
                    "leave_on_group_control_failure": True,
                    "group_control_failure_limit": 2,
                    "group_control_failure_window_hours": 24,
                    "levels": ["B"],
                }
            ),
        )

        await service._handle_group_control_ad_failure(
            account.id,
            group,
            f"{acquisition_automation.AD_GROUP_CONTROL_ERROR_PREFIX}can't write",
        )

        await test_db.refresh(membership)
        assert membership.status == "joined"
        service._leave_group.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_account_group_ad_daily_capacity_uses_asset_multiplier(self, test_db, monkeypatch):
        account = TelegramAccount(
            phone="+15550000002",
            identifier="+15550000002",
            session_name="asset_group_cap_account",
            account_type=AccountType.PROMOTER,
            status=AccountStatus.ONLINE,
            is_active=True,
            asset_tier=AccountAssetTier.YEAR_1.value,
        )
        membership = GroupAccountMembership(
            telegram_group_id=1946699881,
            account_id=1,
            status="joined",
            join_method="manual",
            account_group_daily_cap=400,
        )
        test_db.add(account)
        await test_db.flush()
        membership.account_id = account.id

        policy = dict(DEFAULT_ACCOUNT_ASSET_POLICY_SETTINGS)
        policy["tiers"] = {
            **DEFAULT_ACCOUNT_ASSET_POLICY_SETTINGS["tiers"],
            AccountAssetTier.YEAR_1.value: {
                **DEFAULT_ACCOUNT_ASSET_POLICY_SETTINGS["tiers"][AccountAssetTier.YEAR_1.value],
                "ad_multiplier": 0.72,
            },
        }
        monkeypatch.setattr(acquisition_automation, "get_account_asset_policy_settings", AsyncMock(return_value=policy))

        service = AcquisitionAutomationService(db=test_db)
        cap = await service._account_group_ad_daily_capacity(
            account.id,
            membership,
            {"account_group_daily_cap_default": 20},
        )

        assert cap == 14

    @pytest.mark.asyncio
    async def test_ad_daily_limit_uses_recent_probe_formula_and_business_stage(self, test_db):
        account = TelegramAccount(
            phone="+15550000901",
            identifier="+15550000901",
            session_name="ad_formula_account",
            account_type=AccountType.PROMOTER,
            status=AccountStatus.ONLINE,
            is_active=True,
            created_at=datetime.utcnow() - timedelta(days=10),
            asset_tier=AccountAssetTier.YEAR_1.value,
        )
        config = AccountOperationConfig(
            account=account,
            auto_join_enabled=True,
            auto_ads_enabled=True,
            max_groups_per_day=100,
            max_groups_total=1000,
            max_messages_per_day=500,
            business_stage="normal",
            enabled=True,
        )
        campaign = AdCampaign(
            name="Probe Formula Campaign",
            enabled=True,
            status="active",
            send_mode=AdSendMode.INTERVAL.value,
            max_sends_per_account_per_day=500,
        )
        test_db.add_all([account, config, campaign])
        await test_db.flush()

        now = datetime.utcnow()
        for index in range(10):
            group = Group(
                group_id=900000 + index,
                title=f"Formula Group {index}",
                level=GroupLevel.B,
                status="active",
            )
            test_db.add(group)
            await test_db.flush()
            test_db.add(
                GroupAccountMembership(
                    group_id=group.id,
                    telegram_group_id=group.group_id,
                    account_id=account.id,
                    status="joined",
                    join_method="manual",
                    probe_status="success",
                    last_probe_at=now - timedelta(minutes=10),
                    ad_eligible_after=now - timedelta(minutes=1),
                    note='{"passed": true, "can_send_messages": true}',
                )
            )
            test_db.add(
                GroupAdProfile(
                    group_id=group.id,
                    telegram_group_id=group.group_id,
                    ad_policy_mode=GroupAdPolicyMode.SOFT_AD_ALLOWED.value,
                    ad_policy_confidence=100,
                    ad_policy_source="manual",
                    ad_policy_verified_at=now - timedelta(days=5),
                    ad_policy_expires_at=now + timedelta(days=30),
                    ad_tier=GroupAdTier.TRIAL.value,
                    daily_capacity=1,
                )
            )
        await test_db.commit()

        service = AcquisitionAutomationService(db=test_db)
        limit = await service._ad_dynamic_daily_limit(account.id, config, campaign, now)

        assert 1 <= limit <= 50
        assert config.business_stage == "hot"

    @pytest.mark.asyncio
    async def test_ad_daily_limit_blocks_new_managed_account_observe_stage(self, test_db):
        now = datetime.utcnow()
        account = TelegramAccount(
            phone="+15550000911",
            identifier="+15550000911",
            session_name="ad_warmup_observe_account",
            account_type=AccountType.PROMOTER,
            status=AccountStatus.ONLINE,
            is_active=True,
            created_at=now - timedelta(days=30),
            managed_started_at=now,
            asset_tier=AccountAssetTier.YEAR_1.value,
        )
        config = AccountOperationConfig(
            account=account,
            auto_join_enabled=True,
            auto_ads_enabled=True,
            max_groups_per_day=100,
            max_groups_total=1000,
            max_messages_per_day=500,
            business_stage="normal",
            enabled=True,
        )
        campaign = AdCampaign(
            name="Warmup Observe Campaign",
            enabled=True,
            status="active",
            send_mode=AdSendMode.INTERVAL.value,
            max_sends_per_account_per_day=500,
        )
        test_db.add_all([account, config, campaign])
        await test_db.commit()

        service = AccountDynamicFrequencyService(test_db)
        limit = await service.ad_dynamic_daily_limit(account.id, config, campaign, now)

        assert limit == 0
        assert account.warmup_stage == "observe"
        assert config.business_stage == "new"

    @pytest.mark.asyncio
    async def test_auto_join_dynamic_limit_pauses_risk_frozen_account(self, test_db):
        account = TelegramAccount(
            phone="+15550000902",
            identifier="+15550000902",
            session_name="join_pause_account",
            account_type=AccountType.PROMOTER,
            status=AccountStatus.ONLINE,
            is_active=True,
            risk_pause_until=datetime.utcnow() + timedelta(hours=1),
        )
        config = AccountOperationConfig(
            account=account,
            auto_join_enabled=True,
            max_groups_per_day=100,
            max_groups_total=1000,
            enabled=True,
        )
        test_db.add_all([account, config])
        await test_db.commit()

        service = AcquisitionAutomationService(db=test_db)
        limit = await service._auto_join_dynamic_daily_limit(config, datetime.utcnow())

        assert limit == 0
        assert config.business_stage == "cooldown"

    @pytest.mark.asyncio
    async def test_zero_dynamic_join_limit_reports_health_pause(self, test_db):
        service = AcquisitionAutomationService(db=test_db)
        service._auto_join_dynamic_daily_limit = AsyncMock(return_value=0)

        reason = await service._check_join_quota(SimpleNamespace(account_id=3))

        assert reason == "account_dynamic_health_paused"

    @pytest.mark.asyncio
    async def test_join_health_includes_old_joined_memberships_and_ignores_left(self, test_db):
        now = datetime.utcnow()
        account = TelegramAccount(
            phone="+15550000931",
            identifier="+15550000931",
            session_name="old_joined_health_account",
            account_type=AccountType.PROMOTER,
            status=AccountStatus.ONLINE,
            is_active=True,
            created_at=now - timedelta(days=60),
        )
        joined_group = Group(
            group_id=903001,
            title="Old Joined Group",
            level=GroupLevel.B,
            status="active",
        )
        left_group = Group(
            group_id=903002,
            title="Recently Left Group",
            level=GroupLevel.B,
            status="active",
        )
        test_db.add_all([account, joined_group, left_group])
        await test_db.flush()
        test_db.add_all(
            [
                GroupAccountMembership(
                    group_id=joined_group.id,
                    telegram_group_id=joined_group.group_id,
                    account_id=account.id,
                    status="joined",
                    note='{"passed": true, "can_send_messages": true}',
                    updated_at=now - timedelta(days=30),
                ),
                GroupAccountMembership(
                    group_id=left_group.id,
                    telegram_group_id=left_group.group_id,
                    account_id=account.id,
                    status="left",
                    probe_status="failed",
                    last_probe_at=now,
                    note='{"passed": false, "can_send_messages": false}',
                    updated_at=now,
                ),
            ]
        )
        await test_db.commit()

        metrics = await AccountDynamicFrequencyService(test_db).account_join_quality_metrics(
            account.id,
            now,
        )

        assert metrics["joined_groups"] == 1
        assert metrics["writable_checked"] == 1
        assert metrics["writable_success"] == 1
        assert metrics["writable_rate"] == 1.0
        assert metrics["probe_failed_24h"] == 0
        assert metrics["probe_success_rate_24h"] == 1.0

    @pytest.mark.asyncio
    async def test_limited_account_retains_reduced_join_capacity(self, test_db):
        now = datetime.utcnow()
        account = TelegramAccount(
            phone="+15550000932",
            identifier="+15550000932",
            session_name="limited_join_capacity_account",
            account_type=AccountType.PROMOTER,
            status=AccountStatus.ONLINE,
            is_active=True,
            risk_score=69.0,
            risk_level="limited",
            created_at=now - timedelta(days=30),
            managed_started_at=now - timedelta(days=30),
            warmup_stage="normal",
        )
        config = AccountOperationConfig(
            account=account,
            auto_join_enabled=True,
            max_groups_per_day=100,
            max_groups_total=1000,
            business_stage="cooldown",
            enabled=True,
        )
        test_db.add_all([account, config])
        await test_db.commit()

        service = AccountDynamicFrequencyService(test_db)
        health = await service.account_health(account.id, now)
        limit = await service.auto_join_dynamic_daily_limit(config, now)

        assert health["health_score"] >= 45
        assert not any(

            item["reason"] in {"risk_score", "risk_level_limited"}
            for item in health["adjustments"]
        )
        assert service.account_risk_limit_multiplier(account, now) == 0.35
        assert limit > 0

    @pytest.mark.asyncio
    async def test_business_stage_sync_clears_stale_join_cooldown(self, test_db):
        now = datetime(2026, 1, 1, 12, 0, 0)
        account = TelegramAccount(
            phone="+15550000923",
            identifier="+15550000923",
            session_name="stale_join_cooldown_account",
            account_type=AccountType.PROMOTER,
            status=AccountStatus.ONLINE,
            is_active=True,
        )
        config = AccountOperationConfig(
            account=account,
            auto_join_enabled=True,
            max_groups_per_day=100,
            max_groups_total=1000,
            business_stage="normal",
            join_interval_max_seconds=75 * 60,
            next_join_after=now + timedelta(hours=12),
            enabled=True,
        )
        test_db.add_all([account, config])
        await test_db.commit()

        service = AccountDynamicFrequencyService(test_db)
        await service.apply_business_stage_state(config, "normal", now)

        assert config.next_join_after == now

    @pytest.mark.asyncio
    async def test_dynamic_frequency_caps_stable_join_range(self, test_db):
        now = datetime(2026, 1, 1, 12, 0, 0)
        account = TelegramAccount(
            phone="+15550000903",
            identifier="+15550000903",
            session_name="stable_join_policy_account",
            account_type=AccountType.PROMOTER,
            status=AccountStatus.ONLINE,
            is_active=True,
            created_at=now - timedelta(days=10),
            managed_started_at=now - timedelta(days=20),
        )
        config = AccountOperationConfig(
            account=account,
            auto_join_enabled=True,
            max_groups_per_day=100,
            max_groups_total=1000,
            business_stage="normal",
            enabled=True,
        )
        test_db.add_all([account, config])
        await test_db.flush()

        for index in range(10):
            group = Group(
                group_id=901000 + index,
                title=f"Stable Policy Group {index}",
                level=GroupLevel.B,
                status="active",
            )
            test_db.add(group)
            await test_db.flush()
            test_db.add(
                GroupAccountMembership(
                    group_id=group.id,
                    telegram_group_id=group.group_id,
                    account_id=account.id,
                    status="joined",
                    join_method="manual",
                    probe_status="success",
                    last_probe_at=now - timedelta(minutes=10),
                    ad_eligible_after=now - timedelta(minutes=1),
                    note='{"passed": true, "can_send_messages": true}',
                    updated_at=now - timedelta(minutes=5),
                )
            )
        await test_db.commit()

        service = AccountDynamicFrequencyService(test_db)
        limit = await service.auto_join_dynamic_daily_limit(config, now)

        assert 10 < limit <= 100
        assert config.business_stage == "hot"

    def test_dynamic_frequency_ignores_single_failed_probe_sample(self):
        now = datetime(2026, 1, 1, 12, 0, 0)
        account = TelegramAccount(
            phone="+15550000924",
            identifier="+15550000924",
            session_name="single_probe_failure_account",
            account_type=AccountType.PROMOTER,
            status=AccountStatus.ONLINE,
            is_active=True,
            created_at=now - timedelta(days=10),
        )
        service = AccountDynamicFrequencyService(MagicMock())

        segment = service.lifecycle_segment(
            account,
            now,
            93.0,
            {
                "writable_rate": 0.75,
                "probe_success_rate_24h": 0.0,
                "probe_success_24h": 0,
                "probe_failed_24h": 1,
                "ad_success_rate_24h": 1.0,
                "ad_success_24h": 5,
                "ad_failed_24h": 0,
                "average_group_quality_score": 60.0,
            },
            {"peer_flood": 0},
            asset_policy={"enabled": False, "tiers": {}},
        )

        assert segment == "normal"
        assert service.AD_DAILY_RANGES["normal"].maximum == 18

    def test_ad_failures_pause_ads_without_pausing_join_lifecycle(self):
        now = datetime(2026, 1, 1, 12, 0, 0)
        account = TelegramAccount(
            phone="+15550000933",
            identifier="+15550000933",
            session_name="ad_join_lifecycle_isolation",
            account_type=AccountType.PROMOTER,
            status=AccountStatus.ONLINE,
            is_active=True,
            created_at=now - timedelta(days=30),
            managed_started_at=now - timedelta(days=30),
        )
        service = AccountDynamicFrequencyService(MagicMock())
        join_metrics = {
            "writable_rate": 1.0,
            "probe_success_rate_24h": 1.0,
            "probe_success_24h": 3,
            "probe_failed_24h": 0,
            "ad_success_rate_24h": 0.0,
            "ad_success_24h": 0,
            "ad_failed_24h": 3,
            "average_group_quality_score": 60.0,
        }

        ad_segment = service.lifecycle_segment(
            account,
            now,
            80.0,
            join_metrics,
            {"peer_flood": 0},
        )
        join_segment = service.lifecycle_segment(
            account,
            now,
            80.0,
            join_metrics,
            {"peer_flood": 0},
            include_ad_health=False,
        )

        assert ad_segment == "cooldown"
        assert join_segment in {"normal", "stable"}

    @pytest.mark.asyncio
    async def test_join_health_counts_post_join_filters_as_transport_success(self, test_db):
        now = datetime.utcnow()
        account = TelegramAccount(
            phone="+15550000913",
            identifier="+15550000913",
            session_name="post_join_filter_health_account",
            account_type=AccountType.PROMOTER,
            status=AccountStatus.ONLINE,
            is_active=True,
            created_at=now - timedelta(days=10),
        )
        test_db.add(account)
        await test_db.flush()

        for index in range(10):
            filtered = index >= 4
            test_db.add(
                AutoJoinAttempt(
                    account_id=account.id,
                    status=DeliveryStatus.SKIPPED.value if filtered else DeliveryStatus.SUCCESS.value,
                    reason="account_banned" if index == 4 else ("non_chinese_chat" if filtered else None),
                    attempted_at=now - timedelta(minutes=index),
                    joined_at=now - timedelta(minutes=index),
                )
            )
        await test_db.commit()

        service = AccountDynamicFrequencyService(test_db)
        metrics = await service.join_attempt_metrics(account.id, now)
        health = await service.account_health(account.id, now, join_attempts=metrics)

        assert metrics["success"] == 10
        assert metrics["failed"] == 0
        assert metrics["post_join_filtered"] == 6
        assert metrics["success_rate"] == 1.0
        assert metrics["account_banned"] == 0
        assert not any(
            item["reason"] in {"join_account_banned", "join_success_rate_low"}
            for item in health["adjustments"]
        )

    @pytest.mark.asyncio
    async def test_dynamic_frequency_peer_flood_forces_cooldown(self, test_db):
        account = TelegramAccount(
            phone="+15550000904",
            identifier="+15550000904",
            session_name="peer_flood_policy_account",
            account_type=AccountType.PROMOTER,
            status=AccountStatus.ONLINE,
            is_active=True,
            created_at=datetime.utcnow() - timedelta(days=10),
        )
        config = AccountOperationConfig(
            account=account,
            auto_join_enabled=True,
            max_groups_per_day=100,
            max_groups_total=1000,
            business_stage="normal",
            enabled=True,
        )
        test_db.add_all([account, config])
        await test_db.flush()
        test_db.add(
            AutoJoinAttempt(
                account_id=account.id,
                status=DeliveryStatus.FAILED.value,
                reason="peer_flood",
                error="PEER_FLOOD",
                attempted_at=datetime.utcnow() - timedelta(minutes=5),
            )
        )
        await test_db.commit()

        service = AccountDynamicFrequencyService(test_db)
        now = datetime.utcnow()
        health = await service.account_health(account.id, now)
        limit = await service.auto_join_dynamic_daily_limit(config, now)

        assert health["health_score"] <= 20
        assert limit == 0
        assert config.business_stage == "cooldown"

    @pytest.mark.asyncio
    async def test_dynamic_frequency_rejects_low_quality_group_for_new_account(self, test_db):
        now = datetime(2026, 1, 1, 12, 0, 0)
        account = TelegramAccount(
            phone="+15550000905",
            identifier="+15550000905",
            session_name="new_low_quality_policy_account",
            account_type=AccountType.PROMOTER,
            status=AccountStatus.ONLINE,
            is_active=True,
            created_at=now - timedelta(days=1),
        )
        config = AccountOperationConfig(
            account=account,
            auto_join_enabled=True,
            max_groups_per_day=100,
            max_groups_total=1000,
            business_stage="new",
            enabled=True,
        )
        group = Group(
            group_id=902000,
            title="Low Quality Group",
            level=GroupLevel.C,
            status="active",
        )
        test_db.add_all([account, config, group])
        await test_db.commit()

        service = AccountDynamicFrequencyService(test_db)
        decision = await service.join_candidate_decision(config, group, now)

        assert decision["allowed"] is False
        assert decision["reason"] == "group_quality_too_low_for_account_stage"

    @pytest.mark.asyncio
    async def test_ad_delivery_continues_after_failed_group(self, test_db, monkeypatch):
        account = TelegramAccount(
            phone="+15550000002",
            identifier="+15550000002",
            session_name="ad_continue_account",
            account_type=AccountType.PROMOTER,
            status=AccountStatus.ONLINE,
            is_active=True,
        )
        config = AccountOperationConfig(
            account=account,
            auto_join_enabled=False,
            auto_ads_enabled=True,
            max_groups_per_day=100,
            max_groups_total=1000,
            join_interval_min_seconds=60,
            join_interval_max_seconds=120,
            max_messages_per_day=500,
            message_interval_seconds=45,
            keyword_auto_replenish_enabled=False,
            keyword_replenish_requires_review=False,
            risk_level="normal",
            enabled=True,
        )
        level_config = GroupLevelConfig(level=GroupLevel.B, can_send_ads=True)
        first_group = Group(group_id=1111, title="Bad Group", level=GroupLevel.B, status="active")
        second_group = Group(group_id=2222, title="Good Group", level=GroupLevel.B, status="active")
        campaign = AdCampaign(
            name="Regression Campaign",
            enabled=True,
            status="active",
            send_mode=AdSendMode.INTERVAL.value,
            target_group_levels='["B"]',
            interval_minutes=180,
            max_sends_per_group_per_day=9999,
            max_sends_per_account_per_day=500,
        )
        creative = AdCreative(name="Text Creative", content="hello", creative_type="text", enabled=True)
        test_db.add_all([account, config, level_config, first_group, second_group, campaign, creative])
        await test_db.flush()
        binding = AccountAdBinding(
            account_id=account.id,
            ad_campaign_id=campaign.id,
            creative_id=creative.id,
            enabled=True,
            priority=100,
        )
        first_membership = GroupAccountMembership(
            group_id=first_group.id,
            telegram_group_id=first_group.group_id,
            account_id=account.id,
            status="joined",
            join_method="manual",
            warmup_status="ad_eligible",
            probe_status="success",
            ad_status="active",
            last_probe_at=datetime.utcnow(),
            ad_eligible_after=datetime.utcnow() - timedelta(minutes=1),
            interaction_started_at=datetime.utcnow() - timedelta(days=20),
            first_ad_allowed_at=datetime.utcnow() - timedelta(days=5),
        )
        second_membership = GroupAccountMembership(
            group_id=second_group.id,
            telegram_group_id=second_group.group_id,
            account_id=account.id,
            status="joined",
            join_method="manual",
            warmup_status="ad_eligible",
            probe_status="success",
            ad_status="active",
            last_probe_at=datetime.utcnow(),
            ad_eligible_after=datetime.utcnow() - timedelta(minutes=1),
            interaction_started_at=datetime.utcnow() - timedelta(days=20),
            first_ad_allowed_at=datetime.utcnow() - timedelta(days=5),
        )
        test_db.add_all([binding, first_membership, second_membership])
        test_db.add_all(
            [
                GroupAdProfile(
                    group_id=group.id,
                    telegram_group_id=group.group_id,
                    ad_policy_mode=GroupAdPolicyMode.SOFT_AD_ALLOWED.value,
                    ad_policy_confidence=100,
                    ad_policy_source="manual",
                    ad_policy_verified_at=datetime.utcnow() - timedelta(days=5),
                    ad_policy_expires_at=datetime.utcnow() + timedelta(days=30),
                    ad_tier=GroupAdTier.TRIAL.value,
                    daily_capacity=1,
                )
                for group in (first_group, second_group)
            ]
        )
        await test_db.commit()

        service = AcquisitionAutomationService(db=test_db)
        service._sync_account_pool = AsyncMock()
        service._send_ad = AsyncMock(side_effect=[RuntimeError("can't write in this chat"), 9001])
        service._handle_group_control_ad_failure = AsyncMock()
        service._ad_account_throttle_skip_reason = AsyncMock(return_value=None)
        service._ad_dynamic_daily_limit = AsyncMock(return_value=2)
        service._ad_dynamic_run_limit = AsyncMock(return_value=2)
        service._maybe_send_ad_interaction = AsyncMock(return_value=None)
        service._reserve_group_daily_delivery_slot = AsyncMock(return_value=(True, "reserved", "test-slot"))
        service._release_group_daily_delivery_slot = AsyncMock()
        service._schedule_next_ad_delivery_after_success = AsyncMock()
        monkeypatch.setattr(acquisition_automation, "get_ad_delivery_throttle_settings", lambda: {"enabled": False})

        result = await service.run_ad_delivery(max_deliveries=2)

        assert result["failed"] == 1
        assert result["succeeded"] == 1
        assert service._send_ad.await_count == 2
        logs = (await test_db.execute(select(AdDeliveryLog).order_by(AdDeliveryLog.id))).scalars().all()
        assert [log.status for log in logs] == [DeliveryStatus.FAILED.value, DeliveryStatus.SUCCESS.value]

    @pytest.mark.asyncio
    async def test_choose_delivery_creative_uses_existing_variant_pool(self, test_db):
        account = TelegramAccount(
            phone="+15550000003",
            identifier="+15550000003",
            session_name="ad_variant_pool_account",
            account_type=AccountType.PROMOTER,
            status=AccountStatus.ONLINE,
            is_active=True,
        )
        campaign = AdCampaign(
            name="Variant Pool Campaign",
            enabled=True,
            status="active",
            send_mode=AdSendMode.INTERVAL.value,
            target_group_levels='["B"]',
        )
        repeated = AdCreative(
            name="Repeated",
            content="稳定高速节点，点击 {{link_url}} 立即体验",
            creative_type="text",
            link_url="https://example.com",
            enabled=True,
            weight=100,
        )
        fresh = AdCreative(
            name="Fresh",
            content="新手也能快速上手，教程和节点都准备好了 {{link_url}}",
            creative_type="text",
            link_url="https://example.com",
            enabled=True,
            weight=100,
        )
        group = Group(group_id=3333, title="Target Group", level=GroupLevel.B, status="active")
        test_db.add_all([account, campaign, repeated, fresh, group])
        await test_db.flush()
        binding = AccountAdBinding(account_id=account.id, ad_campaign_id=campaign.id, creative_id=repeated.id, enabled=True)
        test_db.add_all(
            [
                binding,
                AccountAdBinding(account_id=account.id, ad_campaign_id=campaign.id, creative_id=fresh.id, enabled=True),
                AdDeliveryLog(
                    account_id=account.id,
                    telegram_group_id=group.group_id,
                    ad_campaign_id=campaign.id,
                    creative_id=repeated.id,
                    status=DeliveryStatus.SUCCESS.value,
                    sent_at=datetime.utcnow() - timedelta(hours=1),
                ),
            ]
        )
        await test_db.commit()
        await test_db.refresh(binding)

        service = AcquisitionAutomationService(db=test_db)
        chosen = await service._choose_delivery_creative(binding, group.group_id)

        assert chosen is not None
        assert chosen.id == fresh.id

    @pytest.mark.asyncio
    async def test_ad_delivery_dedupes_bindings_but_keeps_creative_pool(self, test_db):
        account = TelegramAccount(
            phone="+15550000005",
            identifier="+15550000005",
            session_name="ad_dedupe_binding_account",
            account_type=AccountType.PROMOTER,
            status=AccountStatus.ONLINE,
            is_active=True,
        )
        campaign = AdCampaign(
            name="Deduped Binding Campaign",
            enabled=True,
            status="active",
            send_mode=AdSendMode.INTERVAL.value,
            target_group_levels='["B"]',
        )
        first = AdCreative(name="First", content="first", creative_type="text", enabled=True, weight=100)
        second = AdCreative(name="Second", content="second", creative_type="text", enabled=True, weight=100)
        test_db.add_all([account, campaign, first, second])
        await test_db.flush()
        test_db.add_all(
            [
                AccountAdBinding(
                    account_id=account.id,
                    ad_campaign_id=campaign.id,
                    creative_id=first.id,
                    enabled=True,
                    priority=100,
                ),
                AccountAdBinding(
                    account_id=account.id,
                    ad_campaign_id=campaign.id,
                    creative_id=second.id,
                    enabled=True,
                    priority=50,
                ),
            ]
        )
        await test_db.commit()

        service = AcquisitionAutomationService(db=test_db)
        bindings = await service._list_enabled_ad_bindings()

        assert len(bindings) == 1
        assert bindings[0].creative_id == first.id
        pool = await service._creative_pool_for_binding(bindings[0])
        assert {item.id for item in pool} == {first.id, second.id}

    @pytest.mark.asyncio
    async def test_choose_delivery_creative_generates_and_binds_ai_variants(self, test_db, monkeypatch):
        class FakeLLM:
            async def generate(self, *args, **kwargs):
                return "更适合新手的稳定节点方案，点击 {{link_url}} 了解\n多平台都能用的轻量套餐，先试用再决定 {{link_url}}"

        account = TelegramAccount(
            phone="+15550000004",
            identifier="+15550000004",
            session_name="ad_ai_variant_account",
            account_type=AccountType.PROMOTER,
            status=AccountStatus.ONLINE,
            is_active=True,
        )
        campaign = AdCampaign(
            name="AI Variant Campaign",
            enabled=True,
            status="active",
            send_mode=AdSendMode.INTERVAL.value,
            target_group_levels='["B"]',
        )
        seed = AdCreative(
            name="Seed",
            content="稳定高速节点，点击 {{link_url}} 立即体验",
            creative_type="text",
            link_url="https://example.com",
            enabled=True,
        )
        test_db.add_all([account, campaign, seed])
        await test_db.flush()
        binding = AccountAdBinding(account_id=account.id, ad_campaign_id=campaign.id, creative_id=seed.id, enabled=True)
        test_db.add(binding)
        await test_db.commit()
        await test_db.refresh(binding)

        service = AcquisitionAutomationService(db=test_db)
        monkeypatch.setattr(service, "_ad_creative_llm", lambda: FakeLLM())

        chosen = await service._choose_delivery_creative(binding, 4444)

        assert chosen is not None
        creatives = (await test_db.execute(select(AdCreative).order_by(AdCreative.id))).scalars().all()
        bindings = (await test_db.execute(select(AccountAdBinding).order_by(AccountAdBinding.id))).scalars().all()
        assert len(creatives) >= 3
        assert len(bindings) >= 3
        assert any(item.name.startswith("AI变体-AI Variant Campaign") for item in creatives)

    def test_generated_ad_creative_parser_rejects_html_response(self):
        response = """<!doctype html>
<html lang="zh-CN">
<head>
<title>错误页面</title>
</head>
<body>请访问 https://example.com</body>
</html>"""

        parsed = AcquisitionAutomationService._parse_generated_ad_creatives(response)

        assert parsed == []
        assert not AcquisitionAutomationService._is_valid_generated_ad_creative("<head>")
        assert not AcquisitionAutomationService._is_valid_generated_ad_creative('<html lang="zh-CN">')


@pytest.mark.asyncio
async def test_account_asset_warmup_days_never_bypasses_seven_day_floor(monkeypatch):
    service = AcquisitionAutomationService.__new__(AcquisitionAutomationService)
    service.db = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(asset_tier="year_2")))

    async def low_warmup_policy(_db):
        return {
            "enabled": True,
            "tiers": {
                "year_2": {"warmup_days": 3},
            },
        }

    monkeypatch.setattr(
        acquisition_automation,
        "get_account_asset_policy_settings",
        low_warmup_policy,
    )

    assert await service._account_asset_warmup_days(1, 0) == 7
    assert await service._account_asset_warmup_days(1, 15) == 15