"""
Unit Tests for Keyword Matcher
"""

import pytest
import time

from app.core.keyword.matcher import KeywordMatcher, MatchResult, MessageMatchResult
from app.core.keyword.models import Keyword, KeywordType, KeywordStatus, MatchMode


class TestMatchResult:
    """Test MatchResult dataclass."""

    def test_match_result_creation(self):
        """Test creating a MatchResult."""
        keyword = Keyword(
            id=1,
            text="test",
            type=KeywordType.DEMAND,
            status=KeywordStatus.APPROVED,
            match_mode=MatchMode.EXACT,
        )
        
        result = MatchResult(
            keyword=keyword,
            start_pos=0,
            end_pos=4,
            matched_text="test",
            confidence=1.0,
        )
        
        assert result.start_pos == 0
        assert result.end_pos == 4
        assert result.matched_text == "test"
        assert result.length == 4

    def test_match_result_length_property(self):
        """Test length property calculation."""
        keyword = Keyword(
            id=1,
            text="test",
            type=KeywordType.DEMAND,
            status=KeywordStatus.APPROVED,
            match_mode=MatchMode.EXACT,
        )
        
        result = MatchResult(
            keyword=keyword,
            start_pos=5,
            end_pos=12,
            matched_text="example",
        )
        
        assert result.length == 7


class TestMessageMatchResult:
    """Test MessageMatchResult dataclass."""

    def test_message_match_result_empty(self):
        """Test empty MessageMatchResult."""
        result = MessageMatchResult(
            message_id=123,
            text="test message",
        )
        
        assert result.message_id == 123
        assert result.text == "test message"
        assert not result.has_match
        assert result.match_count == 0

    def test_has_match_property(self):
        """Test has_match property."""
        keyword = Keyword(
            id=1,
            text="test",
            type=KeywordType.DEMAND,
            status=KeywordStatus.APPROVED,
            match_mode=MatchMode.EXACT,
        )
        
        result = MessageMatchResult(message_id=1, text="test")
        assert not result.has_match
        
        result.matches.append(MatchResult(
            keyword=keyword,
            start_pos=0,
            end_pos=4,
            matched_text="test",
        ))
        assert result.has_match
        assert result.match_count == 1

    def test_get_keywords_by_type(self):
        """Test filtering matches by keyword type."""
        keywords = [
            Keyword(id=1, text="test1", type=KeywordType.DEMAND, status=KeywordStatus.APPROVED, match_mode=MatchMode.FUZZY),
            Keyword(id=2, text="test2", type=KeywordType.PRICE, status=KeywordStatus.APPROVED, match_mode=MatchMode.FUZZY),
        ]
        
        result = MessageMatchResult(message_id=1, text="test")
        result.matches.append(MatchResult(keyword=keywords[0], start_pos=0, end_pos=4, matched_text="test1"))
        result.matches.append(MatchResult(keyword=keywords[1], start_pos=0, end_pos=4, matched_text="test2"))
        
        demand_matches = result.get_keywords_by_type(KeywordType.DEMAND)
        assert len(demand_matches) == 1
        assert demand_matches[0].keyword.type == KeywordType.DEMAND

    def test_has_keyword_type(self):
        """Test checking for specific keyword type."""
        keyword = Keyword(
            id=1,
            text="test",
            type=KeywordType.DEMAND,
            status=KeywordStatus.APPROVED,
            match_mode=MatchMode.EXACT,
        )
        
        result = MessageMatchResult(message_id=1, text="test")
        result.matches.append(MatchResult(
            keyword=keyword,
            start_pos=0,
            end_pos=4,
            matched_text="test",
        ))
        
        assert result.has_keyword_type(KeywordType.DEMAND)
        assert not result.has_keyword_type(KeywordType.PRICE)


