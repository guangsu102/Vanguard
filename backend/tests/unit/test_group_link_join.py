import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.api.groups import GroupJoinByLinkRequest
from app.core.account.models import AccountStatus, AccountType, TelegramAccount
from app.core.account.telegram_execution import (
    TelegramExecutionError,
    TelegramExecutionService,
    TelegramJoinRequestPendingError,
    parse_telegram_group_link,
)
from app.core.group.models import Group, GroupAccountMembership, GroupLevel

groups_api = importlib.import_module("app.api.groups")


@pytest.mark.parametrize(
    ("value", "kind", "target"),
    [
        ("https://t.me/public_group", "public", "public_group"),
        ("telegram.me/public_group", "public", "public_group"),
        ("https://t.me/+AbCdEfGh123", "private", "AbCdEfGh123"),
        ("https://t.me/joinchat/AbCdEfGh123", "private", "AbCdEfGh123"),
        ("tg://join?invite=AbCdEfGh123", "private", "AbCdEfGh123"),
    ],
)
def test_parse_telegram_group_link(value, kind, target):
    parsed = parse_telegram_group_link(value)

    assert parsed.kind == kind
    assert parsed.target == target


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com/public_group",
        "https://t.me/c/123/456",
        "https://t.me/share/url?url=https://example.com",
        "not a telegram link",
    ],
)
def test_parse_telegram_group_link_rejects_unrelated_links(value):
    with pytest.raises(TelegramExecutionError):
        parse_telegram_group_link(value)


@pytest.mark.asyncio
async def test_join_public_group_by_link_uses_resolved_group():
    entity = SimpleNamespace(
        id=1001,
        title="Public Group",
        username="public_group",
        broadcast=False,
        megagroup=True,
        type="supergroup",
    )

    class PublicClient:
        def __init__(self):
            self.requests = []

        async def get_entity(self, target):
            assert target == "public_group"
            return entity

        async def __call__(self, request):
            self.requests.append(request)

    client = PublicClient()
    result = await TelegramExecutionService().join_group_by_link(
        SimpleNamespace(client=client),
        "https://t.me/public_group",
    )

    assert result["id"] == 1001
    assert result["title"] == "Public Group"
    assert client.requests[0].__class__.__name__ == "JoinChannelRequest"


@pytest.mark.asyncio
async def test_join_private_group_returns_existing_membership_without_importing():
    entity = SimpleNamespace(
        id=2002,
        title="Private Group",
        username=None,
        broadcast=False,
        megagroup=True,
        type="supergroup",
    )

    class PrivateClient:
        def __init__(self):
            self.requests = []

        async def __call__(self, request):
            self.requests.append(request)
            return SimpleNamespace(chat=entity)

    client = PrivateClient()
    result = await TelegramExecutionService().join_group_by_link(
        SimpleNamespace(client=client),
        "https://t.me/+AbCdEfGh123",
    )

    assert result["title"] == "Private Group"
    assert [request.__class__.__name__ for request in client.requests] == [
        "CheckChatInviteRequest"
    ]


@pytest.mark.asyncio
async def test_join_private_group_imports_invite_and_returns_joined_chat():
    entity = SimpleNamespace(
        id=2003,
        title="New Private Group",
        username=None,
        broadcast=False,
        megagroup=True,
        type="supergroup",
    )

    class PrivateClient:
        def __init__(self):
            self.requests = []

        async def __call__(self, request):
            self.requests.append(request)
            if request.__class__.__name__ == "CheckChatInviteRequest":
                return SimpleNamespace(broadcast=False, megagroup=True)
            return SimpleNamespace(chats=[entity])

    client = PrivateClient()
    result = await TelegramExecutionService().join_group_by_link(
        SimpleNamespace(client=client),
        "https://t.me/joinchat/AbCdEfGh123",
    )

    assert result["title"] == "New Private Group"
    assert [request.__class__.__name__ for request in client.requests] == [
        "CheckChatInviteRequest",
        "ImportChatInviteRequest",
    ]


@pytest.mark.asyncio
async def test_join_private_group_reports_pending_approval():
    class InviteRequestSentError(RuntimeError):
        pass

    class PendingClient:
        async def __call__(self, request):
            if request.__class__.__name__ == "CheckChatInviteRequest":
                return SimpleNamespace(broadcast=False, megagroup=True)
            raise InviteRequestSentError("request sent")

    with pytest.raises(TelegramJoinRequestPendingError, match="awaiting group approval"):
        await TelegramExecutionService().join_group_by_link(
            SimpleNamespace(client=PendingClient()),
            "https://t.me/+AbCdEfGh123",
        )


