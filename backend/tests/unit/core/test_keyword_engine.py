"""
Unit Tests for Keyword Engine
"""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.keyword.engine import KeywordEngine, CompiledKeyword
from app.core.keyword.models import Keyword, KeywordType, KeywordStatus, MatchMode


class TestCompiledKeyword:
    """Test CompiledKeyword dataclass."""

    def test_compiled_keyword_creation(self):
        """Test creating a CompiledKeyword."""
        import re
        pattern = re.compile(r"test", re.IGNORECASE)
        keyword = Keyword(
            id=1,
            text="test",
            type=KeywordType.DEMAND,
            status=KeywordStatus.APPROVED,
            match_mode=MatchMode.EXACT,
        )
        
        compiled = CompiledKeyword(
            id=1,
            text="test",
            pattern=pattern,
            keyword_type=KeywordType.DEMAND,
            match_mode=MatchMode.EXACT,
            keyword=keyword,
        )
        
        assert compiled.id == 1
        assert compiled.text == "test"
        assert compiled.keyword_type == KeywordType.DEMAND
        assert compiled.match_mode == MatchMode.EXACT
        assert compiled.keyword is keyword


class TestKeywordEngine:
    """Test KeywordEngine class."""

    @pytest_asyncio.fixture
    async def engine(self, test_db: AsyncSession) -> KeywordEngine:
        """Create KeywordEngine with test database."""
        return KeywordEngine(test_db)

    @pytest.mark.asyncio
    async def test_load_keywords_empty(self, engine: KeywordEngine):
        """Test loading keywords when database is empty."""
        count = await engine.load_keywords()
        assert count == 0

    @pytest.mark.asyncio
    async def test_add_keyword(self, engine: KeywordEngine):
        """Test adding a new keyword."""
        keyword = await engine.add_keyword(
            text="测试关键词",
            keyword_type=KeywordType.DEMAND,
            match_mode=MatchMode.FUZZY,
            status=KeywordStatus.APPROVED,
        )
        
        assert keyword.id is not None
        assert keyword.text == "测试关键词"
        assert keyword.type == KeywordType.DEMAND
        assert keyword.match_mode == MatchMode.FUZZY
        assert keyword.status == KeywordStatus.APPROVED

    @pytest.mark.asyncio
    async def test_add_keyword_empty_text(self, engine: KeywordEngine):
        """Test adding keyword with empty text raises error."""
        from app.core.exceptions import ValidationError
        
        with pytest.raises(ValidationError):
            await engine.add_keyword(
                text="",
                keyword_type=KeywordType.DEMAND,
            )

    @pytest.mark.asyncio
    async def test_load_keywords_populates_memory(self, engine: KeywordEngine, test_db: AsyncSession):
        """Test that load_keywords populates in-memory index."""
        keyword = Keyword(
            text="test keyword",
            type=KeywordType.INQUIRY,
            status=KeywordStatus.APPROVED,
            match_mode=MatchMode.FUZZY,
        )
        test_db.add(keyword)
        await test_db.commit()
        
        count = await engine.load_keywords()
        
        assert count == 1
        assert len(engine._keywords) == 1
        assert 1 in engine._keywords

    @pytest.mark.asyncio
    async def test_load_keywords_only_active_status(self, engine: KeywordEngine, test_db: AsyncSession):
        """Test that only APPROVED and EXECUTING keywords are loaded."""
        keywords = [
            Keyword(text="approved", type=KeywordType.DEMAND, status=KeywordStatus.APPROVED, match_mode=MatchMode.FUZZY),
            Keyword(text="executing", type=KeywordType.DEMAND, status=KeywordStatus.EXECUTING, match_mode=MatchMode.FUZZY),
            Keyword(text="pending", type=KeywordType.DEMAND, status=KeywordStatus.PENDING, match_mode=MatchMode.FUZZY),
            Keyword(text="discarded", type=KeywordType.DEMAND, status=KeywordStatus.DISCARDED, match_mode=MatchMode.FUZZY),
        ]
        test_db.add_all(keywords)
        await test_db.commit()
        
        count = await engine.load_keywords()
        
        assert count == 2
        assert len(engine._keywords) == 2

    @pytest.mark.asyncio
    async def test_get_keyword(self, engine: KeywordEngine, test_db: AsyncSession):
        """Test getting keyword by ID."""
        keyword = Keyword(
            text="test",
            type=KeywordType.DEMAND,
            status=KeywordStatus.APPROVED,
            match_mode=MatchMode.EXACT,
        )
        test_db.add(keyword)
        await test_db.commit()
        
        result = await engine.get_keyword(keyword.id)
        
        assert result is not None
        assert result.text == "test"

    @pytest.mark.asyncio
    async def test_get_keyword_not_found(self, engine: KeywordEngine):
        """Test getting non-existent keyword returns None."""
        result = await engine.get_keyword(999)
        assert result is None

    @pytest.mark.asyncio
    async def test_list_keywords(self, engine: KeywordEngine, test_db: AsyncSession):
        """Test listing keywords with filters."""
        keywords = [
            Keyword(text="demand1", type=KeywordType.DEMAND, status=KeywordStatus.APPROVED, match_mode=MatchMode.FUZZY),
            Keyword(text="demand2", type=KeywordType.DEMAND, status=KeywordStatus.APPROVED, match_mode=MatchMode.FUZZY),
            Keyword(text="inquiry1", type=KeywordType.INQUIRY, status=KeywordStatus.APPROVED, match_mode=MatchMode.FUZZY),
        ]
        test_db.add_all(keywords)
        await test_db.commit()
        
        all_keywords = await engine.list_keywords()
        assert len(all_keywords) == 3
        
        demand_keywords = await engine.list_keywords(keyword_type=KeywordType.DEMAND)
        assert len(demand_keywords) == 2

    @pytest.mark.asyncio
    async def test_update_keyword(self, engine: KeywordEngine, test_db: AsyncSession):
        """Test updating a keyword."""
        keyword = Keyword(
            text="original",
            type=KeywordType.DEMAND,
            status=KeywordStatus.PENDING,
            match_mode=MatchMode.EXACT,
        )
        test_db.add(keyword)
        await test_db.commit()
        
        updated = await engine.update_keyword(
            keyword_id=keyword.id,
            text="updated",
            status=KeywordStatus.APPROVED,
        )
        
        assert updated.text == "updated"
        assert updated.status == KeywordStatus.APPROVED

    @pytest.mark.asyncio
    async def test_update_keyword_not_found(self, engine: KeywordEngine):
        """Test updating non-existent keyword raises error."""
        from app.core.exceptions import KeywordNotFoundError
        
        with pytest.raises(KeywordNotFoundError):
            await engine.update_keyword(keyword_id=999, text="test")

    @pytest.mark.asyncio
    async def test_delete_keyword(self, engine: KeywordEngine, test_db: AsyncSession):
        """Test deleting a keyword."""
        keyword = Keyword(
            text="to_delete",
            type=KeywordType.DEMAND,
            status=KeywordStatus.APPROVED,
            match_mode=MatchMode.FUZZY,
        )
        test_db.add(keyword)
        await test_db.commit()
        keyword_id = keyword.id
        
        result = await engine.delete_keyword(keyword_id)
        
        assert result is True
        assert keyword_id not in engine._keywords
        
        fetched = await engine.get_keyword(keyword_id)
        assert fetched is None

    @pytest.mark.asyncio
    async def test_delete_keyword_not_found(self, engine: KeywordEngine):
        """Test deleting non-existent keyword raises error."""
        from app.core.exceptions import KeywordNotFoundError
        
        with pytest.raises(KeywordNotFoundError):
            await engine.delete_keyword(999)

    @pytest.mark.asyncio
    async def test_approve_keyword(self, engine: KeywordEngine, test_db: AsyncSession):
        """Test approving a keyword."""
        keyword = Keyword(
            text="pending",
            type=KeywordType.DEMAND,
            status=KeywordStatus.PENDING,
            match_mode=MatchMode.FUZZY,
        )
        test_db.add(keyword)
        await test_db.commit()
        
        approved = await engine.approve_keyword(keyword.id)
        
        assert approved.status == KeywordStatus.APPROVED

    @pytest.mark.asyncio
    async def test_discard_keyword(self, engine: KeywordEngine, test_db: AsyncSession):
        """Test discarding a keyword."""
        keyword = Keyword(
            text="to_discard",
            type=KeywordType.DEMAND,
            status=KeywordStatus.PENDING,
            match_mode=MatchMode.FUZZY,
        )
        test_db.add(keyword)
        await test_db.commit()
        
        discarded = await engine.discard_keyword(keyword.id)
        
        assert discarded.status == KeywordStatus.DISCARDED

    @pytest.mark.asyncio
    async def test_get_statistics(self, engine: KeywordEngine, test_db: AsyncSession):
        """Test getting keyword statistics."""
        keywords = [
            Keyword(text="demand", type=KeywordType.DEMAND, status=KeywordStatus.APPROVED, match_mode=MatchMode.FUZZY, trigger_count=5),
            Keyword(text="inquiry", type=KeywordType.INQUIRY, status=KeywordStatus.APPROVED, match_mode=MatchMode.FUZZY, trigger_count=3),
            Keyword(text="pending", type=KeywordType.DEMAND, status=KeywordStatus.PENDING, match_mode=MatchMode.FUZZY),
        ]
        test_db.add_all(keywords)
        await test_db.commit()
        
        stats = await engine.get_statistics()
        
        assert stats["total_keywords"] == 3
        assert stats["total_triggers"] == 8
        assert stats["by_type"]["demand"] == 2
        assert stats["by_type"]["inquiry"] == 1


