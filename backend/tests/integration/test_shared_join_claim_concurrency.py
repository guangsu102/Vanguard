"""Real PostgreSQL contention against a disposable Unix socket, never production."""

import asyncio
import os
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import URL, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.database import Base
from app.core.group.models import Group
from app.modules.acquisition.join_budget import JoinBudgetBlocked
from app.modules.acquisition.models import AutoJoinAttempt
from tests.conftest import _import_models
from tests.unit.modules.acquisition.test_join_budget import _eligible_account, _service


@pytest.mark.asyncio
@pytest.mark.parametrize("group_known", [True, False])
async def test_eight_accounts_only_one_claims_the_shared_target(group_known):
    socket = os.getenv("GROWTH_TEST_PG_SOCKET")
    if not socket:
        pytest.skip("requires a disposable PostgreSQL Unix socket, never a production URL")
    assert Path(socket).is_absolute() and Path(socket, ".s.PGSQL.5432").is_socket()
    _import_models()
    required = {Base.metadata.tables[name] for name in (
        "telegram_api_config", "proxy", "telegram_account", "telegram_account_operation_config",
        "group", "group_account_membership", "acquisition_auto_join_attempt", "owned_group_assets", "system_setting",
    )}
    # Keep this schema focused on join claims, including its FK dependencies.
    # Production uses migrations for unrelated tables; their test-only
    # create_all defaults are outside this concurrency proof.
    while True:
        expanded = required | {fk.column.table for table in required for fk in table.foreign_keys}
        if expanded == required:
            break
        required = expanded
    tables = list(required)
    engine = create_async_engine(URL.create(
        "postgresql+asyncpg", username="postgres", database="postgres", query={"host": socket},
    ), pool_size=8, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 10, 10)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
        async with sessions() as db:
            accounts = []
            for index in range(8):
                account, _ = await _eligible_account(db, f"race-{index}", now=now)
                accounts.append(account.id)
            group = Group(group_id=-1_000_000_123_456, username="shared_race", status="pending_join")
            if group_known:
                db.add(group)
                await db.commit()

        start = asyncio.Event()

        async def contender(account_id, index):
            async with sessions() as db:
                service = _service(db)
                await start.wait()
                # Known targets contend through different aliases, including a
                # legacy raw Telegram ID. Unknown targets contend by username.
                target = {"target_key": "https://t.me/SHARED_RACE/"}
                if group_known and index % 3 == 0:
                    target = {"group_id": group.id}
                elif group_known and index % 3 == 1:
                    target = {"telegram_group_id": 123_456}
                try:
                    row = await service.reserve(account_id, source="auto_join", now=now, **target)
                    return row.id
                except JoinBudgetBlocked as exc:
                    await db.rollback()
                    assert exc.reason == "join_target_occupied"
                    return None

        tasks = [asyncio.create_task(contender(account_id, index)) for index, account_id in enumerate(accounts)]
        start.set()
        winners = [value for value in await asyncio.wait_for(asyncio.gather(*tasks), 45) if value is not None]
        assert len(winners) == 1
        async with sessions() as db:
            assert await db.scalar(select(func.count(AutoJoinAttempt.id))) == 1
            winner = await db.get(AutoJoinAttempt, winners[0])
            await _service(db).release(winner, reason="released", now=now)
            replacement = await _service(db).reserve(accounts[-1], source="auto_join", now=now,
                target_key="https://t.me/shared_race")
            assert replacement.request_state == "reserved"
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.drop_all(sync, tables=tables))
        await engine.dispose()
