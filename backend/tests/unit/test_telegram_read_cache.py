from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.core.account.read_cache import (
    FeatureFlags,
    ResolveUsernameGuard,
    TelegramReadCache,
    make_account_actor_reader,
)


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.setex_calls: list[tuple[str, int, str]] = []
        self.deleted: list[str] = []

    async def get(self, key: str):
        return self.values.get(key)

    async def setex(self, key: str, ttl: int, value: str):
        self.values[key] = value
        self.ttls[key] = ttl
        self.setex_calls.append((key, ttl, value))
        return True

    async def delete(self, *keys: str):
        for key in keys:
            self.values.pop(key, None)
            self.deleted.append(key)
        return len(keys)


@pytest.mark.asyncio
async def test_concurrent_username_waiters_share_one_rate_token():
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def actor_reader(**_):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {"data": {"id": 123}, "source_version": 2}

    guard = ResolveUsernameGuard(refill_per_second=0)
    cache = TelegramReadCache(actor_reader, redis_client=FakeRedis(), flags=FeatureFlags(entity_cache=True), guard=guard)
    tasks = [asyncio.create_task(cache.resolve_entity(1, "same")) for _ in range(10)]
    await started.wait()
    release.set()
    results = await asyncio.gather(*tasks)
    assert all(result.status == "fresh" for result in results)
    assert calls == 1
    assert guard.state(1)["tokens"] == 4


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_shared_read():
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def actor_reader(**_):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {"data": True, "source_version": 1}

    cache = TelegramReadCache(actor_reader, redis_client=FakeRedis(), flags=FeatureFlags(group_fact_cache=True))
    first = asyncio.create_task(cache.get_membership(1, "target"))
    second = asyncio.create_task(cache.get_membership(1, "target"))
    await started.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    third = asyncio.create_task(cache.get_membership(1, "target"))
    release.set()
    assert all(r.status == "fresh" for r in await asyncio.gather(second, third))
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("source_version", [None, 1])
async def test_old_or_unversioned_actor_result_cannot_authorize_newer_fact(source_version):
    async def actor_reader(**_):
        return {"data": {"member": True}, "source_version": source_version}

    cache = TelegramReadCache(actor_reader, redis_client=FakeRedis(), flags=FeatureFlags(group_fact_cache=True))
    result = await cache.get_membership(1, "target", local_fact_version=2)
    assert not result.usable_for_send
    assert result.status == "unknown"


@pytest.mark.asyncio
async def test_actor_pool_released_when_client_missing():
    released = []
    wrapper = SimpleNamespace(client=None)

    class Pool:
        async def acquire_by_id(self, *_args, **_kwargs):
            return wrapper

        async def release(self, value):
            released.append(value)

    cache = TelegramReadCache(make_account_actor_reader(Pool()))
    assert (await cache.get_membership(1, "target")).status == "unknown"
    assert released == []


@pytest.mark.asyncio
async def test_another_cache_invalidation_invalidates_local_hit():
    redis = FakeRedis()
    calls = 0

    async def actor_reader(**_):
        nonlocal calls
        calls += 1
        return {"data": {"member": calls == 1}, "source_version": calls}

    first = TelegramReadCache(actor_reader, redis_client=redis, flags=FeatureFlags(group_fact_cache=True))
    second = TelegramReadCache(actor_reader, redis_client=redis, flags=FeatureFlags(group_fact_cache=True))
    await first.get_membership(1, "target")
    await second.invalidate({"account_id": 1, "target_key": "target", "event_type": "membership"})
    result = await first.get_membership(1, "target")
    assert result.data["member"] is False
    assert calls == 2


@pytest.mark.asyncio
async def test_entity_cache_uses_actor_once_and_writes_documented_ttl():
    redis = FakeRedis()
    calls: list[dict] = []

    async def actor_reader(**payload):
        calls.append(payload)
        await asyncio.sleep(0)
        return {"telegram_id": 123, "source_version": 4}

    cache = TelegramReadCache(
        actor_reader,
        redis_client=redis,
        flags=FeatureFlags(entity_cache=True),
    )

    first = await cache.resolve_entity(2, "@Example_User")
    second = await cache.resolve_entity(2, "example_user")

    assert first.status == "fresh"
    assert first.data["telegram_id"] == 123
    assert second.source in {"local", "redis"}
    assert len(calls) == 1
    assert redis.ttls["tg:entity:2:username:example_user"] == 6 * 60 * 60


@pytest.mark.asyncio
async def test_single_flight_coalesces_concurrent_reads():
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def actor_reader(**payload):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {"ok": True}

    cache = TelegramReadCache(
        actor_reader,
        flags=FeatureFlags(group_fact_cache=True),
    )
    tasks = [asyncio.create_task(cache.get_membership(7, "group-a")) for _ in range(8)]
    await started.wait()
    release.set()
    results = await asyncio.gather(*tasks)

    assert calls == 1
    assert all(result.status == "fresh" for result in results)


@pytest.mark.asyncio
async def test_stale_value_is_returned_only_after_actor_failure():
    now = [100.0]
    redis = FakeRedis()
    failures = [False]

    async def actor_reader(**payload):
        if failures[0]:
            raise TimeoutError("telegram timeout")
        return {"value": "known", "source_version": 1}

    cache = TelegramReadCache(
        actor_reader,
        redis_client=redis,
        flags=FeatureFlags(group_fact_cache=True),
        clock=lambda: now[0],
    )
    fresh = await cache.get_membership(1, "g")
    assert fresh.status == "fresh"
    now[0] += 5 * 60 + 1
    failures[0] = True
    stale = await cache.get_membership(1, "g")

    assert stale.status == "stale"
    assert not stale.usable_for_send
    assert stale.data["value"] == "known"


