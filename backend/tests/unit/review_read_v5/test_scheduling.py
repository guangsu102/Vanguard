"""Real executor offline regressions; no Telegram/DB/network side effects."""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from tests.fakes.session_exec_redis import FakeRedis

from app.core.account import session_exec


def config(**overrides):
    values = {
        "max_concurrency": 2,
        "defer_if_busy": True,
        "parallel_stages": frozenset({"review", "ads", "join"}),
        "queued_stages": frozenset({"review", "ads", "join"}),
        "priority_stages": frozenset({"ads", "join"}),
        "priority_reserved_slots": 1,
        "poll_interval": 0.002,
    }
    values.update(overrides)
    return values


@contextlib.asynccontextmanager
async def owner(execute, **overrides):
    client = FakeRedis()
    server = asyncio.create_task(
        session_exec.serve_requests(client, owns=lambda _: True, execute_stage=execute, **config(**overrides))
    )
    tasks = []

    def submit(account, stage, timeout=2):
        task = asyncio.create_task(session_exec.forward(account, stage, client=client, timeout=timeout))
        tasks.append(task)
        return task

    try:
        yield client, submit, server
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        server.cancel()
        await asyncio.gather(server, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("protected", ["ads", "join"])
async def test_r6_slow_protected_account_does_not_block_other_review(protected):
    entered, release = asyncio.Event(), asyncio.Event()

    async def execute(account, stage):
        if account == 1:
            entered.set()
            await release.wait()
        return {"processed": 1}, 30

    async with owner(execute) as (_, submit, _):
        first = submit(1, protected)
        await asyncio.wait_for(entered.wait(), 1)
        other = await asyncio.wait_for(submit(2, "review"), 0.15)
        assert other["result"]["processed"] == 1
        assert not first.done()
        release.set()
        await first


@pytest.mark.asyncio
async def test_same_account_waiter_cannot_occupy_another_accounts_slot():
    entered, release = asyncio.Event(), asyncio.Event()
    running = set()
    peak = 0

    async def execute(account, stage):
        nonlocal peak
        assert account not in running
        running.add(account)
        peak = max(peak, len(running))
        try:
            if account == 1 and stage == "ads":
                entered.set()
                await release.wait()
            return {"processed": 1}, 30
        finally:
            running.remove(account)

    async with owner(execute) as (_, submit, _):
        first = submit(1, "ads")
        await asyncio.wait_for(entered.wait(), 1)
        same = submit(1, "join")
        await asyncio.sleep(0.02)
        other = await asyncio.wait_for(submit(2, "review"), 0.15)
        assert other["result"]["processed"] == 1
        assert not same.done()
        assert peak <= 2
        release.set()
        await asyncio.gather(first, same)


@pytest.mark.asyncio
async def test_fractional_forward_timeout_removes_unclaimed_request():
    client = FakeRedis()
    result = await session_exec.forward(1, "ads", client=client, timeout=0.04)
    assert result is None
    assert client.brpop_timeouts and min(client.brpop_timeouts) > 0
    client.assert_channel_empty()


@pytest.mark.asyncio
async def test_caller_cancellation_unwinds_running_stage_and_queue():
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def execute(account, stage):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    async with owner(execute, max_concurrency=1) as (client, submit, _):
        running = submit(1, "ads")
        await asyncio.wait_for(entered.wait(), 1)
        queued = submit(2, "review")
        await asyncio.sleep(0.02)
        queued.cancel()
        running.cancel()
        await asyncio.gather(queued, running, return_exceptions=True)
        await asyncio.wait_for(cleaned.wait(), 0.15)
        await asyncio.sleep(0.02)
        client.assert_channel_empty()


@pytest.mark.asyncio
async def test_single_slot_slow_stage_times_out_then_review_runs(monkeypatch):
    monkeypatch.setattr(session_exec, "STAGE_TIMEOUT_SECONDS", 0.05)
    started, cleaned = asyncio.Event(), asyncio.Event()

    async def execute(account, stage):
        if account == 1:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        return {"processed": 1}, 30

    async with owner(execute, max_concurrency=1) as (client, submit, _):
        slow = submit(1, "ads")
        await asyncio.wait_for(started.wait(), 1)
        review = submit(2, "review")
        result = await asyncio.wait_for(review, 0.2)
        assert result["result"]["processed"] == 1
        timed_out = await slow
        assert timed_out["result"]["reason"] == "session_owner_stage_timeout"
        assert timed_out["result"]["wait_phase"] == "execution"
        assert cleaned.is_set()
        client.assert_channel_empty()


@pytest.mark.asyncio
async def test_single_slot_priority_stream_cannot_starve_review():
    entered, release = asyncio.Event(), asyncio.Event()
    order = []

    async def execute(account, stage):
        order.append((account, stage))
        if account == 1:
            entered.set()
            await release.wait()
        await asyncio.sleep(0.002)
        return {"processed": 1}, 30

    async with owner(execute, max_concurrency=1) as (client, submit, _):
        first = submit(1, "ads")
        await asyncio.wait_for(entered.wait(), 1)
        tasks = [submit(2, "ads"), submit(3, "join"), submit(4, "ads"), submit(9, "review")]
        await asyncio.sleep(0.025)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(first, *tasks), 0.3)
        assert all(item["result"]["processed"] == 1 for item in results)
        assert order[:2] == [(1, "ads"), (9, "review")]
        client.assert_channel_empty()


