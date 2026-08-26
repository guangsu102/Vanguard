import asyncio
from typing import Any

import pytest

from app.core.worker_status import TelegramWorkerStatusValue
from app.modules.guardian.models import ManagedGroupBindingStatus, ManagedGroupBotRole
from app.workers.telegram_worker import TelegramWorker, TelegramWorkerRole


def test_growth_worker_status_degraded_without_accounts():
    worker = TelegramWorker(TelegramWorkerRole.GROWTH_USER, worker_id="test-growth")

    assert worker._status_for_snapshot({"enabled_accounts": 0}) == TelegramWorkerStatusValue.DEGRADED.value


def test_growth_worker_status_degraded_without_runtime_capable_accounts():
    worker = TelegramWorker(TelegramWorkerRole.GROWTH_USER, worker_id="test-growth")

    status = worker._status_for_snapshot(
        {"enabled_accounts": 1, "runtime": {"runtime_capable_accounts": 0}}
    )

    assert status == TelegramWorkerStatusValue.DEGRADED.value


def test_growth_worker_status_online_with_enabled_account():
    worker = TelegramWorker(TelegramWorkerRole.GROWTH_USER, worker_id="test-growth")

    assert worker._status_for_snapshot({"enabled_accounts": 1}) == TelegramWorkerStatusValue.ONLINE.value


def test_guardian_worker_status_degraded_when_all_bots_fail():
    worker = TelegramWorker(TelegramWorkerRole.GUARDIAN_BOT, worker_id="test-guardian")

    status = worker._status_for_snapshot({"enabled_bots": 2, "runtime": {"failed_bots": 2}})

    assert status == TelegramWorkerStatusValue.DEGRADED.value


def test_worker_event_flag_supports_property_and_method_shapes():
    property_event = type("PropertyEvent", (), {"user_joined": True})()
    method_event = type("MethodEvent", (), {"user_joined": lambda self: True})()

    assert TelegramWorker._event_flag(property_event, "user_joined") is True
    assert TelegramWorker._event_flag(method_event, "user_joined") is True


def test_guardian_worker_extracts_group_chats_from_updates():
    updates = [
        {"message": {"chat": {"id": -1001, "type": "supergroup", "title": "Ops"}}},
        {"edited_message": {"chat": {"id": 42, "type": "private", "title": "User"}}},
        {"my_chat_member": {"chat": {"id": -1002, "type": "group", "title": "Support"}}},
        {"channel_post": {"chat": {"id": -1001, "type": "supergroup", "title": "Ops renamed"}}},
    ]

    chats = TelegramWorker._guardian_chat_payloads_from_updates(updates)

    assert {chat["id"] for chat in chats} == {-1001, -1002}
    assert next(chat for chat in chats if chat["id"] == -1001)["title"] == "Ops renamed"


def test_guardian_worker_maps_member_status_to_binding_state():
    assert TelegramWorker._guardian_role_and_status({"status": "creator"}) == (
        ManagedGroupBotRole.OWNER,
        ManagedGroupBindingStatus.ACTIVE,
    )
    assert TelegramWorker._guardian_role_and_status({"status": "administrator"}) == (
        ManagedGroupBotRole.ADMIN,
        ManagedGroupBindingStatus.ACTIVE,
    )
    assert TelegramWorker._guardian_role_and_status({"status": "member"}) == (
        ManagedGroupBotRole.MEMBER,
        ManagedGroupBindingStatus.DEGRADED,
    )
    assert TelegramWorker._guardian_role_and_status({"status": "kicked"}) == (
        ManagedGroupBotRole.MEMBER,
        ManagedGroupBindingStatus.INACTIVE,
    )


@pytest.mark.asyncio
async def test_growth_event_dispatch_is_bounded():
    worker = TelegramWorker(TelegramWorkerRole.GROWTH_USER, worker_id="test-growth")
    active = 0
    peak = 0
    limit_reached = asyncio.Event()
    release = asyncio.Event()
    counter_lock = asyncio.Lock()

    async def handler(_account_id: int, _event: Any) -> None:
        nonlocal active, peak
        async with counter_lock:
            active += 1
            peak = max(peak, active)
            if active == worker._growth_event_concurrency:
                limit_reached.set()
        try:
            await release.wait()
        finally:
            async with counter_lock:
                active -= 1

    tasks = [
        asyncio.create_task(worker._run_growth_event(handler, 1, object()))
        for _ in range(worker._growth_event_concurrency + 3)
    ]

    await asyncio.wait_for(limit_reached.wait(), timeout=1)
    await asyncio.sleep(0)
    assert peak == worker._growth_event_concurrency

    release.set()
    await asyncio.gather(*tasks)
