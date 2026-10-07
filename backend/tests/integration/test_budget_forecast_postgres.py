"""PostgreSQL transaction-abort regression, only through a disposable Unix socket."""

import os
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import URL, literal_column, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.account.budget_forecast import apply_forecast
from app.modules.acquisition.capacity_reads import CapacityReads


@pytest.mark.asyncio
@pytest.mark.parametrize("use_facade", [False, True])
async def test_forecast_sql_error_rolls_back_savepoint_and_preserves_outer_writes(use_facade):
    socket = os.getenv("GROWTH_TEST_PG_SOCKET")
    if not socket:
        pytest.skip("requires a disposable PostgreSQL Unix socket, never a production URL")
    assert Path(socket).is_absolute() and Path(socket, ".s.PGSQL.5432").is_socket()
    engine = create_async_engine(
        URL.create(
            "postgresql+asyncpg", username="postgres", database="postgres", query={"host": socket}
        ),
        pool_size=1,
        max_overflow=0,
    )
    try:
        async with async_sessionmaker(engine)() as db:
            await db.execute(text("CREATE TEMP TABLE forecast_business (id integer PRIMARY KEY)"))
            await db.execute(text("INSERT INTO forecast_business VALUES (1)"))
            reader = CapacityReads(db) if use_facade else db
            if use_facade:
                assert await reader.scalar(select(literal_column("1"))) == 1
                assert reader.results
            limits = {"day": 100, "ad_demand_hold_day": 20}
            budget = {"usage": {"day": {"used": 100, "remaining": 0, "ttl_seconds": 60000}}}

            async def broken(session, account_id, now, proposed_limits, proposed_budget):
                proposed_limits["ad_demand_hold_day"] = 0
                proposed_budget["usage"]["day"]["remaining"] = 100
                await session.execute(select(literal_column("1 / 0")))

            await apply_forecast(
                reader, 2, datetime.utcnow(), "sql_failure", broken, limits, budget
            )
            assert limits == {"day": 100, "ad_demand_hold_day": 20}
            assert budget["usage"]["day"] == {"used": 100, "remaining": 0, "ttl_seconds": 60000}
            assert "sql_failure" in budget["forecast_errors"]
            assert db.is_active
            if use_facade:
                assert reader.is_active and not reader.results
            # PostgreSQL rejects these statements if the failed query was
            # swallowed without rolling back to its savepoint.
            await db.execute(text("INSERT INTO forecast_business VALUES (2)"))
            await db.commit()
            assert (
                await db.execute(text("SELECT id FROM forecast_business ORDER BY id"))
            ).scalars().all() == [1, 2]
    finally:
        await engine.dispose()
