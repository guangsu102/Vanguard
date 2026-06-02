"""
Keyword Matcher Module

Provides message matching functionality with context awareness.

Features:
- Message preprocessing
- Multi-keyword matching
- Context window for related messages
- Match result caching
"""

import re
import time
from dataclasses import dataclass, field
from typing import Optional

import structlog

from app.core.keyword.models import Keyword, KeywordType

logger = structlog.get_logger()


@dataclass
class MatchResult:
    """
    Result of a keyword match.

    Attributes:
        keyword: Matched keyword
        start_pos: Start position of match
        end_pos: End position of match
        matched_text: The matched text
        confidence: Match confidence (0-1)
    """

    keyword: Keyword
    start_pos: int
    end_pos: int
    matched_text: str
    confidence: float = 1.0

    @property
    def length(self) -> int:
        """Get length of match."""
        return self.end_pos - self.start_pos


@dataclass
class MessageMatchResult:
    """
    Complete result of matching a message.

    Attributes:
        message_id: Message ID
        text: Original text
        matches: List of match results
        matched_keywords: Set of matched keyword IDs
        match_time_ms: Time taken to match
    """

    message_id: Optional[int]
    text: str
    matches: list[MatchResult] = field(default_factory=list)
    matched_keyword_ids: set[int] = field(default_factory=set)
    match_time_ms: float = 0.0

    @property
    def has_match(self) -> bool:
        """Check if any matches found."""
        return len(self.matches) > 0

    @property
    def match_count(self) -> int:
        """Get number of matches."""
        return len(self.matches)

    def get_keywords_by_type(self, keyword_type: KeywordType) -> list[MatchResult]:
        """Get matches of specific type."""
        return [m for m in self.matches if m.keyword.type == keyword_type]

    def has_keyword_type(self, keyword_type: KeywordType) -> bool:
        """Check if any matches of specific type."""
        return any(m.keyword.type == keyword_type for m in self.matches)


class KeywordMatcher:
    """
    Keyword matcher for Telegram messages.

    Provides efficient keyword matching with caching and context support.
    """

    def __init__(self, cache_ttl: int = 300):
        """
        Initialize KeywordMatcher.

        Args:
            cache_ttl: Cache TTL in seconds
        """
        self._cache: dict[str, tuple[MessageMatchResult, float]] = {}
        self._cache_ttl = cache_ttl
        self.logger = logger.bind(module="keyword_matcher")

    def clear_cache(self) -> None:
        """Clear the match cache."""
        self._cache.clear()

    def _get_cache_key(self, text: str) -> str:
        """Generate cache key for text."""
        import hashlib
        return hashlib.md5(text.encode()).hexdigest()

    def _clean_text(self, text: str) -> str:
        """
        Clean and normalize text for matching.

        Args:
            text: Raw text

        Returns:
            Cleaned text
        """
        if not text:
            return ""

        cleaned = text.strip()

        cleaned = re.sub(r'\s+', ' ', cleaned)

        cleaned = cleaned.lower()

        return cleaned

    def match_single(
        self,
        text: str,
        keyword: Keyword,
        compiled_pattern: re.Pattern,
    ) -> list[MatchResult]:
        """
        Match a single keyword against text.

        Args:
            text: Text to match
            keyword: Keyword to match
            compiled_pattern: Compiled regex pattern

        Returns:
            List of MatchResult
        """
        matches = []
        cleaned_text = self._clean_text(text)

        try:
            for match in compiled_pattern.finditer(cleaned_text):
                result = MatchResult(
                    keyword=keyword,
                    start_pos=match.start(),
                    end_pos=match.end(),
                    matched_text=match.group(),
                    confidence=1.0,
                )
                matches.append(result)
        except re.error:
            pass

        return matches

    def match_multiple(
        self,
        text: str,
        keywords: list[tuple[Keyword, re.Pattern]],
    ) -> MessageMatchResult:
        """
        Match multiple keywords against text.

        Args:
            text: Text to match
            keywords: List of (Keyword, Pattern) tuples

        Returns:
            MessageMatchResult with all matches
        """
        start_time = time.time()
        result = MessageMatchResult(message_id=None, text=text)

        if not text or not keywords:
            result.match_time_ms = (time.time() - start_time) * 1000
            return result

        cleaned_text = self._clean_text(text)

        for keyword, pattern in keywords:
            try:
                for match in pattern.finditer(cleaned_text):
                    match_result = MatchResult(
                        keyword=keyword,
                        start_pos=match.start(),
                        end_pos=match.end(),
                        matched_text=match.group(),
                        confidence=1.0,
                    )
                    result.matches.append(match_result)
                    result.matched_keyword_ids.add(keyword.id)
            except re.error:
                continue

        result.match_time_ms = (time.time() - start_time) * 1000

        return result

    async def match_cached(
        self,
        text: str,
        keywords: list[tuple[Keyword, re.Pattern]],
    ) -> MessageMatchResult:
        """
        Match with caching.

        Args:
            text: Text to match
            keywords: List of (Keyword, Pattern) tuples

        Returns:
            MessageMatchResult
        """
        cache_key = self._get_cache_key(text)

        if cache_key in self._cache:
            cached_result, cached_time = self._cache[cache_key]
            if time.time() - cached_time < self._cache_ttl:
                return cached_result

        result = self.match_multiple(text, keywords)

        if result.has_match:
            self._cache[cache_key] = (result, time.time())

        return result

    def extract_urls(self, text: str) -> list[str]:
        """
        Extract URLs from text.

        Args:
            text: Text to extract from

        Returns:
            List of URLs
        """
        url_pattern = re.compile(
            r'https?://[^\s<>"{}|\\^`\[\]]+',
            re.IGNORECASE
        )
        return url_pattern.findall(text)

    def extract_domains(self, text: str) -> list[str]:
        """
        Extract domains from text.

        Args:
            text: Text to extract from

        Returns:
            List of domains
        """
        urls = self.extract_urls(text)
        domains = []

        for url in urls:
            match = re.search(r'https?://([^/]+)', url)
            if match:
                domains.append(match.group(1).lower())

        return domains

    def is_competitor_keyword(self, text: str) -> bool:
        """
        Check if text contains competitor keywords.

        Args:
            text: Text to check

        Returns:
            True if competitor keyword found
        """
        competitor_patterns = [
            r'\S*(机场|节点|梯子|VPN)\S*',
            r'\S*加速器\S*',
            r'v2ray|clash|ssr|trojan',
        ]

        cleaned = self._clean_text(text)

        for pattern in competitor_patterns:
            if re.search(pattern, cleaned, re.IGNORECASE):
                return True

        return False
