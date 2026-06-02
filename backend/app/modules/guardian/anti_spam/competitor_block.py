"""
Competitor Blocker

Blocks competitor keywords and domains.
"""

import re
from dataclasses import dataclass
from typing import Optional

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.keyword.engine import KeywordEngine
from app.core.keyword.models import KeywordType
from app.modules.guardian.models import (
    ModerationRule,
    RuleType,
    ViolationLevel,
    ViolationAction,
)

logger = structlog.get_logger()


@dataclass
class CompetitorMatchResult:
    """Result of competitor content check."""
    is_competitor: bool
    matched_keywords: list[str]
    matched_domains: list[str]
    severity: ViolationLevel
    action: ViolationAction


class CompetitorBlocker:
    """
    Blocker for competitor content.
    
    Detects and blocks competitor keywords and domains.
    """
    
    def __init__(
        self,
        db: AsyncSession,
        keyword_engine: KeywordEngine
    ):
        """
        Initialize CompetitorBlocker.
        
        Args:
            db: Database session
            keyword_engine: Keyword engine for matching
        """
        self.db = db
        self.keyword_engine = keyword_engine
        self._competitor_keywords: set[str] = set()
        self._blocked_domains: set[str] = set()
        self.logger = logger.bind(module="competitor_blocker")
    
    async def load_competitor_keywords(self) -> int:
        """
        Load competitor keywords from keyword engine.
        
        Returns:
            Number of keywords loaded
        """
        keywords = await self.keyword_engine.list_keywords(
            keyword_type=KeywordType.COMPETITOR,
            status=None
        )
        
        self._competitor_keywords = {kw.text.lower() for kw in keywords}
        
        self.logger.info(
            "competitor_keywords_loaded",
            count=len(self._competitor_keywords)
        )
        
        return len(self._competitor_keywords)
    
    async def load_blocked_domains(self, group_id: Optional[int] = None) -> int:
        """
        Load blocked domains from moderation rules.
        
        Args:
            group_id: Optional group ID for group-specific rules
            
        Returns:
            Number of domains loaded
        """
        query = select(ModerationRule).where(
            ModerationRule.rule_type == RuleType.DOMAIN,
            ModerationRule.enabled == True
        )
        
        if group_id:
            query = query.where(
                (ModerationRule.group_id == group_id) | (ModerationRule.group_id.is_(None))
            )
        
        result = await self.db.execute(query)
        rules = list(result.scalars().all())
        
        self._blocked_domains = {rule.pattern.lower() for rule in rules}
        
        self.logger.info(
            "blocked_domains_loaded",
            count=len(self._blocked_domains)
        )
        
        return len(self._blocked_domains)
    
    def extract_urls(self, text: str) -> list[str]:
        """
        Extract URLs from text.
        
        Args:
            text: Text to extract from
            
        Returns:
            List of URLs
        """
        url_pattern = re.compile(
            r'(?:https?://)?(?:[\w-]+\.)*(?:[\w-]+)\.(?:com|net|org|io|co|me|app|xyz|top|vip|cc|ru|cn|info|biz|tv|online|site|website|space|club|live|pro|work|tech|tools|ai|cloud|host|store|shop|fun|pro|link|in|fm|fr|de|uk|au|ru|cn|jp|kr|sg|hk|tw|mobi|la|mn|su|ws|sh|ac|ag|la|mn|su|ws|sh|la|mn|su|ws|sh)',
            re.IGNORECASE
        )
        
        return url_pattern.findall(text)
    
    def extract_domains(self, text: str) -> list[str]:
        """
        Extract domains from URLs.
        
        Args:
            text: Text containing URLs
            
        Returns:
            List of domains
        """
        urls = self.extract_urls(text)
        domains = []
        
        for url in urls:
            domain = url.lower()
            for prefix in ['http://', 'https://', 'www.']:
                if domain.startswith(prefix):
                    domain = domain[len(prefix):]
            if '/' in domain:
                domain = domain.split('/')[0]
            domains.append(domain)
        
        return domains
    
    async def check_competitor_content(
        self,
        text: str,
        group_id: Optional[int] = None
    ) -> CompetitorMatchResult:
        """
        Check if text contains competitor content.
        
        Args:
            text: Text to check
            group_id: Optional group ID
            
        Returns:
            CompetitorMatchResult
        """
        if not text:
            return CompetitorMatchResult(
                is_competitor=False,
                matched_keywords=[],
                matched_domains=[],
                severity=ViolationLevel.LOW,
                action=ViolationAction.WARN
            )
        
        text_lower = text.lower()
        matched_keywords = []
        matched_domains = []
        
        if not self._competitor_keywords:
            await self.load_competitor_keywords()
        
        for keyword in self._competitor_keywords:
            if keyword in text_lower:
                matched_keywords.append(keyword)
        
        if not self._blocked_domains:
            await self.load_blocked_domains(group_id)
        
        domains = self.extract_domains(text)
        for domain in domains:
            for blocked_pattern in self._blocked_domains:
                try:
                    pattern = re.compile(blocked_pattern, re.IGNORECASE)
                    if pattern.search(domain):
                        matched_domains.append(domain)
                        break
                except re.error:
                    if blocked_pattern in domain:
                        matched_domains.append(domain)
                        break
        
        if not matched_keywords and not matched_domains:
            return CompetitorMatchResult(
                is_competitor=False,
                matched_keywords=[],
                matched_domains=[],
                severity=ViolationLevel.LOW,
                action=ViolationAction.WARN
            )
        
        severity = self._calculate_severity(matched_keywords, matched_domains)
        action = self._determine_action(severity, matched_keywords)
        
        if matched_keywords:
            self.logger.warning(
                "competitor_content_detected",
                keywords=matched_keywords,
                domains=matched_domains
            )
        
        return CompetitorMatchResult(
            is_competitor=True,
            matched_keywords=matched_keywords,
            matched_domains=matched_domains,
            severity=severity,
            action=action
        )
    
    def _calculate_severity(
        self,
        keywords: list[str],
        domains: list[str]
    ) -> ViolationLevel:
        """Calculate severity based on matched content."""
        if keywords:
            return ViolationLevel.HIGH
        if domains:
            return ViolationLevel.MEDIUM
        return ViolationLevel.LOW
    
    def _determine_action(
        self,
        severity: ViolationLevel,
        matched_keywords: list[str]
    ) -> ViolationAction:
        """Determine action based on severity and matched content."""
        if severity == ViolationLevel.HIGH:
            return ViolationAction.BAN
        elif severity == ViolationLevel.MEDIUM:
            return ViolationAction.MUTE
        else:
            return ViolationAction.WARN
    
    async def add_competitor_keyword(self, keyword: str) -> None:
        """
        Add a competitor keyword.
        
        Args:
            keyword: Keyword to add
        """
        self._competitor_keywords.add(keyword.lower())
        self.logger.info("competitor_keyword_added", keyword=keyword)
    
    async def remove_competitor_keyword(self, keyword: str) -> None:
        """
        Remove a competitor keyword.
        
        Args:
            keyword: Keyword to remove
        """
        self._competitor_keywords.discard(keyword.lower())
        self.logger.info("competitor_keyword_removed", keyword=keyword)
    
    async def reload(self) -> dict:
        """
        Reload all competitor data.
        
        Returns:
            Dict with counts
        """
        kw_count = await self.load_competitor_keywords()
        domain_count = await self.load_blocked_domains()
        
        return {
            "keywords": kw_count,
            "domains": domain_count
        }
