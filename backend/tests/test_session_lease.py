from __future__ import annotations

import pytest

from app.core.account import session_lease
from app.core.account.session_lease import PROCESS_ID, SessionLease


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expiry: dict[str, int] = {}

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool:
        if nx and key in self.values:
            return False
        self.values[key] = value
        self.expiry[key] = ex or 0
        return True

    async def get(self, key: str):
        return self.values.get(key)

    async def eval(self, script: str, numkeys: int, *rest) -> int:
        keys, argv = list(rest[:numkeys]), list(rest[numkeys:])
        if "quarantine_publish_sentinel_v1" in script:
            current, previous = self.values.get(keys[0]), self.values.get(keys[2])
            if (current == argv[1] or (current is None and not argv[2] and previous in (None, argv[0]))
                    or (argv[2] and current == argv[2] and self.values.get(keys[1]) == argv[3])):
                self.values[keys[2]] = argv[0]
                self.values[keys[0]] = argv[1]
                self.expiry[keys[0]] = 0
                self.values.pop(keys[1], None)
                return 1
            return 0
        if "quarantine_probe_claim_v1" in script:
            if self.values.get(keys[0]) != argv[0] or self.values.get(keys[2]) != argv[1]:
                return 0
            self.values[keys[0]], self.values[keys[1]] = argv[2], argv[4]
            self.expiry[keys[0]] = int(argv[3])
            return 1
        if "quarantine_probe_release_v1" in script:
            if self.values.get(keys[0]) != argv[0]:
                return 0
            self.values[keys[0]], self.values[keys[2]] = argv[1], argv[2]
            self.expiry[keys[0]] = 0
            self.values.pop(keys[1], None)
            return 1
        if "quarantine_compare_delete_v1" in script:
            if self.values.get(keys[0]) not in (None, argv[0]) or self.values.get(keys[1]) not in (None, argv[1]):
                return 0
            for key in keys:
                self.values.pop(key, None)
            return 1
        lease_key = keys[0]
        owner_key = keys[1] if len(keys) > 1 else None
        token = argv[0]
        if self.values.get(lease_key) != token:
            return 0
        if "expire" in script:
            self.expiry[lease_key] = int(argv[1])
            if owner_key is not None:
                self.values[owner_key] = argv[2]
                self.expiry[owner_key] = int(argv[1])
            return 1
        self.values.pop(lease_key, None)
        if owner_key is not None:
            self.values.pop(owner_key, None)
        return 1


@pytest.fixture(autouse=True)
def isolated_quarantine_store(monkeypatch, tmp_path):
    monkeypatch.setattr("app.core.account.session_quarantine._root", lambda: tmp_path)


@pytest.mark.asyncio
async def test_only_one_owner_and_stale_release_is_safe() -> None:
    redis = FakeRedis()
    first = SessionLease(redis, 7)
    second = SessionLease(redis, 7)

    assert await first.acquire()
    assert not await second.acquire()
    assert await first.renew()

    # A stale owner cannot delete a lease acquired by another owner.
    await first.release()
    assert await second.acquire()
    assert not await first.release()
    assert redis.values[second.key] == second.token


@pytest.mark.asyncio
async def test_owner_registry_follows_the_lease() -> None:
    redis = FakeRedis()
    lease = SessionLease(redis, 9)

    assert await lease.acquire()
    assert redis.values[lease.owner_key] == PROCESS_ID
    assert await lease.renew()
    assert redis.values[lease.owner_key] == PROCESS_ID

    # A foreign marker under our still-valid lease is stale garbage; the
    # token-guarded release clears it together with the lease.
    redis.values[lease.owner_key] = "someone-else"
    assert await lease.release()
    assert lease.key not in redis.values
    assert lease.owner_key not in redis.values

