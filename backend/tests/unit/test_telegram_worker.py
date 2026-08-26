import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.workers.telegram_worker as telegram_worker_module
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


def _private_event(*, text: str = "hello", media_only: bool = False) -> Any:
    sender = SimpleNamespace(
        username="customer",
        first_name="Ada",
        last_name="Lovelace",
    )
    photo = SimpleNamespace(id=987654321) if media_only else None
    message = SimpleNamespace(
        id=77,
        date=datetime(2026, 8, 26, 10, 0, 0),
        sender=sender,
        media=photo,
        photo=photo,
        sticker=None,
        voice=None,
        video=None,
        audio=None,
        document=None,
        contact=None,
        geo=None,
        file=(
            SimpleNamespace(name="photo.jpg", mime_type="image/jpeg", size=1024)
            if media_only
            else None
        ),
        reply_to_msg_id=None,
    )
    return SimpleNamespace(
        raw_text="" if media_only else text,
        text="" if media_only else text,
        sender_id=123456,
        chat_id=123456,
        id=77,
        message=message,
        sender=sender,
        is_private=True,
    )


@pytest.mark.asyncio
async def test_private_message_commits_and_pushes_when_auto_reply_is_off(
    monkeypatch,
):
    worker = TelegramWorker(TelegramWorkerRole.GROWTH_USER, worker_id="test-growth")
    db = object()
    conversation = object()
    private_message = object()
    order: list[str] = []

    @asynccontextmanager
    async def fake_db_session():
        yield db
        order.append("commit")

    persist = AsyncMock(return_value=(conversation, private_message, True))
    auto_reply_enabled = AsyncMock(return_value=False)

    async def fake_publish(event_type: str, _data: dict[str, Any]) -> None:
        order.append(event_type)

    handler_class = MagicMock()
    monkeypatch.setattr(telegram_worker_module, "get_db_session", fake_db_session)
    monkeypatch.setattr(
        telegram_worker_module, "persist_incoming_private_message", persist
    )
    monkeypatch.setattr(
        telegram_worker_module,
        "is_conversation_auto_reply_enabled",
        auto_reply_enabled,
    )
    monkeypatch.setattr(
        telegram_worker_module, "serialize_conversation", lambda _value: {"id": 1}
    )
    monkeypatch.setattr(
        telegram_worker_module, "serialize_private_message", lambda _value: {"id": 2}
    )
    monkeypatch.setattr(
        telegram_worker_module, "publish_private_chat_event", fake_publish
    )
    monkeypatch.setattr(
        telegram_worker_module, "AcquisitionEventHandler", handler_class
    )

    await worker._handle_growth_new_message(9, _private_event())

    incoming = persist.await_args.args[1]
    assert incoming.account_id == 9
    assert incoming.peer_telegram_id == 123456
    assert incoming.telegram_message_id == 77
    assert incoming.peer_username == "customer"
    assert incoming.peer_display_name == "Ada Lovelace"
    assert order == [
        "commit",
        "telegram:private-conversation",
        "telegram:private-message",
    ]
    auto_reply_enabled.assert_awaited_once_with(db, conversation)
    handler_class.assert_not_called()


@pytest.mark.asyncio
async def test_private_message_enters_existing_handler_when_auto_reply_is_on(
    monkeypatch,
):
    worker = TelegramWorker(TelegramWorkerRole.GROWTH_USER, worker_id="test-growth")
    db = object()

    @asynccontextmanager
    async def fake_db_session():
        yield db

    handler = MagicMock()
    handler.initialize = AsyncMock()
    handler.on_message = AsyncMock()
    monkeypatch.setattr(telegram_worker_module, "get_db_session", fake_db_session)
    monkeypatch.setattr(
        telegram_worker_module,
        "persist_incoming_private_message",
        AsyncMock(return_value=(object(), object(), True)),
    )
    monkeypatch.setattr(
        telegram_worker_module,
        "is_conversation_auto_reply_enabled",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        telegram_worker_module, "serialize_conversation", lambda _value: {"id": 1}
    )
    monkeypatch.setattr(
        telegram_worker_module, "serialize_private_message", lambda _value: {"id": 2}
    )
    monkeypatch.setattr(
        telegram_worker_module,
        "publish_private_chat_event",
        AsyncMock(),
    )
    handler_class = MagicMock(return_value=handler)
    monkeypatch.setattr(
        telegram_worker_module, "AcquisitionEventHandler", handler_class
    )

    await worker._handle_growth_new_message(9, _private_event())

    handler.initialize.assert_awaited_once()
    handler.on_message.assert_awaited_once()
    dispatched = handler.on_message.await_args.args[0]
    assert dispatched.content == "hello"
    assert dispatched.is_group is False


