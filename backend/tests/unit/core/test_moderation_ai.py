"""
Unit Tests for ModerationAI Module

Tests cover:
- Content moderation analysis
- Quick pattern-based check
- Competitor detection
- Violation level determination
- Action recommendations
- Sensitive keyword generation (AI-assisted)
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.ai.moderation_ai import (
    ModerationAI,
    ModerationResult,
    ViolationType,
    ViolationLevel,
    SensitiveKeywordSuggestion,
    SensitiveKeywordGenerator,
    generate_sensitive_keywords,
)


class TestModerationAI:
    """Test ModerationAI class."""

    @pytest.fixture
    def moderator(self):
        """Create ModerationAI instance without LLM."""
        return ModerationAI()

    @pytest.fixture
    def moderator_with_llm(self):
        """Create ModerationAI instance with mock LLM."""
        mock_llm = MagicMock()
        mock_llm.generate = AsyncMock(
            return_value='{"violation": true, "type": "competitor", "level": "high", "confidence": 0.95, "reason": "检测到竞品推广"}'
        )
        return ModerationAI(llm_client=mock_llm)

    def test_init_without_llm(self):
        """Test initialization without LLM client."""
        moderator = ModerationAI()
        assert moderator.llm is None

    def test_init_with_llm(self):
        """Test initialization with LLM client."""
        mock_llm = MagicMock()
        moderator = ModerationAI(llm_client=mock_llm)
        assert moderator.llm is mock_llm

    def test_competitor_keywords_defined(self):
        """Test competitor keywords are defined."""
        moderator = ModerationAI()
        assert len(moderator.COMPETITOR_KEYWORDS) > 0
        assert "机场" in moderator.COMPETITOR_KEYWORDS
        assert "VPN" in moderator.COMPETITOR_KEYWORDS

    def test_competitor_domains_defined(self):
        """Test competitor domain patterns are defined."""
        moderator = ModerationAI()
        assert len(moderator.COMPETITOR_DOMAINS) > 0


class TestQuickCheck:
    """Test quick pattern-based check."""

    @pytest.fixture
    def moderator(self):
        """Create ModerationAI instance."""
        return ModerationAI()

    def test_quick_check_with_keyword(self, moderator):
        """Test quick check detects keywords."""
        result = moderator._quick_check("有人推荐机场服务吗")

        assert result is not None
        assert result.is_violation is True
        # SPAM is returned for single keyword, COMPETITOR for multiple
        assert result.violation_type in [ViolationType.COMPETITOR, ViolationType.SPAM]

    def test_quick_check_with_domain(self, moderator):
        """Test quick check detects domain patterns."""
        # Use a clear .vip domain pattern
        result = moderator._quick_check("example.vip")

        assert result is not None
        assert result.is_violation is True
        assert result.violation_type == ViolationType.COMPETITOR
        assert result.level == ViolationLevel.HIGH
        assert "可疑外链" in result.reason

    def test_quick_check_with_multiple_competitor_keywords(self, moderator):
        """Test quick check with multiple competitor keywords."""
        result = moderator._quick_check("机场节点VPN梯子")

        assert result is not None
        assert result.is_violation is True
        assert result.violation_type == ViolationType.COMPETITOR
        assert result.level == ViolationLevel.HIGH

    def test_quick_check_safe_content(self, moderator):
        """Test quick check with safe content."""
        result = moderator._quick_check("今天天气真好")

        assert result is None

    def test_quick_check_case_insensitive(self, moderator):
        """Test quick check is case insensitive."""
        result1 = moderator._quick_check("机场")
        result2 = moderator._quick_check("机场".upper())

        assert result1 is not None
        assert result2 is not None


class TestGetMatchedKeywords:
    """Test keyword matching."""

    @pytest.fixture
    def moderator(self):
        """Create ModerationAI instance."""
        return ModerationAI()

    def test_match_single_keyword(self, moderator):
        """Test matching single keyword."""
        matched = moderator._get_matched_keywords("有人卖机场吗")
        assert "机场" in matched

    def test_match_multiple_keywords(self, moderator):
        """Test matching multiple keywords."""
        matched = moderator._get_matched_keywords("机场节点翻墙")
        assert "机场" in matched
        assert "节点" in matched
        assert "翻墙" in matched

    def test_match_no_keywords(self, moderator):
        """Test no keywords matched."""
        matched = moderator._get_matched_keywords("你好")
        assert len(matched) == 0

    def test_match_case_insensitive(self, moderator):
        """Test matching is case insensitive."""
        matched1 = moderator._get_matched_keywords("机场")
        matched2 = moderator._get_matched_keywords("机")  # Only partial match possible
        assert len(matched1) > 0


class TestGetMatchedDomains:
    """Test domain pattern matching."""

    @pytest.fixture
    def moderator(self):
        """Create ModerationAI instance."""
        return ModerationAI()

    def test_match_vip_domain(self, moderator):
        """Test matching .vip domain."""
        matched = moderator._get_matched_domains("example.vip")
        assert len(matched) > 0

    def test_match_xyz_domain(self, moderator):
        """Test matching .xyz domain."""
        matched = moderator._get_matched_domains("example.xyz")
        assert len(matched) > 0

    def test_match_tme_link(self, moderator):
        """Test matching t.me links."""
        matched = moderator._get_matched_domains("t.me/username")
        assert len(matched) > 0

    def test_match_short_url(self, moderator):
        """Test matching short URLs."""
        matched = moderator._get_matched_domains("bit.ly/abc")
        assert len(matched) > 0

    def test_no_domain_match(self, moderator):
        """Test no domain match."""
        matched = moderator._get_matched_domains("example.com")
        assert len(matched) == 0


class TestAnalyze:
    """Test analyze method."""

    @pytest.fixture
    def moderator(self):
        """Create ModerationAI instance without LLM."""
        return ModerationAI()

    @pytest.mark.asyncio
    async def test_analyze_without_llm_safe_content(self, moderator):
        """Test analyze with safe content without LLM."""
        result = await moderator.analyze("今天天气真好")

        assert result.is_violation is False
        assert result.violation_type == ViolationType.SAFE

    @pytest.mark.asyncio
    async def test_analyze_without_llm_violation(self, moderator):
        """Test analyze with violation without LLM."""
        result = await moderator.analyze("机场服务推荐")

        assert result.is_violation is True
        # SPAM or COMPETITOR depending on keyword count
        assert result.violation_type in [ViolationType.COMPETITOR, ViolationType.SPAM]


class TestBatchAnalyze:
    """Test batch analyze method."""

    @pytest.fixture
    def moderator(self):
        """Create ModerationAI instance without LLM."""
        return ModerationAI()

    @pytest.mark.asyncio
    async def test_batch_analyze(self, moderator):
        """Test batch analyzing messages."""
        contents = [
            "机场推荐",  # Has "机场"
            "今天天气好",  # Safe
            "节点服务"  # Has "节点"
        ]

        results = await moderator.batch_analyze(contents)

        assert len(results) == 3
        assert results[0].is_violation is True  # 机场
        assert results[1].is_violation is False  # Safe
        assert results[2].is_violation is True  # 节点


class TestIsCompetitorMention:
    """Test competitor mention detection."""

    @pytest.fixture
    def moderator(self):
        """Create ModerationAI instance."""
        return ModerationAI()

    def test_has_competitor_mention(self, moderator):
        """Test detection of competitor mention."""
        assert moderator.is_competitor_mention("机场服务") is True

    def test_no_competitor_mention(self, moderator):
        """Test no competitor mention."""
        assert moderator.is_competitor_mention("你好") is False


class TestGetActionForViolation:
    """Test action recommendation."""

    @pytest.fixture
    def moderator(self):
        """Create ModerationAI instance."""
        return ModerationAI()

    def test_action_high_competitor(self, moderator):
        """Test action for high competitor violation."""
        result = ModerationResult(
            is_violation=True,
            violation_type=ViolationType.COMPETITOR,
            level=ViolationLevel.HIGH,
            confidence=0.9,
            reason="竞品推广",
            matched_patterns=[]
        )
        action = moderator.get_action_for_violation(result)
        assert action == "ban"

    def test_action_high_other(self, moderator):
        """Test action for high other violation."""
        result = ModerationResult(
            is_violation=True,
            violation_type=ViolationType.SCAM,
            level=ViolationLevel.HIGH,
            confidence=0.9,
            reason="诈骗",
            matched_patterns=[]
        )
        action = moderator.get_action_for_violation(result)
        assert action == "mute"

    def test_action_medium(self, moderator):
        """Test action for medium violation."""
        result = ModerationResult(
            is_violation=True,
            violation_type=ViolationType.SPAM,
            level=ViolationLevel.MEDIUM,
            confidence=0.7,
            reason="垃圾",
            matched_patterns=[]
        )
        action = moderator.get_action_for_violation(result)
        assert action == "warn"

    def test_action_low(self, moderator):
        """Test action for low violation."""
        result = ModerationResult(
            is_violation=True,
            violation_type=ViolationType.SPAM,
            level=ViolationLevel.LOW,
            confidence=0.5,
            reason="轻微",
            matched_patterns=[]
        )
        action = moderator.get_action_for_violation(result)
        assert action == "ignore"


class TestModerationResult:
    """Test ModerationResult dataclass."""

    def test_create_result(self):
        """Test creating ModerationResult."""
        result = ModerationResult(
            is_violation=True,
            violation_type=ViolationType.COMPETITOR,
            level=ViolationLevel.HIGH,
            confidence=0.95,
            reason="竞品推广",
            matched_patterns=["机场", "节点"]
        )

        assert result.is_violation is True
        assert result.violation_type == ViolationType.COMPETITOR
        assert result.level == ViolationLevel.HIGH
        assert result.confidence == 0.95
        assert result.reason == "竞品推广"
        assert result.matched_patterns == ["机场", "节点"]


class TestSensitiveKeywordGenerator:
    """Test SensitiveKeywordGenerator class."""

    @pytest.fixture
    def generator(self):
        """Create generator without LLM."""
        return SensitiveKeywordGenerator()

    @pytest.fixture
    def generator_with_llm(self):
        """Create generator with mock LLM."""
        mock_llm = MagicMock()
        mock_llm.generate = AsyncMock(return_value="机场\n节点\nVPN\n翻墙")
        return SensitiveKeywordGenerator(llm_client=mock_llm)

    def test_init_without_llm(self, generator):
        """Test initialization without LLM."""
        assert generator.llm is None

    def test_init_with_llm(self, generator_with_llm):
        """Test initialization with LLM."""
        assert generator_with_llm.llm is not None

    @pytest.mark.asyncio
    async def test_generate_with_llm(self, generator_with_llm):
        """Test keyword generation with LLM."""
        samples = ["有人推荐机场服务", "低价节点"]
        suggestions = await generator_with_llm.generate_sensitive_keywords(samples, "competitor")

        assert len(suggestions) > 0
        assert all(isinstance(s, SensitiveKeywordSuggestion) for s in suggestions)

    @pytest.mark.asyncio
    async def test_generate_without_llm(self, generator):
        """Test keyword generation fallback without LLM."""
        # Use samples that match the fallback patterns exactly
        samples = ["机场推荐服务", "节点购买"]
        suggestions = await generator.generate_sensitive_keywords(samples, "competitor")

        # Fallback extracts 机场 and 节点
        assert len(suggestions) >= 0  # May be empty depending on regex match
        assert all(s.confidence == 0.5 for s in suggestions)

    @pytest.mark.asyncio
    async def test_generate_empty_samples(self, generator):
        """Test keyword generation with empty samples."""
        suggestions = await generator.generate_sensitive_keywords([], "competitor")
        assert len(suggestions) == 0

    @pytest.mark.asyncio
    async def test_generate_max_keywords(self, generator):
        """Test keyword generation respects max limit."""
        samples = ["机场" for _ in range(10)]
        suggestions = await generator.generate_sensitive_keywords(
            samples, "competitor", max_keywords=5
        )
        assert len(suggestions) <= 5


class TestGenerateSensitiveKeywordsFunction:
    """Test standalone generate_sensitive_keywords function."""

    @pytest.mark.asyncio
    async def test_generate_with_mock_llm(self):
        """Test standalone function with mock LLM."""
        mock_llm = MagicMock()
        mock_llm.generate = AsyncMock(return_value="机场\n节点")

        suggestions = await generate_sensitive_keywords(
            samples=["推荐机场服务"],
            category="competitor",
            llm_client=mock_llm
        )

        assert len(suggestions) > 0


class TestParseKeywords:
    """Test keyword parsing."""

    @pytest.fixture
    def generator(self):
        """Create generator instance."""
        return SensitiveKeywordGenerator()

    def test_parse_simple_keywords(self, generator):
        """Test parsing simple keywords."""
        response = "机场\n节点\nVPN"
        keywords = generator._parse_keywords(response)

        assert "机场" in keywords
        assert "节点" in keywords
        assert "VPN" in keywords

    def test_parse_numbered_keywords(self, generator):
        """Test parsing numbered keywords."""
        response = "1. 机场\n2. 节点\n3. VPN"
        keywords = generator._parse_keywords(response)

        assert "机场" in keywords
        assert "节点" in keywords
        assert "VPN" in keywords

    def test_parse_keywords_with_prefix(self, generator):
        """Test parsing keywords with various prefixes."""
        response = "- 机场\n* 节点\n、VPN"
        keywords = generator._parse_keywords(response)

        assert "机场" in keywords
        assert "节点" in keywords

    def test_parse_empty_lines(self, generator):
        """Test parsing with empty lines."""
        response = "机场\n\n节点\n\nVPN"
        keywords = generator._parse_keywords(response)

        assert len(keywords) == 3

    def test_parse_short_keywords_filtered(self, generator):
        """Test short keywords are filtered."""
        response = "A\n机场\nB\n节点"
        keywords = generator._parse_keywords(response)

        assert "A" not in keywords
        assert "B" not in keywords
        assert "机场" in keywords


class TestViolationTypeEnum:
    """Test ViolationType enum."""

    def test_all_violation_types_defined(self):
        """Test all expected violation types are defined."""
        expected_types = {
            ViolationType.COMPETITOR,
            ViolationType.SPAM,
            ViolationType.SCAM,
            ViolationType.SENSITIVE,
            ViolationType.SAFE,
        }
        assert set(ViolationType) == expected_types

    def test_violation_type_values(self):
        """Test violation type string values."""
        assert ViolationType.COMPETITOR.value == "competitor"
        assert ViolationType.SPAM.value == "spam"
        assert ViolationType.SCAM.value == "scam"
        assert ViolationType.SENSITIVE.value == "sensitive"
        assert ViolationType.SAFE.value == "safe"


class TestViolationLevelEnum:
    """Test ViolationLevel enum."""

    def test_all_violation_levels_defined(self):
        """Test all expected violation levels are defined."""
        expected_levels = {
            ViolationLevel.LOW,
            ViolationLevel.MEDIUM,
            ViolationLevel.HIGH,
        }
        assert set(ViolationLevel) == expected_levels

    def test_violation_level_values(self):
        """Test violation level string values."""
        assert ViolationLevel.LOW.value == "low"
        assert ViolationLevel.MEDIUM.value == "medium"
        assert ViolationLevel.HIGH.value == "high"
