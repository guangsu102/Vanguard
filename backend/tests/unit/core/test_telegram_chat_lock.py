import asyncio
from types import SimpleNamespace

import pytest

from app.core.telegram_chat_lock import (
    acquire_telegram_chat_transaction_lock,
    telegram_account_advisory_lock,
    telegram_chat_advisory_lock,
)


@pytest.mark.asyncio
async def test_session_chat_lock_can_reenter_transaction_lock() -> None:
    db = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
    )
    async with telegram_chat_advisory_lock(db, -100123):
        await asyncio.wait_for(
            acquire_telegram_chat_transaction_lock(db, -100123),
            timeout=0.2,
        )


@pytest.mark.asyncio
async def test_account_lock_serializes_different_chat_sends() -> None:
    db = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
    )
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()

    async def first_chat_send() -> None:
        async with telegram_account_advisory_lock(db, 77):
            first_entered.set()
            await release_first.wait()

    async def second_chat_send() -> None:
        await first_entered.wait()
        async with telegram_account_advisory_lock(db, 77):
            second_entered.set()

    first = asyncio.create_task(first_chat_send())
    await first_entered.wait()
    second = asyncio.create_task(second_chat_send())
    await asyncio.sleep(0)

    assert not second_entered.is_set()
    release_first.set()
    await asyncio.gather(first, second)
    assert second_entered.is_set()
