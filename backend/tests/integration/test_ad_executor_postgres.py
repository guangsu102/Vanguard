"""A disposable PG proves concurrent reviews leave one live control connection."""

import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import URL, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.account.models import AccountOperationConfig, AccountStatus, TelegramAccount
from app.core.account.session_exec import REPLY_PREFIX, REQUEST_KEY, serve_requests
from app.core.database import Base
from app.core.group.models import Group, GroupAccountMembership
from app.core.settings_models import SystemSetting
from app.core.telegram_chat_lock import telegram_chat_advisory_lock
from app.modules.acquisition.group_qualification import POLICY_VERSION
from app.modules.acquisition.models import AdDeliveryScheduleState, GroupQualificationAudit
from app.modules.acquisition.qualification_events import acknowledge, pending, queue_due_gaps, request
from tests.conftest import _import_models
from tests.unit.test_session_exec import FakeRedis


def _test_url():
    socket = os.getenv("GROWTH_TEST_PG_SOCKET")
    if not socket:
        pytest.skip("requires disposable PostgreSQL socket")
    assert Path(socket).is_absolute() and Path(socket, ".s.PGSQL.5432").is_socket()
    return URL.create("postgresql+asyncpg", username="postgres", database="postgres", query={"host": socket})


@pytest.mark.asyncio
async def test_two_reviews_do_not_starve_ad_and_leave_heartbeat_capacity(monkeypatch):
    engine = create_async_engine(_test_url(), pool_size=2, max_overflow=1, pool_timeout=0.5)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    cache = FakeRedis()
    first_started, both_started, release_second = asyncio.Event(), asyncio.Event(), asyncio.Event()
    ad_started, release_ad = asyncio.Event(), asyncio.Event()
    cancelled = asyncio.Event()

    async def execute(account_id, stage):
        try:
            async with sessions() as db:
                await db.scalar(text("SELECT 1"))
                if stage == "review":
                    if account_id == 1:
                        first_started.set()
                        await asyncio.Event().wait()
                    else:
                        both_started.set()
                        await release_second.wait()
                else:
                    # The production ad path commits before acquiring the
                    # dedicated chat lock; mirror that boundary so the review
                    # session and the lock still leave a pooled connection for
                    # heartbeat/control work.
                    await db.commit()
                    async with telegram_chat_advisory_lock(db, 99001122):
                        ad_started.set()
                        await release_ad.wait()
                return {"processed": 1}, 30
        finally:
            if account_id == 1:
                cancelled.set()

    async def enqueue(request_id, account_id, stage, expiry=5):
        await cache.lpush(REQUEST_KEY, json.dumps({"id": request_id, "account_id": account_id,
            "stage": stage, "expires_at": time.time() + expiry}))

    async def reply(request_id):
        raw = await cache.brpop(REPLY_PREFIX + request_id, timeout=2)
        assert raw is not None
        return json.loads(raw[1])

    async def heartbeat():
        async with sessions() as db:
            return await db.scalar(text("SELECT 1"))

    server = asyncio.create_task(serve_requests(cache, owns=lambda _: True, execute_stage=execute,
        max_concurrency=2, defer_if_busy=True,
        parallel_stages=frozenset({"review", "ads"}), queued_stages=frozenset({"ads"}),
        poll_interval=0.005))
    try:
        await enqueue("first", 1, "review", expiry=1.5)
        await asyncio.wait_for(first_started.wait(), 2)
        await enqueue("second", 2, "review")
        await asyncio.wait_for(both_started.wait(), 2)
        assert engine.pool.checkedout() == 2
        assert await asyncio.wait_for(asyncio.gather(*(heartbeat() for _ in range(20))), 2) == [1] * 20
        await enqueue("ad", 3, "ads")
        ad_reply = asyncio.create_task(reply("ad"))
        await asyncio.sleep(0.05)
        assert not ad_reply.done()
        assert (await reply("first"))["result"]["reason"] == "session_owner_stage_timeout"
        await asyncio.wait_for(ad_started.wait(), 2)
        assert engine.pool.checkedout() == 2
        assert await asyncio.wait_for(heartbeat(), 1) == 1
        release_ad.set()
        assert (await ad_reply)["result"]["processed"] == 1
        release_second.set()
        assert (await reply("second"))["result"]["processed"] == 1
        assert cancelled.is_set()
        assert engine.pool.checkedout() == 0
    finally:
        release_second.set()
        release_ad.set()
        server.cancel()
        await asyncio.gather(server, return_exceptions=True)
        await engine.dispose()


@pytest.mark.asyncio
async def test_legacy_event_horizon_and_callback_lock_keep_latest_generation():
    _import_models()
    schema = "ad_events_" + uuid.uuid4().hex
    admin = create_async_engine(_test_url())
    engine = create_async_engine(_test_url(), pool_size=2, max_overflow=1,
        connect_args={"server_settings": {"search_path": schema}})
    sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    tables = {TelegramAccount.__table__, AccountOperationConfig.__table__, Group.__table__, GroupAccountMembership.__table__,
              GroupQualificationAudit.__table__, SystemSetting.__table__, AdDeliveryScheduleState.__table__}
    while True:
        expanded = tables | {fk.column.table for table in tables for fk in table.foreign_keys}
        if expanded == tables:
            break
        tables = expanded
    try:
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with engine.begin() as connection:
            await connection.run_sync(lambda db: Base.metadata.create_all(db, tables=list(tables)))
        now = datetime.utcnow()
        async with sessions.begin() as db:
            db.add_all([TelegramAccount(id=2, identifier="ad-event", session_name="ad-event",
                status=AccountStatus.ONLINE, is_active=True), Group(id=40, group_id=11223344)])
            await db.flush()
            db.add(GroupAccountMembership(id=60, account_id=2, group_id=40, telegram_group_id=11223344,
                status="joined", joined_at=now, review_status="approved", ad_status="active"))
            await db.flush()
            db.add(GroupQualificationAudit(membership_id=60, membership_joined_at=now, group_id=40,
                account_id=2, batch_id="ad-event", policy_version=POLICY_VERSION, content_scope="text_profile",
                state="completed", decision="trial", reason="ordinary_member_ads_verified", attempts=5,
                next_retry_at=now + timedelta(days=14)))
        async with sessions.begin() as db:
            await request(db, await db.get(GroupAccountMembership, 60), "rules")
        async with sessions.begin() as callback:
            member = await callback.scalar(select(GroupAccountMembership).where(
                GroupAccountMembership.id == 60).with_for_update(of=GroupAccountMembership))
            async with sessions.begin() as dispatch:
                assert await asyncio.wait_for(queue_due_gaps(dispatch, 2), 2) == 0
            await request(callback, member, "membership")
        async with sessions.begin() as db:
            assert await queue_due_gaps(db, 2) == 1
            member = await db.get(GroupAccountMembership, 60)
            old = await pending(db, member)
            audit = await db.scalar(select(GroupQualificationAudit))
            assert audit.state == "queued" and audit.next_retry_at <= datetime.utcnow()
        async with sessions.begin() as db:
            await request(db, await db.get(GroupAccountMembership, 60), "rules", message_ids=[123])
        async with sessions.begin() as db:
            member = await db.get(GroupAccountMembership, 60)
            assert not await acknowledge(db, member, old)
            assert (await pending(db, member))["message_ids"] == [123]
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.dispose()
