from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from app.core.account.models import TelegramAccount
from app.core.security import get_current_user
from app.main import app
from app.modules.private_chat.service import (
    IncomingPrivateMessage,
    persist_incoming_private_message,
)


async def _seed_private_chats(test_db):
    account = TelegramAccount(
        identifier="private-api",
        display_name="Private API",
        session_name="private-api",
    )
    test_db.add(account)
    await test_db.commit()
    await test_db.refresh(account)

    first_conversation = None
    for message_id, content in ((10, "first"), (11, "second")):
        first_conversation, _, _ = await persist_incoming_private_message(
            test_db,
            IncomingPrivateMessage(
                account_id=account.id,
                peer_telegram_id=10001,
                telegram_message_id=message_id,
                content=content,
                occurred_at=datetime(2026, 8, 26, 10, message_id - 10, 0),
                peer_username="alice",
                peer_display_name="Alice",
            ),
        )
    second_conversation, _, _ = await persist_incoming_private_message(
        test_db,
        IncomingPrivateMessage(
            account_id=account.id,
            peer_telegram_id=10002,
            telegram_message_id=20,
            content="other",
            occurred_at=datetime(2026, 8, 26, 11, 0, 0),
            peer_username="bob",
            peer_display_name="Bob",
        ),
    )
    await test_db.commit()
    return first_conversation, second_conversation


@pytest.mark.asyncio
async def test_private_chat_queries_and_summary(client, test_db):
    first_conversation, _ = await _seed_private_chats(test_db)
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 99,
        "username": "admin",
        "role": "admin",
    }

    list_response = await client.get(
        "/api/private-chats/conversations",
        params={"keyword": "Alice", "unread_only": True},
    )
    assert list_response.status_code == 200
    list_body = list_response.json()
    assert list_body["total"] == 1
    assert list_body["data"][0]["peer_username"] == "alice"
    assert list_body["data"][0]["unread_count"] == 2

    messages_response = await client.get(
        f"/api/private-chats/conversations/{first_conversation.id}/messages",
        params={"limit": 1},
    )
    assert messages_response.status_code == 200
    messages_body = messages_response.json()
    assert messages_body["total"] == 2
    assert [item["content"] for item in messages_body["data"]] == ["second"]

    summary_response = await client.get("/api/private-chats/summary")
    assert summary_response.status_code == 200
    assert summary_response.json()["data"] == {
        "conversation_count": 2,
        "unread_count": 3,
        "open_count": 2,
    }


@pytest.mark.asyncio
async def test_private_chat_read_takeover_and_close(
    client,
    test_db,
    monkeypatch,
):
    first_conversation, _ = await _seed_private_chats(test_db)
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 99,
        "username": "admin",
        "role": "admin",
    }
    private_chats_api = __import__(
        "app.api.private_chats", fromlist=["publish_private_chat_event"]
    )
    publish = AsyncMock()
    monkeypatch.setattr(private_chats_api, "publish_private_chat_event", publish)

    read_response = await client.post(
        f"/api/private-chats/conversations/{first_conversation.id}/read"
    )
    assert read_response.status_code == 200
    assert read_response.json()["data"]["unread_count"] == 0

    takeover_response = await client.patch(
        f"/api/private-chats/conversations/{first_conversation.id}",
        json={"handling_mode": "human"},
    )
    assert takeover_response.status_code == 200
    takeover = takeover_response.json()["data"]
    assert takeover["handling_mode"] == "human"
    assert takeover["assigned_admin_id"] == 99

    close_response = await client.patch(
        f"/api/private-chats/conversations/{first_conversation.id}",
        json={"status": "closed"},
    )
    assert close_response.status_code == 200
    assert close_response.json()["data"]["status"] == "closed"
    assert publish.await_count == 3


@pytest.mark.asyncio
async def test_private_chat_manual_reply_is_queued_idempotently(
    client,
    test_db,
    monkeypatch,
):
    first_conversation, _ = await _seed_private_chats(test_db)
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 99,
        "username": "admin",
        "role": "admin",
    }
    private_chats_api = __import__(
        "app.api.private_chats", fromlist=["publish_private_chat_event"]
    )
    publish = AsyncMock()
    monkeypatch.setattr(private_chats_api, "publish_private_chat_event", publish)

    payload = {
        "content": " Manual answer ",
        "client_request_id": "manual-request-1",
    }
    first_response = await client.post(
        f"/api/private-chats/conversations/{first_conversation.id}/messages",
        json=payload,
    )
    duplicate_response = await client.post(
        f"/api/private-chats/conversations/{first_conversation.id}/messages",
        json=payload,
    )

    assert first_response.status_code == 202
    assert duplicate_response.status_code == 202
    first_message = first_response.json()["data"]
    assert duplicate_response.json()["data"]["id"] == first_message["id"]
    assert first_message["content"] == "Manual answer"
    assert first_message["status"] == "pending"
    assert first_message["source"] == "operator"
    assert first_message["operator_id"] == 99
    assert publish.await_count == 2
