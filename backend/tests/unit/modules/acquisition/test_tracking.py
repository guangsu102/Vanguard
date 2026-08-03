"""
Tests for Tracking Module
"""

import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timedelta

from app.modules.acquisition.tracking.url_builder import URLBuilder
from app.modules.acquisition.tracking.attribution import AttributionAnalyzer, TouchPoint
from app.modules.acquisition.private_msg.welcome import WelcomeGenerator
from app.modules.acquisition.keyword_trigger.matcher import KeywordMatcher, TriggerMatch
from app.modules.acquisition.models import TriggerType


class TestURLBuilder:
    """Tests for URLBuilder."""

    def setup_method(self):
        """Set up test fixtures."""
        self.builder = URLBuilder(base_url="https://xboard.com")

    def test_build_tracking_url(self):
        """Test building a basic tracking URL."""
        url = asyncio.run(
            self.builder.build_tracking_url(
                tracking_code="acq_123_abc",
                source_type="tg_group",
            )
        )

        assert "https://xboard.com/register" in url
        assert "ref=acq_123_abc" in url
        assert "source=tg_group" in url

    def test_build_tracking_url_with_params(self):
        """Test building tracking URL with additional parameters."""
        url = asyncio.run(
            self.builder.build_tracking_url(
                tracking_code="acq_123",
                source_type="tg_group",
                campaign="summer_promo",
                group_id=123456,
                keyword="vpn",
                bot_id="bot_001",
            )
        )

        assert "campaign=summer_promo" in url
        assert "group_id=123456" in url
        assert "keyword=vpn" in url
        assert "bot_id=bot_001" in url

    def test_build_invite_url(self):
        """Test building an invite URL."""
        url = asyncio.run(
            self.builder.build_invite_url(user_id=12345, source="group_123")
        )

        assert "https://xboard.com/register" in url
        assert "source=tg_invite" in url

    def test_validate_tracking_code_valid(self):
        """Test validating valid tracking codes."""
        assert self.builder.validate_tracking_code("acq_123_abc") is True
        assert self.builder.validate_tracking_code("inv_123_group") is True

    def test_validate_tracking_code_invalid(self):
        """Test validating invalid tracking codes."""
        assert self.builder.validate_tracking_code("") is False
        assert self.builder.validate_tracking_code("invalid") is False
        assert self.builder.validate_tracking_code("x" * 150) is False

    def test_parse_tracking_params(self):
        """Test parsing tracking parameters from URL."""
        url = "https://xboard.com/register?source=tg_group&ref=acq_123&campaign=test&group_id=456"

        params = asyncio.run(
            self.builder.parse_tracking_params(url)
        )

        assert params["source_type"] == "tg_group"
        assert params["tracking_code"] == "acq_123"
        assert params["campaign"] == "test"
        assert params["group_id"] == 456

    def test_encrypt_decrypt(self):
        """Test encryption and decryption of tracking codes."""
        builder = URLBuilder(
            base_url="https://xboard.com",
            config=MagicMock(
                encryption_key="test-key-123",
                encryption_enabled=True,
                base_url="https://xboard.com",
            ),
        )

        original = "acq_123_xyz"
        encrypted = builder._encrypt(original)
        decrypted = builder._decrypt(encrypted)

        assert encrypted != original
        assert decrypted == original

    def test_generate_short_code(self):
        """Test generating short codes."""
        short = asyncio.run(
            self.builder.generate_short_code("acq_123_abcdefgh")
        )

        assert len(short) == 8
        assert short.isalnum()

    def test_build_deep_link(self):
        """Test building deep links."""
        link = asyncio.run(
            self.builder.build_deep_link(
                action="register",
                params={"campaign": "test"},
            )
        )

        assert "xboard://" in link
        assert "register" in link


class TestWelcomeGenerator:
    """Tests for WelcomeGenerator."""

    def test_build_tracking_link_uses_url_builder(self):
        url_builder = AsyncMock()
        url_builder.encryption_enabled = False
        url_builder.build_tracking_url = AsyncMock(return_value="https://xboard.com/register?source=tg_private&ref=inv_1_tg_private")
        generator = WelcomeGenerator(url_builder=url_builder)

        link = asyncio.run(
            generator._build_tracking_link(1, {"source": "tg_private", "campaign": "spring"})
        )

        assert link.startswith("https://xboard.com/register")
        url_builder.build_tracking_url.assert_awaited_once()


class TestKeywordMatcher:
    """Tests for KeywordMatcher history deduplication."""

    @pytest.mark.asyncio
    async def test_filter_by_user_history_dedupes_recent_trigger(self):
        db = AsyncMock()
        keyword_engine = AsyncMock()
        matcher = KeywordMatcher(db=db, keyword_engine=keyword_engine)

        matcher._triggers = {
            1: MagicMock(cooldown_seconds=3600),
        }

        match = TriggerMatch(
            trigger_id=10,
            keyword_id=1,
            keyword_text="vpn",
            trigger_type=TriggerType.KEYWORD,
            matched_text="vpn",
            match_position=0,
        )

        recent_result = MagicMock()
        recent_result.scalar_one_or_none.return_value = 123
        db.execute = AsyncMock(return_value=recent_result)

        filtered = await matcher._filter_by_user_history([match], user_id=99)

        assert filtered == []
        assert db.execute.await_count == 1


class TestAttributionAnalyzer:
    """Tests for AttributionAnalyzer."""

    def test_last_touch_attribution(self):
        """Test last-touch attribution model."""
        analyzer = AttributionAnalyzer(db=None)

        touchpoints = [
            TouchPoint("search", "search", None, None, None, datetime.now() - timedelta(hours=2), False),
            TouchPoint("tg_group", "telegram", "promo", 123, "vpn", datetime.now() - timedelta(hours=1), False),
            TouchPoint("tg_private", "telegram", None, None, None, datetime.now(), True),
        ]

        winner = analyzer._last_touch_attribution(touchpoints)

        assert winner.source == "tg_private"
        assert winner.is_conversion is True

    def test_first_touch_attribution(self):
        """Test first-touch attribution model."""
        analyzer = AttributionAnalyzer(db=None)

        touchpoints = [
            TouchPoint("search", "search", None, None, None, datetime.now() - timedelta(hours=2), False),
            TouchPoint("tg_group", "telegram", "promo", 123, "vpn", datetime.now() - timedelta(hours=1), False),
        ]

        winner = analyzer._first_touch_attribution(touchpoints)

        assert winner.source == "search"

    def test_calculate_channel_weights(self):
        """Test channel weight calculation."""
        analyzer = AttributionAnalyzer(db=None)

        touchpoints = [
            TouchPoint("tg_group", "telegram", None, 1, None, datetime.now(), False),
            TouchPoint("tg_group", "telegram", None, 2, None, datetime.now(), False),
            TouchPoint("search", "search", None, None, None, datetime.now(), False),
        ]

        weights = analyzer._calculate_channel_weights(touchpoints)

        # 2 out of 3 from telegram
        assert weights.get("telegram") == pytest.approx(66.67, rel=0.1)
        # 1 out of 3 from search
        assert weights.get("search") == pytest.approx(33.33, rel=0.1)

    def test_empty_touchpoints(self):
        """Test attribution with empty touchpoints."""
        analyzer = AttributionAnalyzer(db=None)

        winner = analyzer._last_touch_attribution([])
        assert winner is None

        weights = analyzer._calculate_channel_weights([])
        assert weights == {}
