"""Work reservations use actual Redis Lua, without Telegram or production data."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.core.account import read_work
from app.core.account import rpc_governor as rpc
from app.core.account.reserved_reads import RESERVED_BUDGET_LUA, reservation_args
from app.modules.acquisition.group_qualification import EvidenceCollector
from tests.integration.test_growth_account_scheduler import (
    queue_store,  # noqa: F401 -- pytest fixture
)
from tests.unit.test_evidence_progress import ResumeClient
from tests.unit.test_group_qualification_sampling import ENTITY, NOW, message


async def allocate(
    store,
    *,
    cost,
    token="",
    mode="read",
    group=40,
    scope="member:epoch",
    kind="review",
    lane="critical",
    holds=None,
):
    limits = rpc.limits_for({}, datetime.utcnow())
    limits.update(holds or {})
    keys, args = reservation_args(
        2,
        limits,
        lane,
        cost,
        0,
        workload=kind,
        work_token=token,
        work_mode=mode,
        work_group=group,
        work_scope=scope,
        work_kind=kind,
    )
    return await store.eval(RESERVED_BUDGET_LUA, len(keys), *keys, *args)


@pytest.mark.asyncio
async def test_reservation_is_atomic_without_precharging_and_preserves_ads(queue_store):
    claims = await asyncio.gather(
        *(
            allocate(queue_store, cost=36, token=str(i), mode="reserve", kind="join")
            for i in range(20)
        )
    )
    assert claims.count(0) == 1
    assert await queue_store.get("vanguard:rpc:2:hour") is None
    assert await allocate(queue_store, cost=40) > 0
    assert await allocate(queue_store, cost=84, lane="ad") == 0
    assert int(await queue_store.get("vanguard:rpc:2:hour")) == 84


@pytest.mark.asyncio
async def test_join_handoff_is_membership_scoped_and_cannot_be_stolen(queue_store, monkeypatch):
    monkeypatch.setattr("app.core.redis.get_redis", AsyncMock(return_value=queue_store))
    assert await allocate(queue_store, cost=36, token="joining", mode="reserve", kind="join") == 0
    assert await allocate(queue_store, cost=12, token="joining", kind="join") == 0
    work = read_work.ReadWork(2, 40, "join", token="joining", claimed=True)
    await read_work.handoff(work, "60:joined-at")
    assert work.handed_off
    assert await queue_store.hget(read_work.key(2), "remaining") == "24"
    assert (
        await allocate(queue_store, cost=24, token="wrong", mode="reserve", scope="60:old-join") > 0
    )
    assert (
        await allocate(
            queue_store, cost=24, token="reviewing", mode="reserve", scope="60:joined-at"
        )
        == 0
    )
    assert await allocate(queue_store, cost=24, token="reviewing") == 0
    assert int(await queue_store.get("vanguard:rpc:2:hour")) == 36
    assert await queue_store.hget(read_work.key(2), "remaining") == "0"
    assert await allocate(queue_store, cost=1, token="reviewing") > 0


@pytest.mark.asyncio
async def test_three_remaining_reads_cannot_start_collection_or_reset_counters(queue_store):
    for suffix in (
        "hour",
        "day",
        "join_reserved_hour",
        "join_reserved_day",
        "critical_review_hour",
        "critical_review_day",
    ):
        await queue_store.set(f"vanguard:rpc:2:{suffix}", 69, ex=1900)
    assert await allocate(queue_store, cost=24, token="review", mode="reserve") > 0
    assert await queue_store.get("vanguard:rpc:2:hour") == "69"
    assert 1898 <= await queue_store.ttl("vanguard:rpc:2:hour") <= 1900
    assert not await queue_store.exists(read_work.key(2))


@pytest.mark.asyncio
async def test_abandoned_reservation_expires_without_refunding_spent_reads(queue_store):
    assert await allocate(queue_store, cost=24, token="one", mode="reserve") == 0
    assert await allocate(queue_store, cost=3, token="one") == 0
    await queue_store.pexpire(read_work.key(2), 1)
    await asyncio.sleep(0.02)
    assert await allocate(queue_store, cost=24, token="two", mode="reserve") == 0
    assert await queue_store.get("vanguard:rpc:2:hour") == "3"


@pytest.mark.asyncio
async def test_real_governor_records_cost_and_releases_on_cancellation(queue_store, monkeypatch):
    monkeypatch.setattr("app.core.redis.get_redis", AsyncMock(return_value=queue_store))

    @asynccontextmanager
    async def db():
        yield object()

    monkeypatch.setattr("app.core.database.get_db_session", db)
    monkeypatch.setattr(rpc, "load_state", AsyncMock(return_value={}))
    limits = rpc.limits_for({}, datetime.utcnow())
    monkeypatch.setattr(
        rpc, "snapshot", AsyncMock(return_value={"state": "ready", "limits": limits})
    )
    with pytest.raises(asyncio.CancelledError):
        async with read_work.operation(2, 40, "review") as work:
            governor = rpc.RpcGovernor(2, lambda: "group_qualification")
            await governor.before([])
            assert work.claimed and work.reads == 0
            await governor.before(["users.GetUsersRequest", "updates.GetStateRequest"])
            await governor.failed(["users.GetUsersRequest"], ValueError("synthetic"))
            raise asyncio.CancelledError
    assert not await queue_store.exists(read_work.key(2))
    assert await queue_store.get("vanguard:rpc:2:hour") == "2"
    sample = json.loads((await queue_store.lrange(read_work.sample_key(2, "review"), 0, 0))[0])
    assert sample["reads"] == 2 and sample["outcome"] == "interrupted"
    assert sample["failures"] == {"ValueError": 1}


@pytest.mark.asyncio
async def test_interrupted_history_retains_message_ids_for_next_pass():
    history = [message(100 - i, sender=10 + i) for i in range(10)]

    class Interrupted(ResumeClient):
        async def iter_messages(self, entity, **kwargs):
            async for item in super().iter_messages(entity, **kwargs):
                yield item
                if "offset_date" in kwargs and item.id == 97:
                    raise rpc.RpcDeferred("telegram_read_slice", 60)

    first = await EvidenceCollector(Interrupted(history)).collect(ENTITY, now=NOW)
    assert first["collection_halted"] == "telegram_read_slice"
    assert first["collection_progress"]["pending_message_ids"] == [100, 99, 98, 97]
    assert first["collection_progress"]["history_cursors"][0]["offset_id"] == 97
    client = ResumeClient(history)
    collector = EvidenceCollector(client)
    collector.previous = {**first, "collected_at": NOW.isoformat()}
    await collector.collect(ENTITY, now=NOW + timedelta(minutes=1))
    assert client.target_reads[0] == [100, 99, 98, 97]


def test_cost_estimate_does_not_learn_from_incomplete_attempts():
    now = 1000000
    failures = [json.dumps({"reads": 2, "at": now, "outcome": "technical_wait"})] * 100
    assert read_work.estimate(failures, 24, now) == 24
    completed = [json.dumps({"reads": 30, "at": now, "outcome": "observe"})] * 20
    assert read_work.estimate(failures + completed, 24, now) == 32


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["survival", "sync"])
async def test_reserved_loan_stays_available_and_never_takes_ad_guarantee(queue_store, source):
    # Critical's base and elastic pool are spent. Reserve 24 from a proven idle
    # source, let ads use all 84 guaranteed reads, then consume the reservation
    # after its original demand forecast is no longer available.
    for suffix, value in (
        ("hour", 72),
        ("day", 72),
        ("join_reserved_hour", 72),
        ("join_reserved_day", 72),
        ("critical_join_hour", 36),
        ("critical_review_hour", 36),
    ):
        await queue_store.set(f"vanguard:rpc:2:{suffix}", value, ex=1800)
    holds = {
        f"{source}_hold_hour": 12 if source == "survival" else 24,
        f"{source}_hold_day": 120 if source == "survival" else 240,
    }
    assert await allocate(queue_store, cost=24, token="review", mode="reserve", holds=holds) == 0
    assert await queue_store.get(f"vanguard:rpc:2:{source}_lent_hour") is None
    assert await allocate(queue_store, cost=84, lane="ad") == 0
    assert await allocate(queue_store, cost=24, token="review") == 0
    assert await queue_store.get("vanguard:rpc:2:hour") == "180"
    assert await queue_store.get(f"vanguard:rpc:2:{source}_lent_hour") == "24"
    assert 1797 <= await queue_store.ttl("vanguard:rpc:2:hour") <= 1800


@pytest.mark.asyncio
async def test_latest_manual_result_supersedes_old_join_pending(test_db):
    from app.modules.acquisition.models import GroupQualificationAudit
    from tests.unit.test_qualification_service import setup

    account, group, member, old = await setup(test_db, decision="technical_wait")
    old.batch_id = "join-attempt:40"
    await test_db.commit()
    assert (await read_work.initial_pending(test_db, account.id, 99))[0] == group.id
    test_db.add(
        GroupQualificationAudit(
            batch_id="manual-newer",
            membership_id=member.id,
            account_id=account.id,
            group_id=group.id,
            membership_joined_at=member.joined_at,
            policy_version=old.policy_version,
            content_scope=old.content_scope,
            decision="allowed",
            state="completed",
        )
    )
    await test_db.commit()
    assert await read_work.initial_pending(test_db, account.id, 99) is None


@pytest.mark.asyncio
async def test_ready_first_review_can_adopt_its_reservation_without_reforecast(monkeypatch):
    current = {
        "state": "ready",
        "limits": {},
        "lanes": {"critical": {"remaining": 0, "retry_after_seconds": 1800}},
        "critical_workloads": {"review": {"remaining": 0, "retry_after_seconds": 1800}},
        "work_reservation": {
            "group_id": 40,
            "scope": "60:epoch",
            "kind": "review",
            "pending": True,
            "remaining": 24,
        },
    }
    monkeypatch.setattr(rpc, "snapshot", AsyncMock(return_value=current))
    await rpc.check_read_ready(
        object(),
        2,
        purpose="group_qualification",
        requires_bootstrap=True,
        minimum_reads=24,
        work_group_id=40,
        work_scope="60:epoch",
    )
    with pytest.raises(rpc.RpcDeferred):
        await rpc.check_read_ready(
            object(),
            2,
            purpose="group_qualification",
            minimum_reads=24,
            work_group_id=40,
            work_scope="60:old",
        )


def test_slice_growth_only_applies_to_same_membership_and_is_bounded():
    row = {
        "group_id": 40,
        "scope": "60:epoch",
        "reads": 24,
        "reserved_reads": 24,
        "defer_reason": "telegram_read_slice",
    }
    assert (
        read_work.resumed_estimate(
            [json.dumps(row)], read_work.ReadWork(2, 40, "review", "60:epoch"), 24
        )
        == 32
    )
    assert (
        read_work.resumed_estimate(
            [json.dumps(row)], read_work.ReadWork(2, 40, "review", "60:new"), 24
        )
        == 24
    )
    row["reserved_reads"] = 48
    assert (
        read_work.resumed_estimate(
            [json.dumps(row)], read_work.ReadWork(2, 40, "review", "60:epoch"), 24
        )
        == 48
    )


@pytest.mark.asyncio
async def test_renewal_is_metered_with_its_own_ad_reservation(queue_store, monkeypatch):
    monkeypatch.setattr("app.core.redis.get_redis", AsyncMock(return_value=queue_store))

    @asynccontextmanager
    async def db():
        yield object()

    monkeypatch.setattr("app.core.database.get_db_session", db)
    monkeypatch.setattr(rpc, "load_state", AsyncMock(return_value={}))
    monkeypatch.setattr(rpc, "snapshot", AsyncMock(return_value={
        "state": "ready", "limits": rpc.limits_for({}, datetime.utcnow()),
    }))
    async with read_work.operation(2, 40, "renewal") as work:
        await rpc.RpcGovernor(2, lambda: "ad_qualification_refresh").before(
            ["users.GetUsersRequest"]
        )
        assert work.reads == 1 and work.claimed and work.limit == 16
        assert await queue_store.hget(read_work.key(2), "lane") == "1"
        assert await queue_store.get("vanguard:rpc:2:join_reserved_hour") is None
    assert not await queue_store.exists(read_work.key(2))
    assert len(await queue_store.lrange(read_work.sample_key(2, "renewal"), 0, -1)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason,ready,processed",
    [
        ("telegram_read_budget", True, 1),
        ("telegram_read_budget", False, 0),
        ("telegram_rpc_cooldown", True, 0),
        ("evidence_maturity_wait", True, 0),
    ],
)
async def test_only_obsolete_local_budget_deadline_can_wake_early(
    test_db, monkeypatch, reason, ready, processed
):
    from types import SimpleNamespace

    from app.modules.acquisition import qualification_service as service
    from tests.unit.test_qualification_service import setup

    account, _, _, row = await setup(test_db, decision="technical_wait")
    deadline = datetime.utcnow() + timedelta(hours=10)
    row.reason, row.next_retry_at = reason, deadline
    await test_db.commit()
    monkeypatch.setattr(service, "ensure_membership_reviews", AsyncMock())
    monkeypatch.setattr(
        rpc,
        "check_read_ready",
        AsyncMock(side_effect=None if ready else rpc.RpcDeferred("telegram_read_budget", 3600)),
    )
    monkeypatch.setattr("app.core.account.read_schedule.read_wait", AsyncMock(return_value=None))

    async def assessed(actor, account_id, group, *, row):
        row.state, row.next_retry_at = "completed", None
        return SimpleNamespace(passed=False, reason="reviewed")

    monkeypatch.setattr(service, "assess", assessed)
    result = await service.run_reviews(SimpleNamespace(db=test_db), account_id=account.id, limit=1)
    assert result["processed"] == processed
    if not processed:
        assert row.next_retry_at == deadline and row.attempts == 0


def test_slice_boundary_resumes_without_incrementing_technical_failure_backoff():
    from app.modules.acquisition.qualification_service import review_schedule
    now = datetime.utcnow()
    sample = {"reason": "telegram_read_slice", "retry_after_seconds": 60}
    decision, reason, state, retry = review_schedule(sample, {"technical_failures": 3}, now)
    assert (decision, reason, state) == ("technical_wait", "telegram_read_slice", "completed")
    assert retry == now + timedelta(seconds=60)
    assert sample["technical_failures"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("cost,allowed", [(1, True), (24, True), (43, True), (44, False)])
async def test_reservation_includes_existing_fairness_deficit_before_lending(queue_store, cost, allowed):
    # Production replay at 150%: critical has 120 unborrowed reads, while the
    # opposite workload still holds 121. An idle survival loan of 44 leaves
    # 43 genuinely available. The old max(0, room-hold) lost the 1-read deficit.
    values = {"day": 2120, "ad_day": 419, "survival_day": 260,
              "join_reserved_day": 818, "sync_reserved_day": 589,
              "flex_reserved_day": 34, "critical_join_day": 81,
              "critical_review_day": 737}
    for name, value in values.items():
        await queue_store.set(f"vanguard:rpc:2:{name}", value, ex=36000)
    limits = {"minute": 162, "hour": 324, "day": 3240, "survival_hold_day": 182}
    keys, args = reservation_args(2, limits, "critical", cost, 0, workload="review",
        work_token="replay", work_mode="reserve", work_group=40, work_kind="review")
    delay = await queue_store.eval(RESERVED_BUDGET_LUA, len(keys), *keys, *args)
    assert (delay == 0) is allowed
    assert await queue_store.get("vanguard:rpc:2:day") == "2120"
    assert 35995 <= await queue_store.ttl("vanguard:rpc:2:day") <= 36000
    if allowed:
        keys, args = reservation_args(2, limits, "critical", cost, 0,
            workload="review", work_token="replay")
        assert await queue_store.eval(RESERVED_BUDGET_LUA, len(keys), *keys, *args) == 0
        assert int(await queue_store.get("vanguard:rpc:2:day")) == 2120 + cost
