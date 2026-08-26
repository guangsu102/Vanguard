from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

import app.modules.private_chat.service as private_chat_service
from app.core.account.models import TelegramAccount
from app.modules.private_chat.models import PrivateChatConversation, PrivateChatMessage
from app.modules.private_chat.service import (
    IncomingPrivateMessage,
    claim_pending_outbound_message,
    finalize_outbound_private_message,
    is_conversation_auto_reply_enabled,
    persist_incoming_private_message,
    queue_outbound_private_message,
)
from scripts.apply_sql_migrations import DEFAULT_MIGRATIONS, _split_sql_statements


async def _account(test_db, suffix: str) -> TelegramAccount:
    account = TelegramAccount(
        identifier=f"inbox-{suffix}",
        display_name=f"Inbox {suffix}",
        session_name=f"inbox-{suffix}",
    )
    test_db.add(account)
    await test_db.commit()
    await test_db.refresh(account)
    return account


@pytest.mark.asyncio
async def test_persist_incoming_message_is_idempotent(test_db):
    account = await _account(test_db, "one")
    incoming = IncomingPrivateMessage(
        account_id=account.id,
        peer_telegram_id=998877,
        telegram_message_id=42,
        content="hello",
        occurred_at=datetime(2026, 8, 26, 8, 0, 0),
        peer_username="customer",
        peer_display_name="Customer",
    )

    conversation, message, created = await persist_incoming_private_message(
        test_db, incoming
    )
    duplicate_conversation, duplicate_message, duplicate_created = (
        await persist_incoming_private_message(test_db, incoming)
    )
    await test_db.commit()

    assert created is True
    assert duplicate_created is False
    assert duplicate_conversation.id == conversation.id
    assert duplicate_message.id == message.id
    assert conversation.unread_count == 1
    assert conversation.last_message_preview == "hello"
    assert (
        await test_db.execute(select(func.count(PrivateChatMessage.id)))
    ).scalar_one() == 1


@pytest.mark.asyncio
async def test_same_peer_on_two_accounts_creates_two_conversations(test_db):
    first_account = await _account(test_db, "first")
    second_account = await _account(test_db, "second")

    for account, message_id in ((first_account, 1), (second_account, 2)):
        await persist_incoming_private_message(
            test_db,
            IncomingPrivateMessage(
                account_id=account.id,
                peer_telegram_id=123456,
                telegram_message_id=message_id,
                content=f"message for {account.identifier}",
                occurred_at=datetime(2026, 8, 26, 9, message_id, 0),
            ),
        )
    await test_db.commit()

    conversations = (
        await test_db.execute(
            select(PrivateChatConversation).order_by(
                PrivateChatConversation.account_id
            )
        )
    ).scalars().all()

    assert len(conversations) == 2
    assert {item.account_id for item in conversations} == {
        first_account.id,
        second_account.id,
    }


@pytest.mark.asyncio
async def test_media_only_message_gets_visible_preview(test_db):
    account = await _account(test_db, "media")

    conversation, message, created = await persist_incoming_private_message(
        test_db,
        IncomingPrivateMessage(
            account_id=account.id,
            peer_telegram_id=24680,
            telegram_message_id=3,
            content=None,
            occurred_at=datetime(2026, 8, 26, 10, 0, 0),
            message_type="photo",
            media={"name": "photo.jpg", "size": 1024},
        ),
    )

    assert created is True
    assert conversation.last_message_preview == "[photo]"
    assert message.media_json is not None

@pytest.mark.asyncio
async def test_auto_reply_requires_global_switch_and_auto_open_conversation(
    monkeypatch,
):
    global_setting = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr(
        private_chat_service, "is_private_messaging_enabled", global_setting
    )
    conversation = PrivateChatConversation(
        account_id=1,
        peer_telegram_id=100,
        status="open",
        handling_mode="auto",
    )

    assert (
        await is_conversation_auto_reply_enabled(object(), conversation)
        is False
    )
    assert (
        await is_conversation_auto_reply_enabled(object(), conversation)
        is True
    )

    conversation.handling_mode = "human"
    assert (
        await is_conversation_auto_reply_enabled(object(), conversation)
        is False
    )
    conversation.handling_mode = "auto"
    conversation.status = "closed"
    assert (
        await is_conversation_auto_reply_enabled(object(), conversation)
        is False
    )
    assert global_setting.await_count == 2


@pytest.mark.asyncio
async def test_new_inbound_message_reopens_closed_conversation(test_db):
    account = await _account(test_db, "reopen")
    first_incoming = IncomingPrivateMessage(
        account_id=account.id,
        peer_telegram_id=13579,
        telegram_message_id=1,
        content="first",
        occurred_at=datetime(2026, 8, 26, 11, 0, 0),
    )
    conversation, _, _ = await persist_incoming_private_message(
        test_db, first_incoming
    )
    conversation.status = "closed"
    await test_db.commit()

    reopened, _, created = await persist_incoming_private_message(
        test_db,
        IncomingPrivateMessage(
            account_id=account.id,
            peer_telegram_id=13579,
            telegram_message_id=2,
            content="new request",
            occurred_at=datetime(2026, 8, 26, 11, 1, 0),
        ),
    )

    assert created is True
    assert reopened.id == conversation.id
    assert reopened.status == "open"
    assert reopened.unread_count == 2


@pytest.mark.asyncio
async def test_manual_reply_outbox_is_idempotent_and_tracks_delivery(test_db):
    account = await _account(test_db, "outbox")
    conversation, _, _ = await persist_incoming_private_message(
        test_db,
        IncomingPrivateMessage(
            account_id=account.id,
            peer_telegram_id=424242,
            telegram_message_id=1,
            content="question",
            occurred_at=datetime(2026, 8, 26, 12, 0, 0),
        ),
    )

    conversation, outbound, created = await queue_outbound_private_message(
        test_db,
        conversation,
        content="answer",
        operator_id=7,
        client_request_id="request-123",
    )
    _, duplicate, duplicate_created = await queue_outbound_private_message(
        test_db,
        conversation,
        content="answer",
        operator_id=7,
        client_request_id="request-123",
    )
    await test_db.commit()

    assert created is True
    assert duplicate_created is False
    assert duplicate.id == outbound.id
    assert conversation.handling_mode == "human"
    assert conversation.assigned_admin_id == 7
    assert conversation.last_message_preview == "answer"

    claimed = await claim_pending_outbound_message(test_db)
    assert claimed is not None
    assert claimed.id == outbound.id
    assert claimed.status == "sending"
    assert claimed.attempt_count == 1

    finalized_conversation, finalized = await finalize_outbound_private_message(
        test_db,
        outbound.id,
        status="sent",
        telegram_message_id=987,
    )
    assert finalized.status == "sent"
    assert finalized.telegram_message_id == 987
    assert finalized.sent_at is not None
    assert finalized_conversation.last_outbound_at is not None


def test_private_chat_sql_migration_is_registered_and_parseable():
    migration_name = "035_add_telegram_private_inbox.sql"
    migrations_dir = Path(__file__).parents[2] / "migrations"
    migration_path = migrations_dir / migration_name

    assert migration_name in DEFAULT_MIGRATIONS
    assert migration_path.is_file()
    statements = _split_sql_statements(migration_path.read_text(encoding="utf-8"))
    assert any(
        "ALTER COLUMN version_num TYPE VARCHAR(128)" in statement
        for statement in statements
    )
    assert any(
        "CREATE TABLE IF NOT EXISTS telegram_private_conversation" in statement
        for statement in statements
    )
    assert any(
        "CREATE TABLE IF NOT EXISTS telegram_private_message" in statement
        for statement in statements
    )