@pytest.mark.asyncio
async def test_join_group_by_link_rejects_broadcast_channel():
    entity = SimpleNamespace(
        id=3003,
        title="Broadcast",
        username="broadcast_news",
        broadcast=True,
        megagroup=False,
        type="channel",
    )
    client = SimpleNamespace(get_entity=AsyncMock(return_value=entity))

    with pytest.raises(TelegramExecutionError, match="broadcast channel"):
        await TelegramExecutionService().join_group_by_link(
            SimpleNamespace(client=client),
            "https://t.me/broadcast_news",
        )


@pytest.mark.asyncio
async def test_join_group_api_persists_resolved_group_and_membership(test_db, monkeypatch):
    account = TelegramAccount(
        phone="+15550009901",
        identifier="+15550009901",
        session_name="manual_link_join",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    test_db.add(account)
    await test_db.commit()
    await test_db.refresh(account)

    wrapper = SimpleNamespace(account_id=account.id)
    pool = SimpleNamespace(
        add_account_from_db=AsyncMock(return_value=wrapper),
        acquire_by_id=AsyncMock(return_value=wrapper),
        release=AsyncMock(),
    )

    class FakeExecutionService:
        def __init__(self, _risk_guard):
            pass

        async def join_group_by_link(self, acquired_wrapper, group_link):
            assert acquired_wrapper is wrapper
            assert group_link == "https://t.me/real_group"
            return {
                "id": -100987654321,
                "raw_id": 987654321,
                "title": "Resolved Group",
                "username": "real_group",
                "participants_count": 321,
            }

    monkeypatch.setattr(groups_api, "get_account_pool", lambda: pool)
    monkeypatch.setattr(groups_api, "TelegramExecutionService", FakeExecutionService)

    response = await groups_api.join_group_by_link(
        GroupJoinByLinkRequest(
            account_id=account.id,
            group_link="https://t.me/real_group",
        ),
        db=test_db,
        _current_user={"id": 1, "role": "admin"},
    )

    assert response.group_id == -100987654321
    assert response.title == "Resolved Group"
    assert response.level == GroupLevel.A.value
    assert response.account_count == 1
    membership = (
        await test_db.execute(
            select(GroupAccountMembership).where(
                GroupAccountMembership.group_id == response.id,
                GroupAccountMembership.account_id == account.id,
            )
        )
    ).scalar_one()
    assert membership.telegram_group_id == -100987654321
    assert membership.status == "joined"
    assert membership.join_method == "manual_link_join"
    pool.release.assert_awaited_once_with(wrapper)

    group_count = await test_db.scalar(select(func.count(Group.id)))
    membership_count = await test_db.scalar(select(func.count(GroupAccountMembership.id)))
    assert group_count == 1
    assert membership_count == 1


@pytest.mark.asyncio
async def test_join_group_api_does_not_persist_pending_request(test_db, monkeypatch):
    account = TelegramAccount(
        phone="+15550009902",
        identifier="+15550009902",
        session_name="pending_link_join",
        account_type=AccountType.PROMOTER,
        status=AccountStatus.ONLINE,
        is_active=True,
    )
    test_db.add(account)
    await test_db.commit()
    await test_db.refresh(account)

    wrapper = SimpleNamespace(account_id=account.id)
    pool = SimpleNamespace(
        add_account_from_db=AsyncMock(return_value=wrapper),
        acquire_by_id=AsyncMock(return_value=wrapper),
        release=AsyncMock(),
    )

    class PendingExecutionService:
        def __init__(self, _risk_guard):
            pass

        async def join_group_by_link(self, _wrapper, _group_link):
            raise TelegramJoinRequestPendingError(
                "Telegram join request is awaiting group approval"
            )

    monkeypatch.setattr(groups_api, "get_account_pool", lambda: pool)
    monkeypatch.setattr(groups_api, "TelegramExecutionService", PendingExecutionService)

    with pytest.raises(HTTPException) as exc_info:
        await groups_api.join_group_by_link(
            GroupJoinByLinkRequest(
                account_id=account.id,
                group_link="https://t.me/+AbCdEfGh123",
            ),
            db=test_db,
            _current_user={"id": 1, "role": "admin"},
        )

    assert exc_info.value.status_code == 409
    assert await test_db.scalar(select(func.count(Group.id))) == 0
    assert await test_db.scalar(select(func.count(GroupAccountMembership.id))) == 0
    pool.release.assert_awaited_once_with(wrapper)
