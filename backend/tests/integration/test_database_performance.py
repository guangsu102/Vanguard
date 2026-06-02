"""
Database Performance Tests

Tests database query performance and connection handling.
"""

import asyncio
import time
from typing import List, Dict, Any

import pytest
import pytest_asyncio
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.core.database import Base, get_db
from app.core.user.models import User, UserState
from app.core.keyword.models import Keyword, KeywordType, KeywordStatus, MatchMode
from app.core.group.models import Group, GroupLevel
from app.main import app


class QueryMetrics:
    """Collect and report query performance metrics."""

    def __init__(self):
        self.queries: List[Dict[str, Any]] = []

    def record(self, query_name: str, latency: float, rows: int = 0):
        self.queries.append({
            "name": query_name,
            "latency_ms": latency * 1000,
            "rows": rows,
        })

    def get_report(self) -> Dict[str, Any]:
        if not self.queries:
            return {}

        total_time = sum(q["latency_ms"] for q in self.queries)
        return {
            "total_queries": len(self.queries),
            "total_time_ms": f"{total_time:.2f}",
            "avg_time_ms": f"{total_time / len(self.queries):.2f}",
            "queries": [
                {
                    "name": q["name"],
                    "latency_ms": f"{q['latency_ms']:.2f}",
                    "rows": q["rows"],
                }
                for q in sorted(self.queries, key=lambda x: x["latency_ms"], reverse=True)
            ],
        }


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    """Create a test database session."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as session:
        yield session

    await engine.dispose()


@pytest_asyncio.fixture
async def populated_db(db_session: AsyncSession) -> AsyncSession:
    """Populate database with test data."""
    users = [
        User(
            telegram_id=1000 + i,
            username=f"user_{i}",
            state=UserState.ACTIVE if i % 5 == 0 else UserState.PENDING,
        )
        for i in range(1000)
    ]
    db_session.add_all(users)

    keywords = [
        Keyword(
            text=f"keyword_{i}",
            type=KeywordType.DEMAND if i % 3 == 0 else KeywordType.INQUIRY,
            status=KeywordStatus.APPROVED,
            match_mode=MatchMode.FUZZY,
            trigger_count=i * 10,
        )
        for i in range(500)
    ]
    db_session.add_all(keywords)

    groups = [
        Group(
            group_id=2000 + i,
            title=f"Group {i}",
            level=GroupLevel.A if i % 4 == 0 else GroupLevel.B,
            member_count=100 + i,
        )
        for i in range(200)
    ]
    db_session.add_all(groups)

    await db_session.commit()
    return db_session


@pytest.mark.asyncio
async def test_select_single_record(db_session: AsyncSession, populated_db: AsyncSession):
    """Test SELECT single record performance."""
    metrics = QueryMetrics()

    for _ in range(100):
        start = time.perf_counter()
        result = await db_session.execute(
            select(User).where(User.id == 1)
        )
        user = result.scalar_one_or_none()
        latency = time.perf_counter() - start
        metrics.record("SELECT single", latency, 1 if user else 0)

    report = metrics.get_report()
    print(f"\nSelect Single Record:\n{report}")

    avg_latency = sum(q["latency_ms"] for q in metrics.queries) / len(metrics.queries)
    assert avg_latency < 50, f"Average latency too high: {avg_latency:.2f}ms"


@pytest.mark.asyncio
async def test_select_with_where(db_session: AsyncSession, populated_db: AsyncSession):
    """Test SELECT with WHERE clause performance."""
    metrics = QueryMetrics()

    for i in range(100):
        state = UserState.ACTIVE if i % 2 == 0 else UserState.PENDING
        start = time.perf_counter()
        result = await db_session.execute(
            select(User).where(User.state == state)
        )
        users = result.scalars().all()
        latency = time.perf_counter() - start
        metrics.record("SELECT WHERE", latency, len(users))

    report = metrics.get_report()
    print(f"\nSelect With WHERE:\n{report}")

    avg_latency = sum(q["latency_ms"] for q in metrics.queries) / len(metrics.queries)
    assert avg_latency < 100, f"Average latency too high: {avg_latency:.2f}ms"


@pytest.mark.asyncio
async def test_select_with_join(db_session: AsyncSession, populated_db: AsyncSession):
    """Test SELECT with JOIN performance."""
    metrics = QueryMetrics()

    for _ in range(50):
        start = time.perf_counter()
        result = await db_session.execute(
            select(User, Keyword)
            .join(Keyword, User.id == Keyword.id)
            .where(User.state == UserState.ACTIVE)
            .limit(10)
        )
        rows = result.all()
        latency = time.perf_counter() - start
        metrics.record("SELECT JOIN", latency, len(rows))

    report = metrics.get_report()
    print(f"\nSelect With JOIN:\n{report}")

    avg_latency = sum(q["latency_ms"] for q in metrics.queries) / len(metrics.queries)
    assert avg_latency < 100, f"Average latency too high: {avg_latency:.2f}ms"


@pytest.mark.asyncio
async def test_aggregation_query(db_session: AsyncSession, populated_db: AsyncSession):
    """Test aggregation query performance (COUNT, SUM, AVG)."""
    metrics = QueryMetrics()

    for _ in range(50):
        start = time.perf_counter()
        result = await db_session.execute(
            select(func.count(User.id)).where(User.state == UserState.ACTIVE)
        )
        count = result.scalar()
        latency = time.perf_counter() - start
        metrics.record("COUNT", latency, 1)

    for _ in range(50):
        start = time.perf_counter()
        result = await db_session.execute(
            select(func.sum(Keyword.trigger_count)).where(Keyword.status == KeywordStatus.APPROVED)
        )
        total = result.scalar()
        latency = time.perf_counter() - start
        metrics.record("SUM", latency, 1)

    for _ in range(50):
        start = time.perf_counter()
        result = await db_session.execute(
            select(func.avg(Keyword.trigger_count))
        )
        avg = result.scalar()
        latency = time.perf_counter() - start
        metrics.record("AVG", latency, 1)

    report = metrics.get_report()
    print(f"\nAggregation Queries:\n{report}")

    avg_latency = sum(q["latency_ms"] for q in metrics.queries) / len(metrics.queries)
    assert avg_latency < 100, f"Average latency too high: {avg_latency:.2f}ms"


@pytest.mark.asyncio
async def test_pagination_query(db_session: AsyncSession, populated_db: AsyncSession):
    """Test paginated query performance."""
    metrics = QueryMetrics()
    page_size = 20

    for page in range(50):
        offset = page * page_size
        start = time.perf_counter()
        result = await db_session.execute(
            select(User)
            .order_by(User.id)
            .limit(page_size)
            .offset(offset)
        )
        users = result.scalars().all()
        latency = time.perf_counter() - start
        metrics.record("Pagination", latency, len(users))

    report = metrics.get_report()
    print(f"\nPaginated Queries:\n{report}")

    avg_latency = sum(q["latency_ms"] for q in metrics.queries) / len(metrics.queries)
    assert avg_latency < 100, f"Average latency too high: {avg_latency:.2f}ms"


@pytest.mark.asyncio
async def test_insert_performance(db_session: AsyncSession):
    """Test bulk insert performance."""
    metrics = QueryMetrics()

    for batch in range(10):
        start = time.perf_counter()
        users = [
            User(
                telegram_id=5000 + batch * 100 + i,
                username=f"bulk_user_{batch}_{i}",
                state=UserState.NEW,
            )
            for i in range(100)
        ]
        db_session.add_all(users)
        await db_session.commit()
        latency = time.perf_counter() - start
        metrics.record("Bulk INSERT", latency, 100)

    report = metrics.get_report()
    print(f"\nBulk Insert:\n{report}")

    avg_latency = sum(q["latency_ms"] for q in metrics.queries) / len(metrics.queries)
    assert avg_latency < 500, f"Average latency too high: {avg_latency:.2f}ms"


@pytest.mark.asyncio
async def test_update_performance(db_session: AsyncSession, populated_db: AsyncSession):
    """Test update query performance."""
    metrics = QueryMetrics()

    for i in range(100):
        start = time.perf_counter()
        result = await db_session.execute(
            select(User).where(User.id == i + 1)
        )
        user = result.scalar_one_or_none()
        if user:
            user.warning_count += 1
            await db_session.commit()
        latency = time.perf_counter() - start
        metrics.record("UPDATE", latency, 1)

    report = metrics.get_report()
    print(f"\nUpdate Queries:\n{report}")

    avg_latency = sum(q["latency_ms"] for q in metrics.queries) / len(metrics.queries)
    assert avg_latency < 100, f"Average latency too high: {avg_latency:.2f}ms"


@pytest.mark.asyncio
async def test_delete_performance(db_session: AsyncSession, populated_db: AsyncSession):
    """Test delete query performance."""
    metrics = QueryMetrics()

    for i in range(50):
        start = time.perf_counter()
        result = await db_session.execute(
            select(Keyword).where(Keyword.id == i + 1)
        )
        keyword = result.scalar_one_or_none()
        if keyword:
            await db_session.delete(keyword)
            await db_session.commit()
        latency = time.perf_counter() - start
        metrics.record("DELETE", latency, 1)

    report = metrics.get_report()
    print(f"\nDelete Queries:\n{report}")

    avg_latency = sum(q["latency_ms"] for q in metrics.queries) / len(metrics.queries)
    assert avg_latency < 100, f"Average latency too high: {avg_latency:.2f}ms"


@pytest.mark.asyncio
async def test_concurrent_queries(db_session: AsyncSession, populated_db: AsyncSession):
    """Test concurrent query performance."""
    async def run_query(query_num: int):
        metrics = QueryMetrics()
        start = time.perf_counter()

        if query_num % 3 == 0:
            result = await db_session.execute(select(User).limit(100))
        elif query_num % 3 == 1:
            result = await db_session.execute(select(Keyword).limit(100))
        else:
            result = await db_session.execute(select(Group).limit(100))

        rows = result.scalars().all()
        latency = time.perf_counter() - start
        metrics.record(f"concurrent_{query_num}", latency, len(rows))
        return metrics

    tasks = [run_query(i) for i in range(50)]
    all_metrics = await asyncio.gather(*tasks)

    total_latency = sum(
        sum(q["latency_ms"] for q in m.queries)
        for m in all_metrics
    )
    total_queries = sum(len(m.queries) for m in all_metrics)
    avg_latency = total_latency / total_queries if total_queries > 0 else 0

    print(f"\nConcurrent Queries ({len(tasks)} concurrent):\n"
          f"  Total queries: {total_queries}\n"
          f"  Average latency: {avg_latency:.2f}ms\n"
          f"  Max latency: {max(max(m.queries, key=lambda x: x['latency_ms'])['latency_ms'] for m in all_metrics):.2f}ms")

    assert avg_latency < 300, f"Average latency too high under concurrency: {avg_latency:.2f}ms"


@pytest.mark.asyncio
async def test_index_effectiveness(db_session: AsyncSession, populated_db: AsyncSession):
    """Test that indexes are being used effectively."""
    metrics = QueryMetrics()

    start = time.perf_counter()
    result = await db_session.execute(
        select(User).where(User.state == UserState.ACTIVE)
    )
    users = result.scalars().all()
    indexed_latency = time.perf_counter() - start
    metrics.record("Indexed query", indexed_latency, len(users))

    start = time.perf_counter()
    result = await db_session.execute(
        select(User).where(User.telegram_id == 1500)
    )
    users = result.scalars().all()
    unique_latency = time.perf_counter() - start
    metrics.record("Unique index query", unique_latency, len(users))

    report = metrics.get_report()
    print(f"\nIndex Effectiveness:\n{report}")

    for query in metrics.queries:
        assert query["latency_ms"] < 100, f"Query {query['name']} too slow: {query['latency_ms']:.2f}ms"


@pytest.mark.asyncio
async def test_connection_pool_efficiency():
    """Test database connection pool efficiency."""
    from app.core.database import engine

    if engine is None:
        pytest.skip("Engine not initialized")

    pool = engine.pool
    print(f"\nConnection Pool Status:\n"
          f"  Pool size: {pool.size()}\n"
          f"  Overflow: {pool.overflow()}\n"
          f"  Checked in: {pool.checkedin()}\n"
          f"  Checked out: {pool.checkedout()}")

    assert pool.size() > 0, "Connection pool not properly initialized"