class TestKeywordMatchModes:
    """Test different match modes."""

    @pytest_asyncio.fixture
    async def engine(self, test_db: AsyncSession) -> KeywordEngine:
        """Create KeywordEngine with test database."""
        return KeywordEngine(test_db)

    @pytest.mark.asyncio
    async def test_exact_match_word_boundary(self, engine: KeywordEngine, test_db: AsyncSession):
        """Test EXACT mode matches only at word boundaries."""
        keyword = Keyword(
            text="hello",
            type=KeywordType.DEMAND,
            status=KeywordStatus.APPROVED,
            match_mode=MatchMode.EXACT,
        )
        test_db.add(keyword)
        await test_db.commit()
        await engine.load_keywords()
        
        # Should match
        matches = await engine.match("hello world")
        assert len(matches) == 1
        
        # Should NOT match (embedded word)
        matches = await engine.match("say hello123")
        assert len(matches) == 0
        
        # Should match (case insensitive)
        matches = await engine.match("Hello!")
        assert len(matches) == 1

    @pytest.mark.asyncio
    async def test_fuzzy_match_substring(self, engine: KeywordEngine, test_db: AsyncSession):
        """Test FUZZY mode matches substrings."""
        keyword = Keyword(
            text="hello",
            type=KeywordType.DEMAND,
            status=KeywordStatus.APPROVED,
            match_mode=MatchMode.FUZZY,
        )
        test_db.add(keyword)
        await test_db.commit()
        await engine.load_keywords()
        
        # Should match (substring)
        matches = await engine.match("say hello123")
        assert len(matches) == 1
        
        # Should match (case insensitive)
        matches = await engine.match("HELLO world")
        assert len(matches) == 1

    @pytest.mark.asyncio
    async def test_regex_match(self, engine: KeywordEngine, test_db: AsyncSession):
        """Test REGEX mode supports regex patterns."""
        keyword = Keyword(
            text=r"\d{3}-\d{4}",
            type=KeywordType.INQUIRY,
            status=KeywordStatus.APPROVED,
            match_mode=MatchMode.REGEX,
        )
        test_db.add(keyword)
        await test_db.commit()
        await engine.load_keywords()
        
        # Should match phone number format
        matches = await engine.match("My number is 123-4567")
        assert len(matches) == 1
        
        # Should NOT match invalid format
        matches = await engine.match("12-34")
        assert len(matches) == 0

    @pytest.mark.asyncio
    async def test_invalid_regex_handled(self, engine: KeywordEngine, test_db: AsyncSession):
        """Test that invalid regex is handled gracefully."""
        keyword = Keyword(
            text="[invalid",
            type=KeywordType.DEMAND,
            status=KeywordStatus.APPROVED,
            match_mode=MatchMode.REGEX,
        )
        test_db.add(keyword)
        await test_db.commit()
        
        # Should not raise, just skip the invalid keyword
        count = await engine.load_keywords()
        assert count == 0

    @pytest.mark.asyncio
    async def test_match_by_type(self, engine: KeywordEngine, test_db: AsyncSession):
        """Test matching keywords by specific type."""
        keywords = [
            Keyword(text="price query", type=KeywordType.PRICE, status=KeywordStatus.APPROVED, match_mode=MatchMode.FUZZY),
            Keyword(text="general inquiry", type=KeywordType.INQUIRY, status=KeywordStatus.APPROVED, match_mode=MatchMode.FUZZY),
        ]
        test_db.add_all(keywords)
        await test_db.commit()
        await engine.load_keywords()
        
        matches = await engine.match_by_type("check price query", KeywordType.PRICE)
        assert len(matches) == 1
        assert matches[0].keyword_type == KeywordType.PRICE

    @pytest.mark.asyncio
    async def test_match_empty_text(self, engine: KeywordEngine):
        """Test matching empty text returns empty list."""
        matches = await engine.match("")
        assert matches == []

    @pytest.mark.asyncio
    async def test_match_none_text(self, engine: KeywordEngine):
        """Test matching None text returns empty list."""
        matches = await engine.match(None)
        assert matches == []

    @pytest.mark.asyncio
    async def test_multiple_keywords_match(self, engine: KeywordEngine, test_db: AsyncSession):
        """Test matching multiple keywords from single text."""
        keywords = [
            Keyword(text="hello", type=KeywordType.INQUIRY, status=KeywordStatus.APPROVED, match_mode=MatchMode.FUZZY),
            Keyword(text="price", type=KeywordType.PRICE, status=KeywordStatus.APPROVED, match_mode=MatchMode.FUZZY),
            Keyword(text="buy", type=KeywordType.DEMAND, status=KeywordStatus.APPROVED, match_mode=MatchMode.FUZZY),
        ]
        test_db.add_all(keywords)
        await test_db.commit()
        await engine.load_keywords()
        
        matches = await engine.match("Hello, what's the price to buy?")
        
        assert len(matches) == 3
        types = {m.keyword_type for m in matches}
        assert KeywordType.INQUIRY in types
        assert KeywordType.PRICE in types
        assert KeywordType.DEMAND in types


class TestKeywordReload:
    """Test keyword reload functionality."""

    @pytest_asyncio.fixture
    async def engine(self, test_db: AsyncSession) -> KeywordEngine:
        """Create KeywordEngine with test database."""
        return KeywordEngine(test_db)

    @pytest.mark.asyncio
    async def test_reload_updates_memory(self, engine: KeywordEngine, test_db: AsyncSession):
        """Test that reload updates in-memory index."""
        await engine.load_keywords()
        assert len(engine._keywords) == 0
        
        keyword = Keyword(
            text="new keyword",
            type=KeywordType.DEMAND,
            status=KeywordStatus.APPROVED,
            match_mode=MatchMode.FUZZY,
        )
        test_db.add(keyword)
        await test_db.commit()
        
        count = await engine.reload()
        
        assert count == 1
        assert len(engine._keywords) == 1

    @pytest.mark.asyncio
    async def test_add_keyword_reloads_when_approved(self, engine: KeywordEngine):
        """Test that adding approved keyword triggers reload."""
        keyword = await engine.add_keyword(
            text="auto load",
            keyword_type=KeywordType.DEMAND,
            status=KeywordStatus.APPROVED,
        )
        
        assert keyword.id in engine._keywords
