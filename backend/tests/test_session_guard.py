from __future__ import annotations

import pytest
from telethon.errors import AuthKeyDuplicatedError

from app.core.account.session_guard import (
    AccountSessionGuard,
    SessionQuarantined,
    install_session_failure_hook,
    is_wrong_session_error,
)


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool:
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def get(self, key: str):
        return self.values.get(key)

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


@pytest.mark.asyncio
async def test_lease_loss_while_waiting_on_gate_fences_rpc() -> None:
    redis = FakeRedis()
    gate = AccountSessionGuard(redis=redis, account_id=43)
    assert await gate.acquire()
    # Simulate a replacement process taking the distributed lease while the
    # old owner is waiting for its local account lock.
    redis.values[gate.lease.key] = "replacement-generation"
    with pytest.raises(SessionQuarantined):
        await gate.assert_owned(expected_token=gate.lease.token)


def test_typed_telethon_auth_key_failure_is_a_wrong_session() -> None:
    assert is_wrong_session_error(AuthKeyDuplicatedError(None))


@pytest.mark.asyncio
async def test_update_decoder_quarantines_wrong_session_once() -> None:
    class State:
        def decrypt_message_data(self, _body):
            raise RuntimeError("Server replied with a wrong session ID (see FAQ for details)")

    quarantined: list[BaseException] = []

    class Guard:
        async def _quarantine(self, error: BaseException) -> None:
            quarantined.append(error)

    state = State()
    client = type("Client", (), {})()
    client._sender = type("Sender", (), {"_state": state})()
    client._vanguard_session_guard = Guard()

    install_session_failure_hook(client)
    with pytest.raises(RuntimeError, match="wrong session ID"):
        state.decrypt_message_data(b"first")
    with pytest.raises(RuntimeError, match="wrong session ID"):
        state.decrypt_message_data(b"second")

    await client._vanguard_session_failure_task
    assert len(quarantined) == 1
    assert client._vanguard_session_quarantined is True