@pytest.mark.asyncio
async def test_priority_accounts_rotate_before_reusing_same_accounts_other_stage():
    entered, release = asyncio.Event(), asyncio.Event()
    order = []

    async def execute(account, stage):
        order.append((account, stage))
        if len(order) == 1:
            entered.set()
            await release.wait()
        return {"processed": 1}, 30

    async with owner(execute, max_concurrency=1) as (_, submit, _):
        first = submit(1, "ads")
        await asyncio.wait_for(entered.wait(), 1)
        same = submit(1, "join")
        other = submit(2, "ads")
        await asyncio.sleep(0.02)
        release.set()
        await asyncio.wait_for(asyncio.gather(first, same, other), 0.2)
        assert order == [(1, "ads"), (2, "ads"), (1, "join")]


@pytest.mark.asyncio
async def test_background_does_not_consume_reserved_protected_slot():
    entered, protected_entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    active = set()
    peak = 0
    background_peak = 0

    async def execute(account, stage):
        nonlocal peak, background_peak
        active.add((account, stage))
        peak = max(peak, len(active))
        background_peak = max(background_peak, sum(s == "review" for _, s in active))
        if account == 1:
            entered.set()
            await release.wait()
        if stage == "ads":
            protected_entered.set()
        active.remove((account, stage))
        return {"processed": 1}, 30

    async with owner(execute) as (_, submit, _):
        first = submit(1, "review")
        await asyncio.wait_for(entered.wait(), 1)
        waiting = submit(2, "review")
        await asyncio.sleep(0.02)
        assert not waiting.done()
        protected = submit(3, "ads")
        await asyncio.wait_for(protected_entered.wait(), 0.15)
        assert not waiting.done()
        assert (await protected)["result"]["processed"] == 1
        release.set()
        await asyncio.gather(first, waiting)
        assert peak <= 2 and background_peak == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("same_account,phase", [(True, "account_session"), (False, "executor_capacity")])
async def test_bounded_queue_timeout_reports_wait_phase(same_account, phase):
    entered = asyncio.Event()

    async def execute(account, stage):
        entered.set()
        await asyncio.Event().wait()

    async with owner(execute, max_concurrency=1, queue_timeout=0.04) as (client, submit, _):
        first = submit(1, "ads")
        await asyncio.wait_for(entered.wait(), 1)
        reply = await asyncio.wait_for(submit(1 if same_account else 2, "review"), 0.2)
        assert reply["result"]["reason"] == "session_owner_queue_timeout"
        assert reply["result"]["wait_phase"] == phase
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        await asyncio.sleep(0.02)
        client.assert_channel_empty()


