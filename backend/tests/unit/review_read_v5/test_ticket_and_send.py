"""Real dispatch cancellation and durable outbound ledger through the owner."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest
from tests.fakes.session_exec_redis import FakeRedis
from sqlalchemy import select
from telethon.tl.types import InputPeerChannel
from .test_scheduling import owner

from app.core.account import session_exec
from app.core.account.models import (
    AccountOperationConfig,
    AccountOutboundAttempt,
    AccountStatus,
    TelegramAccount,
)
from app.core.account.telegram_execution import TelegramExecutionService
from app.core.scheduler import growth_dispatch


@pytest.mark.asyncio
async def test_real_owner_deadline_leaves_unknown_ad_and_retry_cannot_send(test_db, monkeypatch):
    from app.modules.acquisition import qualification_actions, qualification_service

    account = TelegramAccount(
        identifier="offline-deadline",
        session_name="offline-deadline",
        status=AccountStatus.ONLINE,
        is_active=True,
        risk_level="normal",
        registered_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=365),
    )
    test_db.add(account)
    await test_db.flush()
    test_db.add(
        AccountOperationConfig(
            account_id=account.id,
            enabled=True,
            dynamic_capacity_enabled=True,
            auto_ads_enabled=True,
            max_ads_per_day=30,
            max_messages_per_day=62,
            message_interval_seconds=600,
        )
    )
    await test_db.commit()
    monkeypatch.setattr(qualification_service, "send_gate", AsyncMock(return_value=None))
    monkeypatch.setattr(qualification_actions, "validate_live_send", AsyncMock())
    monkeypatch.setattr(
        qualification_service,
        "current_authorization",
        AsyncMock(
            return_value=(
                Obj(
                    id=1,
                    evidence_hash="h",
                    policy_version="pp-ai-qualification-v2",
                    content_scope="text_profile",
                    evidence_json="{}",
                ),
                Obj(group_id=-1001111222233),
                Obj(id=1),
            )
        ),
    )
    rpc_entered = asyncio.Event()

    async def slow_send(*args, **kwargs):
        rpc_entered.set()
        await asyncio.Event().wait()

    wrapper = Obj(
        account_id=account.id,
        client=Obj(
            get_input_entity=AsyncMock(return_value=InputPeerChannel(1111222233, 987)),
            send_message=AsyncMock(side_effect=slow_send),
        ),
    )
    risk = Obj(
        db=test_db,
        check_and_reserve=AsyncMock(return_value=Obj(allowed=True)),
        record_failure=AsyncMock(),
        record_success=AsyncMock(),
    )
    service = TelegramExecutionService(risk)
    token = "original"

    async def execute(account_id, stage):
        await service.send_ad(wrapper, -1001111222233, "offline test", reservation_token=token)
        return {"processed": 1}, 30

    monkeypatch.setattr(session_exec, "STAGE_TIMEOUT_SECONDS", 0.15)
    async with owner(execute, max_concurrency=1) as (client, submit, _):
        result = await asyncio.wait_for(submit(account.id, "ads"), 1)
        assert rpc_entered.is_set()
        assert result["result"]["reason"] == "session_owner_stage_timeout"
        row = await test_db.scalar(select(AccountOutboundAttempt))
        assert row.state == "unknown" and row.attempted_at is not None
        monkeypatch.setattr(session_exec, "STAGE_TIMEOUT_SECONDS", 2)
        original = await submit(account.id, "ads")
        assert original["error"] == "execution_failed"
        assert original["error_type"] == "TelegramSendOutcomeUnknownError"
        token = "replacement"
        replacement = await submit(account.id, "ads")
        assert replacement["error_type"] == "TelegramSendDeferredError"
        assert wrapper.client.send_message.await_count == 1
        client.assert_channel_empty()


class TicketRedis(FakeRedis):
    async def eval(self, script, numkeys, *args):
        if "session_exec_" in script:
            return await super().eval(script, numkeys, *args)
        if numkeys == 1:
            key, token = args
            if self.strings.get(key) != token:
                return 0
            if "DEL" in script:
                await self.delete(key)
            return 1
        if "local released" in script:
            run_key, priority_key, token = args
            released = int(self.strings.get(run_key) == token)
            if released:
                await self.delete(run_key)
            if self.strings.get(priority_key) == token:
                await self.delete(priority_key)
            return released
        run_key, priority_key, token, critical, priority_ttl, run_ttl = args
        if critical == "1":
            if run_key not in self.strings or priority_key not in self.strings:
                self.strings[priority_key] = token
        elif priority_key in self.strings:
            return -1
        if run_key in self.strings:
            return 0
        self.strings[run_key] = token
        return 1


@pytest.mark.asyncio
async def test_real_run_ticket_cancellation_clears_forward_and_account_fence(monkeypatch):
    from app.core import redis
    from app.core.scheduler import tasks
    from app.modules.acquisition import qualification_service

    client = TicketRedis()
    started, cleaned = asyncio.Event(), asyncio.Event()
    stage_ticket = {"review:1": "ticket"}

    async def finish(redis_client, stage, account_id, token, due):
        assert redis_client is client
        assert stage_ticket.pop(f"{stage}:{account_id}") == token

    async def execute(account, stage):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    db = Obj(get=AsyncMock(return_value=Obj(id=1)), commit=AsyncMock())

    async def run_with_db(operation):
        return await operation(db)

    monkeypatch.setattr(redis, "get_redis", AsyncMock(return_value=client))
    monkeypatch.setattr(growth_dispatch, "start", AsyncMock(return_value=1))
    monkeypatch.setattr(growth_dispatch, "finish", finish)
    monkeypatch.setattr(tasks, "_run_with_db", run_with_db)
    monkeypatch.setattr(qualification_service, "account_block_reason", lambda account, now: None)
    monkeypatch.setattr(session_exec, "_dedicated_client", lambda: client)
    await client.set("vanguard:telegram:owner:1", "other-process", ex=90)
    server = asyncio.create_task(
        session_exec.serve_requests(
            client, owns=lambda _: True, execute_stage=execute, max_concurrency=1, poll_interval=0.002
        )
    )
    ticket = asyncio.create_task(growth_dispatch.run_ticket("review", 1, "ticket"))
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert await client.get(growth_dispatch.account_run_key(1)) == "review:ticket"
        ticket.cancel()
        await asyncio.gather(ticket, return_exceptions=True)
        await asyncio.wait_for(cleaned.wait(), 0.2)
        assert not stage_ticket
        assert await client.get(growth_dispatch.account_run_key(1)) is None
        assert not any(client.lists.values())
        assert not any("session-exec" in key for key in client.strings)
    finally:
        ticket.cancel()
        server.cancel()
        await asyncio.gather(ticket, server, return_exceptions=True)
