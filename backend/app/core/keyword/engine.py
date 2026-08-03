"""
Keyword Engine Module

Provides keyword matching and management functionality.

Features:
- Keyword CRUD operations
- In-memory keyword index for fast matching
- Support for multiple match modes (exact, fuzzy, regex)
- Trigger counting and statistics
"""

import asyncio
import re
from dataclasses import dataclass
from typing import Optional

import structlog
from sqlalchemy import select, delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.keyword.models import Keyword, KeywordType, KeywordStatus, MatchMode
from app.core.keyword.regex_guard import safe_compile, safe_search, RegexTimeoutError
from app.core.exceptions import KeywordNotFoundError, ValidationError

logger = structlog.get_logger()


@dataclass
class CompiledKeyword:
    """
    Compiled keyword for fast matching.

    Attributes:
        id: Keyword database ID
        text: Original keyword text
        pattern: Compiled regex pattern
        keyword_type: Type of keyword
        match_mode: How to match
        keyword: Reference to original Keyword object
    """

    id: int
    text: str
    pattern: re.Pattern
    keyword_type: KeywordType
    match_mode: MatchMode
    keyword: Keyword


class KeywordEngine:
    """
    Keyword matching engine with in-memory indexing.

    Provides fast keyword matching for Telegram message processing.
    Keywords are loaded into memory for performance.
    """

    def __init__(self, db: AsyncSession):
        """
        Initialize KeywordEngine.

        Args:
            db: SQLAlchemy async session
        """
        self.db = db
        self._keywords: dict[int, CompiledKeyword] = {}
        self._lock = asyncio.Lock()
        self.logger = logger.bind(module="keyword_engine")

    async def load_keywords(self) -> int:
        """
        Load all active keywords into memory.

        Returns:
            Number of keywords loaded
        """
        async with self._lock:
            result = await self.db.execute(
                select(Keyword).where(
                    Keyword.status.in_([KeywordStatus.APPROVED, KeywordStatus.EXECUTING])
                )
            )
            keywords = list(result.scalars().all())

            self._keywords.clear()

            for kw in keywords:
                compiled = self._compile_keyword(kw)
                if compiled:
                    self._keywords[kw.id] = compiled

            self.logger.info("keywords_loaded", count=len(self._keywords))

            return len(self._keywords)

    def _compile_keyword(self, keyword: Keyword) -> Optional[CompiledKeyword]:
        """
        Compile keyword into matching pattern.

        Args:
            keyword: Keyword to compile

        Returns:
            CompiledKeyword or None if invalid
        """
        try:
            escaped = re.escape(keyword.text)
            
            if keyword.match_mode == MatchMode.EXACT:
                # 精确匹配：整个词边界匹配
                pattern = re.compile(r'\b' + escaped + r'\b', re.IGNORECASE)
            elif keyword.match_mode == MatchMode.FUZZY:
                # 模糊匹配：包含关键词即可（子串匹配）
                pattern = re.compile(escaped, re.IGNORECASE)
            elif keyword.match_mode == MatchMode.REGEX:
                # 正则模式：用户提供的正则表达式 —— 必须经过 ReDoS 安全检查
                pattern = safe_compile(keyword.text, re.IGNORECASE)
            else:
                # 默认使用模糊匹配
                pattern = re.compile(escaped, re.IGNORECASE)

            return CompiledKeyword(
                id=keyword.id,
                text=keyword.text,
                pattern=pattern,
                keyword_type=keyword.type,
                match_mode=keyword.match_mode,
                keyword=keyword,
            )
        except (re.error, ValueError) as e:
            self.logger.warning(
                "invalid_or_unsafe_keyword_regex",
                keyword_id=keyword.id,
                text=keyword.text,
                error=str(e),
            )
            return None

    async def match(self, text: str) -> list[CompiledKeyword]:
        """
        Match text against all keywords with ReDoS-protected regex execution.

        Args:
            text: Text to match against

        Returns:
            List of CompiledKeyword that matched
        """
        if not text:
            return []

        matches = []
        truncated = text[:10000] if len(text) > 10000 else text

        async with self._lock:
            for compiled in self._keywords.values():
                try:
                    result = await safe_search(compiled.pattern, truncated, timeout=0.5)
                    if result:
                        matches.append(compiled)
                except RegexTimeoutError:
                    self.logger.warning(
                        "regex_timeout_skipped",
                        keyword_id=compiled.id,
                        text_preview=truncated[:50],
                    )
                    continue
                except re.error:
                    continue

        return matches

    async def match_by_type(
        self,
        text: str,
        keyword_type: KeywordType,
    ) -> list[CompiledKeyword]:
        """
        Match text against keywords of specific type.

        Args:
            text: Text to match
            keyword_type: Type of keywords to match

        Returns:
            List of matching CompiledKeyword
        """
        all_matches = await self.match(text)
        return [
            compiled for compiled in all_matches
            if compiled.keyword_type == keyword_type
        ]

    async def increment_trigger(self, keyword_id: int) -> None:
        """
        Increment trigger count for a keyword.

        Args:
            keyword_id: Keyword database ID
        """
        result = await self.db.execute(
            select(Keyword).where(Keyword.id == keyword_id)
        )
        keyword = result.scalar_one_or_none()

        if keyword:
            keyword.trigger_count += 1
            await self.db.commit()

    async def add_keyword(
        self,
        text: str,
        keyword_type: KeywordType,
        match_mode: MatchMode = MatchMode.FUZZY,
        status: KeywordStatus = KeywordStatus.PENDING,
    ) -> Keyword:
        """
        Add a new keyword.

        Args:
            text: Keyword text
            keyword_type: Type of keyword
            match_mode: Match mode
            status: Initial status

        Returns:
            Created Keyword
        """
        if not text or not text.strip():
            raise ValidationError("Keyword text cannot be empty")

        keyword = Keyword(
            text=text.strip(),
            type=keyword_type,
            match_mode=match_mode,
            status=status,
        )

        self.db.add(keyword)
        await self.db.commit()
        await self.db.refresh(keyword)

        self.logger.info(
            "keyword_added",
            keyword_id=keyword.id,
            text=text,
            type=keyword_type.value,
        )

        if status in [KeywordStatus.APPROVED, KeywordStatus.EXECUTING]:
            await self.load_keywords()

        return keyword

    async def get_keyword(self, keyword_id: int) -> Optional[Keyword]:
        """
        Get keyword by ID.

        Args:
            keyword_id: Keyword database ID

        Returns:
            Keyword if found, None otherwise
        """
        result = await self.db.execute(
            select(Keyword).where(Keyword.id == keyword_id)
        )
        return result.scalar_one_or_none()

    async def list_keywords(
        self,
        keyword_type: Optional[KeywordType] = None,
        status: Optional[KeywordStatus] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Keyword]:
        """
        List keywords with filters.

        Args:
            keyword_type: Optional type filter
            status: Optional status filter
            limit: Max results
            offset: Pagination offset

        Returns:
            List of keywords
        """
        query = select(Keyword)

        if keyword_type:
            query = query.where(Keyword.type == keyword_type)
        if status:
            query = query.where(Keyword.status == status)

        query = query.order_by(Keyword.trigger_count.desc())
        query = query.limit(limit).offset(offset)

        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def update_keyword(
        self,
        keyword_id: int,
        text: Optional[str] = None,
        keyword_type: Optional[KeywordType] = None,
        status: Optional[KeywordStatus] = None,
    ) -> Keyword:
        """
        Update keyword.

        Args:
            keyword_id: Keyword ID
            text: New text
            keyword_type: New type
            status: New status

        Returns:
            Updated Keyword

        Raises:
            KeywordNotFoundError: If not found
        """
        keyword = await self.get_keyword(keyword_id)
        if not keyword:
            raise KeywordNotFoundError(f"Keyword {keyword_id} not found")

        if text is not None:
            keyword.text = text.strip()
        if keyword_type is not None:
            keyword.type = keyword_type
        if status is not None:
            keyword.status = status

        await self.db.commit()
        await self.db.refresh(keyword)

        await self.load_keywords()

        self.logger.info("keyword_updated", keyword_id=keyword_id)

        return keyword

    async def delete_keyword(self, keyword_id: int) -> bool:
        """
        Delete keyword.

        Args:
            keyword_id: Keyword ID

        Returns:
            True if deleted

        Raises:
            KeywordNotFoundError: If not found
        """
        keyword = await self.get_keyword(keyword_id)
        if not keyword:
            raise KeywordNotFoundError(f"Keyword {keyword_id} not found")

        await self.db.execute(delete(Keyword).where(Keyword.id == keyword_id))
        await self.db.commit()

        await self.load_keywords()

        self.logger.info("keyword_deleted", keyword_id=keyword_id)

        return True

    async def approve_keyword(self, keyword_id: int) -> Keyword:
        """
        Approve a keyword (change status to approved).

        Args:
            keyword_id: Keyword ID

        Returns:
            Updated Keyword
        """
        return await self.update_keyword(keyword_id, status=KeywordStatus.APPROVED)

    async def discard_keyword(self, keyword_id: int) -> Keyword:
        """
        Discard a keyword.

        Args:
            keyword_id: Keyword ID

        Returns:
            Updated Keyword
        """
        return await self.update_keyword(keyword_id, status=KeywordStatus.DISCARDED)

    async def get_statistics(self) -> dict:
        """
        Get keyword statistics.

        Returns:
            Dictionary with statistics
        """
        result = await self.db.execute(
            select(
                func.count(Keyword.id).label("total"),
                func.sum(Keyword.trigger_count).label("total_triggers"),
            )
        )
        row = result.one()

        type_counts = {}
        for ktype in KeywordType:
            count_result = await self.db.execute(
                select(func.count(Keyword.id)).where(Keyword.type == ktype)
            )
            type_counts[ktype.value] = count_result.scalar()

        status_counts = {}
        for status in KeywordStatus:
            count_result = await self.db.execute(
                select(func.count(Keyword.id)).where(Keyword.status == status)
            )
            status_counts[status.value] = count_result.scalar()

        return {
            "total_keywords": row.total or 0,
            "total_triggers": row.total_triggers or 0,
            "by_type": type_counts,
            "by_status": status_counts,
        }

    async def reload(self) -> int:
        """
        Reload keywords from database.

        Returns:
            Number of keywords loaded
        """
        return await self.load_keywords()