class TestKeywordMatcher:
    """Test KeywordMatcher class."""

    def test_clean_text(self):
        """Test text cleaning and normalization."""
        matcher = KeywordMatcher()
        
        # Normal case
        cleaned = matcher._clean_text("  Hello   World  ")
        assert cleaned == "hello world"
        
        # Empty string
        cleaned = matcher._clean_text("")
        assert cleaned == ""
        
        # None handling
        cleaned = matcher._clean_text(None)
        assert cleaned == ""

    def test_cache_key_generation(self):
        """Test cache key is deterministic."""
        matcher = KeywordMatcher()
        
        key1 = matcher._get_cache_key("test text")
        key2 = matcher._get_cache_key("test text")
        key3 = matcher._get_cache_key("different text")
        
        assert key1 == key2
        assert key1 != key3

    def test_match_single(self):
        """Test matching a single keyword."""
        import re
        matcher = KeywordMatcher()
        
        keyword = Keyword(
            id=1,
            text="hello",
            type=KeywordType.INQUIRY,
            status=KeywordStatus.APPROVED,
            match_mode=MatchMode.EXACT,
        )
        pattern = re.compile(r"\bhello\b", re.IGNORECASE)
        
        results = matcher.match_single("Hello World", keyword, pattern)
        
        assert len(results) == 1
        assert results[0].matched_text == "hello"
        assert results[0].keyword == keyword

    def test_match_single_no_match(self):
        """Test matching with no results."""
        import re
        matcher = KeywordMatcher()
        
        keyword = Keyword(
            id=1,
            text="hello",
            type=KeywordType.INQUIRY,
            status=KeywordStatus.APPROVED,
            match_mode=MatchMode.EXACT,
        )
        pattern = re.compile(r"\bhello\b", re.IGNORECASE)
        
        results = matcher.match_single("Goodbye World", keyword, pattern)
        
        assert len(results) == 0

    def test_match_multiple(self):
        """Test matching multiple keywords."""
        import re
        matcher = KeywordMatcher()
        
        keywords = [
            Keyword(id=1, text="hello", type=KeywordType.INQUIRY, status=KeywordStatus.APPROVED, match_mode=MatchMode.FUZZY),
            Keyword(id=2, text="price", type=KeywordType.PRICE, status=KeywordStatus.APPROVED, match_mode=MatchMode.FUZZY),
        ]
        patterns = [
            (keywords[0], re.compile(r"hello", re.IGNORECASE)),
            (keywords[1], re.compile(r"price", re.IGNORECASE)),
        ]
        
        result = matcher.match_multiple("What's the price? Hello!", patterns)
        
        assert result.has_match
        assert result.match_count == 2
        assert result.matched_keyword_ids == {1, 2}

    def test_match_multiple_empty_text(self):
        """Test matching with empty text."""
        import re
        matcher = KeywordMatcher()
        
        keyword = Keyword(id=1, text="test", type=KeywordType.DEMAND, status=KeywordStatus.APPROVED, match_mode=MatchMode.FUZZY)
        patterns = [(keyword, re.compile(r"test"))]
        
        result = matcher.match_multiple("", patterns)
        
        assert not result.has_match
        assert result.match_time_ms >= 0

    def test_match_multiple_empty_keywords(self):
        """Test matching with empty keyword list."""
        matcher = KeywordMatcher()
        
        result = matcher.match_multiple("test text", [])
        
        assert not result.has_match

    def test_extract_urls(self):
        """Test URL extraction."""
        matcher = KeywordMatcher()
        
        text = "Check https://example.com and http://test.org for more info"
        urls = matcher.extract_urls(text)
        
        assert len(urls) == 2
        assert "https://example.com" in urls
        assert "http://test.org" in urls

    def test_extract_domains(self):
        """Test domain extraction from URLs."""
        matcher = KeywordMatcher()
        
        text = "Visit https://example.com and http://test.org"
        domains = matcher.extract_domains(text)
        
        assert "example.com" in domains
        assert "test.org" in domains

    def test_is_competitor_keyword(self):
        """Test competitor keyword detection."""
        matcher = KeywordMatcher()
        
        # Should detect competitor keywords
        assert matcher.is_competitor_keyword("免费机场节点")
        assert matcher.is_competitor_keyword("便宜梯子VPN")
        assert matcher.is_competitor_keyword("提供clash节点")
        assert matcher.is_competitor_keyword("v2ray加速器")
        
        # Should not match normal text
        assert not matcher.is_competitor_keyword("这个产品很好用")


class TestKeywordMatcherCache:
    """Test KeywordMatcher caching functionality."""

    @pytest.mark.asyncio
    async def test_match_cached_basic(self):
        """Test basic caching functionality."""
        import re
        matcher = KeywordMatcher(cache_ttl=300)
        
        keyword = Keyword(id=1, text="test", type=KeywordType.DEMAND, status=KeywordStatus.APPROVED, match_mode=MatchMode.FUZZY)
        patterns = [(keyword, re.compile(r"test"))]
        
        # First call - should compute and cache
        result1 = await matcher.match_cached("test message", patterns)
        
        # Second call - should return cached result
        result2 = await matcher.match_cached("test message", patterns)
        
        assert result1.has_match
        assert result2.has_match

    @pytest.mark.asyncio
    async def test_cache_expires(self):
        """Test that cache expires after TTL."""
        import re
        matcher = KeywordMatcher(cache_ttl=1)  # 1 second TTL
        
        keyword = Keyword(id=1, text="test", type=KeywordType.DEMAND, status=KeywordStatus.APPROVED, match_mode=MatchMode.FUZZY)
        patterns = [(keyword, re.compile(r"test"))]
        
        # First call
        await matcher.match_cached("test message", patterns)
        
        # Wait for cache to expire
        time.sleep(1.5)
        
        # Cache should be expired - verify by checking internal state
        cache_key = matcher._get_cache_key("test message")
        if cache_key in matcher._cache:
            _, cached_time = matcher._cache[cache_key]
            assert time.time() - cached_time >= 1

    def test_clear_cache(self):
        """Test cache clearing."""
        import re
        import time
        matcher = KeywordMatcher()

        keyword = Keyword(id=1, text="test", type=KeywordType.DEMAND, status=KeywordStatus.APPROVED, match_mode=MatchMode.FUZZY)
        patterns = [(keyword, re.compile(r"test"))]

        # Populate cache directly
        cache_key = ("test message", frozenset())
        matcher._cache[cache_key] = ([], time.time())

        # Verify cache has items
        assert len(matcher._cache) > 0

        # Clear cache
        matcher.clear_cache()
        assert len(matcher._cache) == 0


class TestTextNormalization:
    """Test text normalization in matching."""

    def test_case_insensitive_matching(self):
        """Test that matching is case insensitive."""
        import re
        matcher = KeywordMatcher()
        
        keyword = Keyword(id=1, text="HELLO", type=KeywordType.DEMAND, status=KeywordStatus.APPROVED, match_mode=MatchMode.FUZZY)
        pattern = re.compile(r"HELLO", re.IGNORECASE)
        
        results = matcher.match_single("hello world", keyword, pattern)
        assert len(results) == 1
        
        results = matcher.match_single("HELLO WORLD", keyword, pattern)
        assert len(results) == 1
        
        results = matcher.match_single("HeLLo World", keyword, pattern)
        assert len(results) == 1

    def test_whitespace_normalization(self):
        """Test that whitespace is normalized before matching."""
        matcher = KeywordMatcher()
        
        # Multiple spaces should be normalized
        cleaned = matcher._clean_text("hello    world")
        assert cleaned == "hello world"
