"""
Standalone Test Runner for Keyword Engine
Tests keyword module with isolated database setup
"""

import asyncio
import sys
sys.path.insert(0, "d:/tanxuan/project/Vanguard/backend")

# Import only the models directly without triggering relationships
import re
from datetime import datetime
from enum import Enum


class KeywordType(str, Enum):
    """Keyword type enumeration."""
    DEMAND = "demand"
    INQUIRY = "inquiry"
    PRICE = "price"
    COMPETITOR = "competitor"


class KeywordStatus(str, Enum):
    """Keyword status enumeration."""
    PENDING = "pending"
    APPROVED = "approved"
    EXECUTING = "executing"
    COMPLETED = "completed"
    DISCARDED = "discarded"


class MatchMode(str, Enum):
    """Keyword match mode."""
    EXACT = "exact"
    FUZZY = "fuzzy"
    REGEX = "regex"


from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import declarative_base, Mapped, mapped_column
from sqlalchemy import DateTime, Enum as SQLEnum, Index, Integer, String

# Create test base with only Keyword table
TestBase = declarative_base()


class Keyword(TestBase):
    """Keyword model for testing - mirrors the real model."""
    
    __tablename__ = "keyword"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    text: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[KeywordType] = mapped_column(SQLEnum(KeywordType), nullable=False)
    status: Mapped[KeywordStatus] = mapped_column(SQLEnum(KeywordStatus), default=KeywordStatus.PENDING, nullable=False)
    match_mode: Mapped[MatchMode] = mapped_column(SQLEnum(MatchMode), default=MatchMode.FUZZY, nullable=False)
    trigger_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


# Now import engine components
from app.core.keyword.engine import KeywordEngine, CompiledKeyword
from app.core.keyword.matcher import KeywordMatcher, MatchResult, MessageMatchResult


async def test_compiled_keyword():
    """Test CompiledKeyword creation."""
    print("Testing CompiledKeyword...")
    
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
    print("  [PASS] CompiledKeyword creation")


async def test_keyword_engine_basic():
    """Test KeywordEngine basic operations."""
    print("\nTesting KeywordEngine basic operations...")
    
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    
    async with engine.begin() as conn:
        await conn.run_sync(TestBase.metadata.create_all)
    
    async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    
    async with async_session() as session:
        eng = KeywordEngine(session)
        
        # Test load empty
        count = await eng.load_keywords()
        assert count == 0, f"Expected 0, got {count}"
        print("  [PASS] Load empty keywords")
        
        # Test add keyword
        keyword = await eng.add_keyword(
            text="测试关键词",
            keyword_type=KeywordType.DEMAND,
            match_mode=MatchMode.FUZZY,
            status=KeywordStatus.APPROVED,
        )
        assert keyword.id is not None
        assert keyword.text == "测试关键词"
        print("  [PASS] Add keyword")
        
        # Test load keywords
        count = await eng.load_keywords()
        assert count == 1
        assert len(eng._keywords) == 1
        print("  [PASS] Load keywords populates memory")
        
        # Test get keyword
        result = await eng.get_keyword(keyword.id)
        assert result is not None
        assert result.text == "测试关键词"
        print("  [PASS] Get keyword by ID")
        
        # Test get non-existent keyword
        result = await eng.get_keyword(999)
        assert result is None
        print("  [PASS] Get non-existent keyword returns None")
        
        # Test list keywords
        all_kw = await eng.list_keywords()
        assert len(all_kw) == 1
        print("  [PASS] List keywords")
        
        # Test update keyword
        updated = await eng.update_keyword(
            keyword_id=keyword.id,
            text="更新后的关键词",
            status=KeywordStatus.EXECUTING,
        )
        assert updated.text == "更新后的关键词"
        assert updated.status == KeywordStatus.EXECUTING
        print("  [PASS] Update keyword")
        
        # Test statistics
        stats = await eng.get_statistics()
        assert stats["total_keywords"] == 1
        print("  [PASS] Get statistics")
        
    await engine.dispose()


async def test_match_modes():
    """Test different match modes."""
    print("\nTesting match modes...")
    
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    
    async with engine.begin() as conn:
        await conn.run_sync(TestBase.metadata.create_all)
    
    async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    
    async with async_session() as session:
        eng = KeywordEngine(session)
        
        # Test EXACT mode - should match at word boundaries
        await eng.add_keyword(
            text="hello",
            keyword_type=KeywordType.INQUIRY,
            match_mode=MatchMode.EXACT,
            status=KeywordStatus.APPROVED,
        )
        await eng.load_keywords()
        
        matches = await eng.match("hello world")
        assert len(matches) == 1, f"EXACT mode: 'hello world' should match, got {len(matches)}"
        print("  [PASS] EXACT mode matches word boundary")
        
        matches = await eng.match("say hello123")
        assert len(matches) == 0, f"EXACT mode: 'hello123' should NOT match"
        print("  [PASS] EXACT mode rejects embedded words")
        
        # Test FUZZY mode - should match substrings
        await eng.add_keyword(
            text="test",
            keyword_type=KeywordType.DEMAND,
            match_mode=MatchMode.FUZZY,
            status=KeywordStatus.APPROVED,
        )
        await eng.load_keywords()
        
        matches = await eng.match("this is a test123 string")
        assert len(matches) == 1, f"FUZZY mode: 'test123' should match"
        print("  [PASS] FUZZY mode matches substrings")
        
        matches = await eng.match("TESTING")
        assert len(matches) == 1, f"FUZZY mode should be case insensitive"
        print("  [PASS] FUZZY mode is case insensitive")
        
        # Test REGEX mode
        await eng.add_keyword(
            text=r"\d{3}-\d{4}",
            keyword_type=KeywordType.INQUIRY,
            match_mode=MatchMode.REGEX,
            status=KeywordStatus.APPROVED,
        )
        await eng.load_keywords()
        
        matches = await eng.match("My number is 123-4567")
        assert len(matches) == 1, f"REGEX mode: phone number should match"
        print("  [PASS] REGEX mode works correctly")
        
        # Test match_by_type
        matches = await eng.match_by_type("hello", KeywordType.INQUIRY)
        assert len(matches) == 1
        print("  [PASS] Match by type works")
        
    await engine.dispose()