@pytest.mark.asyncio
async def test_owner_shutdown_replies_and_unwinds_all_stages_and_requests():
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def execute(account, stage):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    async with owner(execute, max_concurrency=1) as (client, submit, server):
        first = submit(1, "ads")
        await asyncio.wait_for(entered.wait(), 1)
        queued = submit(2, "review")
        await asyncio.sleep(0.02)
        server.cancel()
        await asyncio.gather(server, return_exceptions=True)
        results = await asyncio.wait_for(asyncio.gather(first, queued), 0.2)
        assert all(item["result"]["reason"] == "session_owner_shutdown" for item in results)
        assert cleaned.is_set()
        client.assert_channel_empty()


@pytest.mark.asyncio
async def test_cancelling_slot_stays_occupied_until_session_cleanup_finishes():
    entered, cleaning, cleanup_release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    second_entered = asyncio.Event()
    cancellations = 0

    async def execute(account, stage):
        nonlocal cancellations
        if account == 1 and stage == "ads":
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellations += 1
                cleaning.set()
                await cleanup_release.wait()
                raise
        if account == 1:
            second_entered.set()
        return {"processed": 1}, 30

    async with owner(execute) as (client, submit, _):
        first = submit(1, "ads")
        await asyncio.wait_for(entered.wait(), 1)
        same = submit(1, "join")
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        await asyncio.wait_for(cleaning.wait(), 0.15)
        other = await asyncio.wait_for(submit(2, "review"), 0.15)
        assert other["result"]["processed"] == 1
        assert not second_entered.is_set()
        assert cancellations == 1
        cleanup_release.set()
        await asyncio.wait_for(same, 0.15)
        assert second_entered.is_set()
        client.assert_channel_empty()


@pytest.mark.asyncio
async def test_atomic_reply_cannot_leave_orphan_after_caller_cleanup():
    reply_entered, release_reply = asyncio.Event(), asyncio.Event()

    async def execute(account, stage):
        return {"processed": 1}, 30

    async with owner(execute) as (client, submit, _):
        original_eval = client.eval

        async def paused_eval(script, *args):
            if "session_exec_reply_if_active_v1" in script:
                reply_entered.set()
                await release_reply.wait()
            return await original_eval(script, *args)

        client.eval = paused_eval
        task = submit(1, "ads")
        await asyncio.wait_for(reply_entered.wait(), 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        release_reply.set()
        await asyncio.sleep(0.02)
        client.assert_channel_empty()


@pytest.mark.asyncio
async def test_stage_that_swallows_deadline_is_not_reported_as_completed(monkeypatch):
    monkeypatch.setattr(session_exec, "STAGE_TIMEOUT_SECONDS", 0.02)

    async def execute(account, stage):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.01)
            return {"processed": 1}, 30

    async with owner(execute) as (client, submit, _):
        result = await asyncio.wait_for(submit(1, "ads"), 0.2)
        assert result["result"]["reason"] == "session_owner_stage_timeout"
        client.assert_channel_empty()


@pytest.mark.asyncio
async def test_duplicate_stage_is_not_queued_or_executed_twice():
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def execute(account, stage):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return {"processed": 1}, 30

    async with owner(execute) as (_, submit, _):
        first = submit(1, "ads")
        await asyncio.wait_for(entered.wait(), 1)
        duplicate = await asyncio.wait_for(submit(1, "ads"), 0.15)
        assert duplicate["result"]["reason"] == "session_owner_duplicate_wait"
        assert calls == 1
        release.set()
        await first


@pytest.mark.asyncio
async def test_full_queue_defers_without_leaving_request():
    entered = asyncio.Event()

    async def execute(account, stage):
        entered.set()
        await asyncio.Event().wait()

    async with owner(execute, max_concurrency=1) as (client, submit, _):
        tasks = [submit(1, "ads")]
        await asyncio.wait_for(entered.wait(), 1)
        tasks.extend(submit(account, "review") for account in range(2, 6))
        await asyncio.sleep(0.03)
        overflow = await asyncio.wait_for(submit(6, "ads"), 0.15)
        assert overflow["result"]["reason"] == "session_owner_capacity_wait"
        assert overflow["result"]["wait_phase"] == "executor_capacity"
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0.02)
        client.assert_channel_empty()