@pytest.mark.asyncio
async def test_private_media_message_is_persisted_without_auto_reply(
    monkeypatch,
):
    worker = TelegramWorker(TelegramWorkerRole.GROWTH_USER, worker_id="test-growth")

    @asynccontextmanager
    async def fake_db_session():
        yield object()

    persist = AsyncMock(return_value=(object(), object(), True))
    auto_reply_enabled = AsyncMock(return_value=True)
    handler_class = MagicMock()
    monkeypatch.setattr(telegram_worker_module, "get_db_session", fake_db_session)
    monkeypatch.setattr(
        telegram_worker_module, "persist_incoming_private_message", persist
    )
    monkeypatch.setattr(
        telegram_worker_module,
        "is_conversation_auto_reply_enabled",
        auto_reply_enabled,
    )
    monkeypatch.setattr(
        telegram_worker_module, "serialize_conversation", lambda _value: {"id": 1}
    )
    monkeypatch.setattr(
        telegram_worker_module, "serialize_private_message", lambda _value: {"id": 2}
    )
    monkeypatch.setattr(
        telegram_worker_module,
        "publish_private_chat_event",
        AsyncMock(),
    )
    monkeypatch.setattr(
        telegram_worker_module, "AcquisitionEventHandler", handler_class
    )

    await worker._handle_growth_new_message(9, _private_event(media_only=True))

    incoming = persist.await_args.args[1]
    assert incoming.content is None
    assert incoming.message_type == "photo"
    assert incoming.media == {
        "kind": "photo",
        "id": "987654321",
        "name": "photo.jpg",
        "mime_type": "image/jpeg",
        "size": 1024,
    }
    auto_reply_enabled.assert_not_awaited()
    handler_class.assert_not_called()


@pytest.mark.asyncio
async def test_duplicate_private_event_is_not_pushed_or_dispatched(monkeypatch):
    worker = TelegramWorker(TelegramWorkerRole.GROWTH_USER, worker_id="test-growth")

    @asynccontextmanager
    async def fake_db_session():
        yield object()

    publish = AsyncMock()
    auto_reply_enabled = AsyncMock(return_value=True)
    handler_class = MagicMock()
    monkeypatch.setattr(telegram_worker_module, "get_db_session", fake_db_session)
    monkeypatch.setattr(
        telegram_worker_module,
        "persist_incoming_private_message",
        AsyncMock(return_value=(object(), object(), False)),
    )
    monkeypatch.setattr(
        telegram_worker_module,
        "is_conversation_auto_reply_enabled",
        auto_reply_enabled,
    )
    monkeypatch.setattr(
        telegram_worker_module, "publish_private_chat_event", publish
    )
    monkeypatch.setattr(
        telegram_worker_module, "AcquisitionEventHandler", handler_class
    )

    await worker._handle_growth_new_message(9, _private_event())

    publish.assert_not_awaited()
    auto_reply_enabled.assert_not_awaited()
    handler_class.assert_not_called()


@pytest.mark.asyncio
async def test_private_outbox_uses_conversation_account_and_publishes_sent_status(
    monkeypatch,
):
    worker = TelegramWorker(TelegramWorkerRole.GROWTH_USER, worker_id="test-growth")
    claimed = SimpleNamespace(
        id=81,
        account_id=9,
        peer_telegram_id=123456,
        content="manual answer",
    )
    finalized = object()
    conversation = object()
    database_sessions = [object(), object(), object()]

    @asynccontextmanager
    async def fake_db_session():
        yield database_sessions.pop(0)

    claim = AsyncMock(return_value=claimed)
    finalize = AsyncMock(return_value=(conversation, finalized))
    publish = AsyncMock()
    account = SimpleNamespace(account_id=9)
    worker._account_pool.connect_by_id = AsyncMock(return_value=account)
    send_result = SimpleNamespace(id=444)
    execution = MagicMock()
    execution.send_private_message_result = AsyncMock(return_value=send_result)

    monkeypatch.setattr(telegram_worker_module, "get_db_session", fake_db_session)
    monkeypatch.setattr(
        telegram_worker_module, "claim_pending_outbound_message", claim
    )
    monkeypatch.setattr(
        telegram_worker_module, "finalize_outbound_private_message", finalize
    )
    monkeypatch.setattr(
        telegram_worker_module,
        "serialize_private_message",
        lambda value: (
            {"id": 81, "status": "sending"}
            if value is claimed
            else {"id": 81, "status": "sent"}
        ),
    )
    monkeypatch.setattr(
        telegram_worker_module,
        "serialize_conversation",
        lambda _value: {"id": 12},
    )
    monkeypatch.setattr(
        telegram_worker_module, "publish_private_chat_event", publish
    )
    monkeypatch.setattr(
        telegram_worker_module,
        "TelegramExecutionService",
        MagicMock(return_value=execution),
    )

    processed = await worker._process_private_outbox_once()

    assert processed is True
    worker._account_pool.connect_by_id.assert_awaited_once_with(
        9,
        purpose="private_chat_operator_reply",
        require_session=True,
        keep_connected=True,
    )
    execution.send_private_message_result.assert_awaited_once_with(
        account,
        123456,
        "manual answer",
        initiated_by_user=True,
        source="private_chat_operator",
    )
    assert finalize.await_args.kwargs["status"] == "sent"
    assert finalize.await_args.kwargs["telegram_message_id"] == 444
    assert [call.args[0] for call in publish.await_args_list] == [
        "telegram:private-message-status",
        "telegram:private-message-status",
        "telegram:private-conversation",
    ]