@pytest.mark.asyncio
async def test_invalidate_removes_entity_and_group_fact_keys():
    redis = FakeRedis()

    async def actor_reader(**payload):
        return {"value": True}

    cache = TelegramReadCache(
        actor_reader,
        redis_client=redis,
        flags=FeatureFlags(entity_cache=True, group_fact_cache=True),
    )
    await cache.resolve_entity(4, "foo")
    await cache.get_membership(4, "foo")
    count = await cache.invalidate(
        {"account_id": 4, "target_key": "foo", "event_type": "username_changed", "peer_id": 99}
    )

    assert count == 2
    assert "tg:entity:4:username:foo" in redis.deleted
    assert "tg:entity:4:peer:99" in redis.deleted


def test_resolve_username_guard_opens_after_three_flood_waits_and_allows_probe():
    now = [0.0]
    guard = ResolveUsernameGuard(clock=lambda: now[0])

    assert all(guard.allow(1) for _ in range(5))
    assert not guard.allow(1)
    now[0] = 1.0
    guard.record_flood_wait(1, 5)
    guard.record_flood_wait(1, 5)
    guard.record_flood_wait(1, 5)
    assert not guard.allow(1)
    now[0] = 62.0
    assert guard.allow(1)
    assert not guard.allow(1)
    guard.record_success(1)
    assert guard.state(1)["open"] is False


def test_resolve_guard_reopens_after_failed_half_open_probe():
    now = [0.0]
    guard = ResolveUsernameGuard(clock=lambda: now[0])
    for _ in range(3):
        guard.record_flood_wait(1, 60)
    now[0] = 70.0
    assert guard.allow(1)
    guard.record_flood_wait(1, 90)
    assert not guard.allow(1)
    assert guard.state(1)["open_until"] == 160.0


@pytest.mark.asyncio
async def test_invalidation_during_read_does_not_publish_old_fact():
    started, release = asyncio.Event(), asyncio.Event()

    async def reader(**_):
        started.set()
        await release.wait()
        return {"data": {"member": True}, "source_version": 1}

    redis = FakeRedis()
    cache = TelegramReadCache(reader, redis_client=redis, flags=FeatureFlags(group_fact_cache=True))
    reading = asyncio.create_task(cache.get_membership(1, "target"))
    await started.wait()
    await cache.invalidate({"account_id": 1, "target_key": "target", "event_type": "membership"})
    release.set()
    assert (await reading).status == "unknown"
    assert cache.group_fact_key(1, "target", "membership") not in redis.values


@pytest.mark.asyncio
async def test_invalidate_listener_fact_and_deleted_message_keys_precisely():
    redis = FakeRedis()

    async def actor_reader(**payload):
        return {"value": True}

    cache = TelegramReadCache(
        actor_reader,
        redis_client=redis,
        flags=FeatureFlags(group_fact_cache=True, survival_cache=True),
    )
    await cache.get_membership(2, -100123)
    await cache.get_ad_survival(2, -100123, 77)
    count = await cache.invalidate(
        {
            "account_id": 2,
            "target_key": -100123,
            "event_type": "membership",
            "message_key": None,
        }
    )
    assert count == 2
    assert cache.group_fact_key(2, -100123, "membership") in redis.deleted
    assert cache.group_fact_key(2, -100123, "permission") in redis.deleted

    await cache.get_ad_survival(2, -100123, 77)
    count = await cache.invalidate(
        {"account_id": 2, "target_key": -100123, "event_type": "deleted", "ids": [77]}
    )
    assert count == 1
    assert cache.survival_key(2, -100123, 77) in redis.deleted


@pytest.mark.asyncio
async def test_account_actor_reader_owns_pool_read_and_release():
    calls = []

    class Client:
        async def get_entity(self, target):
            calls.append(("entity", target))
            return SimpleNamespace(id=123)

        async def get_permissions(self, entity, user):
            calls.append(("permissions", entity.id, user))
            return SimpleNamespace(has_left=False)

        async def get_messages(self, entity, ids):
            calls.append(("messages", entity.id, ids))
            return SimpleNamespace(id=ids)

    wrapper = SimpleNamespace(client=Client(), get_client=lambda: wrapper.client)

    class Pool:
        def __init__(self):
            self.released = []

        async def acquire_by_id(self, account_id, **kwargs):
            assert account_id == 2
            assert kwargs["purpose"] == "p3_read:membership"
            return wrapper

        async def release(self, value):
            self.released.append(value)

    pool = Pool()
    cache = TelegramReadCache(
        make_account_actor_reader(pool),
        flags=FeatureFlags(group_fact_cache=True),
    )
    result = await cache.get_membership(2, "group", force_refresh=True)
    assert result.status == "unknown"
    assert pool.released == []
    assert calls == []


@pytest.mark.asyncio
async def test_qualification_cache_has_24_hour_ttl_and_disabled_is_unknown():
    redis = FakeRedis()
    value = {"snapshot_id": "qs:1", "verdict": "allowed"}
    enabled = TelegramReadCache(
        lambda **_: None,
        redis_client=redis,
        flags=FeatureFlags(qualification_snapshot=True),
    )
    await enabled.put_qualification("group", "3", "rule", "hash", value)
    key = enabled.qualification_key("group", "3", "rule", "hash")
    assert redis.ttls[key] == 24 * 60 * 60
    assert (await enabled.get_qualification("group", "3", "rule", "hash")).data["snapshot_id"] == "qs:1"
    disabled = TelegramReadCache(lambda **_: None, flags=FeatureFlags())
    assert (await disabled.get_qualification("group", "3", "rule", "hash")).status == "unknown"
