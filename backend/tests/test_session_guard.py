from __future__ import annotations

import pytest

from app.core.account.session_guard import AccountSessionGuard, SessionQuarantined


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool:
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def eval(self, script: str, numkeys: int, *rest) -> int:
        keys, argv = list(rest[:numkeys]), list(rest[numkeys:])
        lease_key, token = keys[0], argv[0]
        if self.values.get(lease_key) != token:
            return 0
        if "expire" in script:
            return 1
        for key in keys:
            self.values.pop(key, None)
        return 1


@pytest.mark.asyncio
async def test_wrong_session_is_not_replayed() -> None:
    redis = FakeRedis()
    guard = AccountSessionGuard(redis=redis, account_id=42)
    assert await guard.acquire()
    calls = 0

    async def operation() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("Server replied with a wrong session ID")

    with pytest.raises(SessionQuarantined):
        await guard.execute(operation)
    assert calls == 1
    assert guard.quarantined
    assert guard.lease.key not in redis.values

