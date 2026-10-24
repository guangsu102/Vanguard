"""Small-pool progress against a disposable PG socket, never a production URL."""

import asyncio
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import URL, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.account.models import AccountOperationConfig, AccountStatus, TelegramAccount
from app.core.database import Base
from app.core.group.models import Group, GroupAccountMembership
from app.core.scheduler import growth_dispatch
from app.modules.acquisition.group_qualification import POLICY_VERSION
from app.modules.acquisition.models import GroupQualificationAudit
from app.modules.acquisition.qualification_due import current_clause, due_clause
from tests.conftest import _import_models


@pytest.mark.asyncio
async def test_waiting_for_owner_leaves_small_pool_available_for_heartbeat(monkeypatch):
    socket = os.getenv("GROWTH_TEST_PG_SOCKET")
    if not socket:
        pytest.skip("requires disposable PostgreSQL socket")
    assert Path(socket).is_absolute() and Path(socket, ".s.PGSQL.5432").is_socket()
    _import_models()
    # TelegramAccount selects this reverse relationship when it is loaded.
    required = {TelegramAccount.__table__, AccountOperationConfig.__table__, Group.__table__,
                GroupAccountMembership.__table__, GroupQualificationAudit.__table__}
    while True:
        expanded = required | {fk.column.table for table in required for fk in table.foreign_keys}
        if required == expanded:
            break
        required = expanded
    engine = create_async_engine(URL.create("postgresql+asyncpg", username="postgres", database="postgres",
                                           query={"host": socket}), pool_size=2, max_overflow=0, pool_timeout=1)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tables = list(required)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(lambda db: Base.metadata.create_all(db, tables=tables))
        now = datetime.utcnow()
        async with sessions() as db:
            account = TelegramAccount(identifier="pool-repair", session_name="pool-repair", is_active=True,
                                      status=AccountStatus.ONLINE, risk_level="normal")
            db.add(account)
            await db.flush()
            account_id = account.id
            group = Group(group_id=12345)
            db.add(group)
            await db.flush()
            member = GroupAccountMembership(group_id=group.id, account_id=account.id, telegram_group_id=12345,
                                            joined_at=now, status="joined")
            db.add(member)
            await db.flush()
            db.add(GroupQualificationAudit(membership_id=member.id, account_id=account.id, group_id=group.id,
                                           membership_joined_at=now, batch_id="pg-review", policy_version=POLICY_VERSION,
                                           content_scope="text_profile", state="waiting_ai", decision="observe",
                                           next_retry_at=now - timedelta(seconds=1)))
            await db.commit()
            assert len((await db.scalars(select(GroupQualificationAudit.id).where(
                current_clause(), due_clause(now),
            ))).all()) == 1
        waiting, release = asyncio.Event(), asyncio.Event()
        arrivals = 0
        async def forward(*args):
            nonlocal arrivals
            arrivals += 1
            if arrivals == 4:
                waiting.set()
            await release.wait()
            return {"result": {"processed": 1}, "interval": 60}
        monkeypatch.setattr("app.core.redis.get_redis", AsyncMock(return_value=object()))
        monkeypatch.setattr("app.core.account.session_exec.owner_id", AsyncMock(return_value="other-process"))
        monkeypatch.setattr("app.core.account.session_exec.forward", forward)
        async def stage():
            async with sessions() as db:
                return await growth_dispatch.execute(db, "review", account_id)
        stages = [asyncio.create_task(stage()) for _ in range(4)]
        try:
            await asyncio.wait_for(waiting.wait(), 3)
            assert engine.pool.checkedout() == 0
            async def heartbeat():
                async with sessions() as db:
                    return await db.scalar(text("SELECT 1"))
            assert await asyncio.wait_for(asyncio.gather(*(heartbeat() for _ in range(20))), 3) == [1] * 20
        finally:
            release.set()
            await asyncio.gather(*stages)
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(lambda db: Base.metadata.drop_all(db, tables=tables))
        await engine.dispose()
