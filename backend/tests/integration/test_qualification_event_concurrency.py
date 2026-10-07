"""PostgreSQL lock-order proof for callback and qualification event dispatch."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.account.models import AccountStatus, TelegramAccount
from app.core.database import Base
from app.core.group.models import Group, GroupAccountMembership
from app.modules.acquisition.group_qualification import POLICY_VERSION
from app.modules.acquisition.models import GroupQualificationAudit
from app.modules.acquisition.qualification_events import (
    acknowledge,
    pending,
    queue_due_gaps,
    request,
)
from tests.conftest import _import_models

POSTGRES_URL = os.getenv("VANGUARD_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="VANGUARD_TEST_POSTGRES_URL is required for the PostgreSQL lock gate",
)


def _async_url(raw_url: str) -> str:
    url = make_url(raw_url)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+asyncpg")
    return url.render_as_string(hide_password=False)


@pytest.mark.asyncio
async def test_callback_and_dispatch_keep_event_generation_across_locks() -> None:
    assert POSTGRES_URL is not None
    _import_models()
    schema = f"qualification_event_{uuid.uuid4().hex}"
    quoted_schema = f'"{schema}"'
    url = _async_url(POSTGRES_URL)
    admin = create_async_engine(url)
    engine = create_async_engine(
        url,
        pool_size=3,
        max_overflow=0,
        connect_args={"server_settings": {"search_path": schema}},
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    joined_at = datetime.utcnow() - timedelta(days=2)

    try:
        async with admin.begin() as connection:
            await connection.execute(text(f"CREATE SCHEMA {quoted_schema}"))
        async with engine.begin() as connection:
            required = (
                "telegram_api_config", "proxy", "telegram_account",
                "telegram_account_operation_config", "group",
                "group_account_membership", "ad_campaign", "ad_delivery_schedule_state",
                "group_qualification_audit", "system_setting",
            )
            tables = [Base.metadata.tables[name] for name in required]
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))

        async with sessions.begin() as db:
            db.add_all([
                TelegramAccount(
                    id=2, identifier="qualification-lock-test", session_name="qualification-lock-test",
                    status=AccountStatus.ONLINE, is_active=True, risk_level="normal",
                ),
                Group(id=40, group_id=1234567890, username="qualification_lock_test", title="test"),
            ])
            await db.flush()
            db.add(GroupAccountMembership(
                id=60, group_id=40, telegram_group_id=1234567890, account_id=2,
                status="joined", joined_at=joined_at, review_status="approved",
                ad_status="active",
            ))
            await db.flush()
            db.add(GroupQualificationAudit(
                batch_id="qualification-lock-test", membership_id=60,
                account_id=2, group_id=40, policy_version=POLICY_VERSION,
                content_scope="text_profile", state="completed", decision="trial",
                membership_joined_at=joined_at, checked_at=datetime.utcnow(),
                expires_at=datetime.utcnow() + timedelta(days=1),
            ))

        async with sessions.begin() as db:
            member = await db.scalar(select(GroupAccountMembership).where(
                GroupAccountMembership.id == 60,
            ).with_for_update(of=GroupAccountMembership))
            await request(db, member, "gap")
            await db.flush()
            initial = await pending(db, member)

        holding_member = asyncio.Event()
        release_member = asyncio.Event()

        async def callback() -> None:
            async with sessions.begin() as db:
                member = await db.scalar(select(GroupAccountMembership).where(
                    GroupAccountMembership.id == 60,
                ).with_for_update(of=GroupAccountMembership))
                holding_member.set()
                await release_member.wait()
                await request(db, member, "membership")

        callback_task = asyncio.create_task(callback())
        try:
            await asyncio.wait_for(holding_member.wait(), 15)
            async with sessions.begin() as db:
                # The dispatcher locks the audit first, then skips the busy
                # membership. The callback never waits for that audit lock.
                assert await asyncio.wait_for(queue_due_gaps(db, 2), 15) == 0
        finally:
            release_member.set()
            await asyncio.wait_for(callback_task, 15)

        async with sessions.begin() as db:
            member = await db.get(GroupAccountMembership, 60)
            first = await pending(db, member)
            assert first is not None and not first["queued"]
            assert first != initial
            assert set(first["kinds"]) == {"gap", "membership"}

        async with sessions.begin() as holding:
            await holding.scalar(select(GroupQualificationAudit).where(
                GroupQualificationAudit.membership_id == 60,
            ).with_for_update(of=GroupQualificationAudit))
            async with sessions.begin() as competing:
                assert await asyncio.wait_for(queue_due_gaps(competing, 2), 15) == 0

        async with sessions() as aborted:
            assert await queue_due_gaps(aborted, 2) == 1
            await aborted.rollback()

        async with sessions.begin() as db:
            assert await queue_due_gaps(db, 2) == 1
        async with sessions.begin() as db:
            member = await db.get(GroupAccountMembership, 60)
            queued = await pending(db, member)
            assert queued is not None and queued["queued"]
            assert await queue_due_gaps(db, 2) == 0
            await request(db, member, "rules", message_ids=[91])
        async with sessions.begin() as db:
            member = await db.get(GroupAccountMembership, 60)
            assert not await acknowledge(db, member, queued)
            current = await pending(db, member)
            assert current is not None and not current["queued"]
            assert current["message_ids"] == [91]
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f"DROP SCHEMA IF EXISTS {quoted_schema} CASCADE"))
        await admin.dispose()