async def test_keyword_matcher():
    """Test KeywordMatcher class."""
    print("\nTesting KeywordMatcher...")
    
    matcher = KeywordMatcher()
    
    # Test clean_text
    cleaned = matcher._clean_text("  Hello   World  ")
    assert cleaned == "hello world"
    print("  [PASS] Text cleaning normalizes whitespace")
    
    # Test cache key
    key1 = matcher._get_cache_key("test text")
    key2 = matcher._get_cache_key("test text")
    assert key1 == key2
    print("  [PASS] Cache key is deterministic")
    
    # Test match_single
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
    print("  [PASS] Single keyword matching")
    
    # Test match_multiple
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
    print("  [PASS] Multiple keyword matching")
    
    # Test extract_urls
    urls = matcher.extract_urls("Check https://example.com and http://test.org")
    assert len(urls) == 2
    print("  [PASS] URL extraction")
    
    # Test is_competitor_keyword
    assert matcher.is_competitor_keyword("免费机场节点")
    assert not matcher.is_competitor_keyword("这个产品很好用")
    print("  [PASS] Competitor keyword detection")
    
    # Test clear_cache
    matcher.clear_cache()
    assert len(matcher._cache) == 0
    print("  [PASS] Cache clearing")


async def test_match_result():
    """Test MatchResult and MessageMatchResult."""
    print("\nTesting MatchResult classes...")
    
    keyword = Keyword(
        id=1,
        text="test",
        type=KeywordType.DEMAND,
        status=KeywordStatus.APPROVED,
        match_mode=MatchMode.EXACT,
    )
    
    # Test MatchResult
    result = MatchResult(
        keyword=keyword,
        start_pos=5,
        end_pos=9,
        matched_text="test",
        confidence=1.0,
    )
    assert result.length == 4
    print("  [PASS] MatchResult length property")
    
    # Test MessageMatchResult
    msg_result = MessageMatchResult(message_id=123, text="test message")
    assert not msg_result.has_match
    assert msg_result.match_count == 0
    
    msg_result.matches.append(result)
    assert msg_result.has_match
    assert msg_result.match_count == 1
    print("  [PASS] MessageMatchResult properties")
    
    # Test get_keywords_by_type
    kw2 = Keyword(id=2, text="price", type=KeywordType.PRICE, status=KeywordStatus.APPROVED, match_mode=MatchMode.FUZZY)
    result2 = MatchResult(keyword=kw2, start_pos=0, end_pos=5, matched_text="price")
    msg_result.matches.append(result2)
    
    demand_matches = msg_result.get_keywords_by_type(KeywordType.DEMAND)
    assert len(demand_matches) == 1
    print("  [PASS] Filter matches by type")


async def test_caching():
    """Test caching functionality."""
    print("\nTesting caching...")
    import time
    
    matcher = KeywordMatcher(cache_ttl=1)
    
    keyword = Keyword(id=1, text="test", type=KeywordType.DEMAND, status=KeywordStatus.APPROVED, match_mode=MatchMode.FUZZY)
    patterns = [(keyword, re.compile(r"test"))]
    
    # First call
    result1 = await matcher.match_cached("test message", patterns)
    assert result1.has_match
    print("  [PASS] First match with caching")
    
    # Check cache was populated
    cache_key = matcher._get_cache_key("test message")
    assert cache_key in matcher._cache
    cached_result, cached_time = matcher._cache[cache_key]
    print("  [PASS] Cache populated correctly")
    
    # Verify cache structure uses (result, timestamp)
    assert isinstance(cached_result, MessageMatchResult)
    assert isinstance(cached_time, float)
    print("  [PASS] Cache stores (result, timestamp) tuple correctly")


async def run_all_tests():
    """Run all tests."""
    print("=" * 60)
    print("Keyword Engine Unit Tests")
    print("=" * 60)
    
    passed = 0
    failed = 0
    
    try:
        await test_compiled_keyword()
        passed += 1
    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback
        traceback.print_exc()
        failed += 1
    
    try:
        await test_keyword_engine_basic()
        passed += 1
    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback
        traceback.print_exc()
        failed += 1
    
    try:
        await test_match_modes()
        passed += 1
    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback
        traceback.print_exc()
        failed += 1
    
    try:
        await test_keyword_matcher()
        passed += 1
    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback
        traceback.print_exc()
        failed += 1
    
    try:
        await test_match_result()
        passed += 1
    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback
        traceback.print_exc()
        failed += 1
    
    try:
        await test_caching()
        passed += 1
    except Exception as e:
        print(f"  [FAIL] {e}")
        import traceback
        traceback.print_exc()
        failed += 1
    
    print("\n" + "=" * 60)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 60)
    
    return failed == 0


if __name__ == "__main__":
    success = asyncio.run(run_all_tests())
    sys.exit(0 if success else 1)
