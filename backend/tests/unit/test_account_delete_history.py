"""Deleting accounts with qualification history must enforce real foreign keys."""

from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.account.manager import AccountManager
from app.core.account.models import TelegramAccount
from app.core.database import Base
from app.core.group.models import Group, GroupAccountMembership
from app.modules.acquisition.models import GroupQualificationAudit
from tests.conftest import _import_models


@pytest_asyncio.fixture
async def deletion_db():
    _import_models()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def enforce_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


async def seed_history(db):
    db.add_all(
        [
            TelegramAccount(id=1, identifier="delete-me", session_name="delete-me"),
            TelegramAccount(id=2, identifier="keep-me", session_name="keep-me"),
            Group(id=1, group_id=-100123456),
        ]
    )
    await db.flush()
    db.add_all(
        [
            GroupAccountMembership(id=1, account_id=1, group_id=1, telegram_group_id=-100123456),
            GroupAccountMembership(id=2, account_id=2, group_id=1, telegram_group_id=-100123456),
        ]
    )
    await db.flush()
    db.add_all(
        [
            GroupQualificationAudit(
                id=account_id,
                batch_id="review",
                membership_id=account_id,
                account_id=account_id,
                group_id=1,
                policy_version="v1",
                state="completed",
            )
            for account_id in (1, 2)
        ]
    )
    await db.commit()
    db.expunge_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("delete_membership", [False, True], ids=["account", "membership"])
async def test_delete_with_qualification_history(deletion_db, delete_membership):
    db = deletion_db
    await seed_history(db)
    if delete_membership:
        membership = await db.get(GroupAccountMembership, 1)
        await db.delete(membership)
        await db.commit()
    else:
        with patch("app.core.account.manager.get_settings"):
            assert await AccountManager(db).delete_account(1) is True
        assert await db.get(TelegramAccount, 1) is None

    assert await db.get(GroupQualificationAudit, 1) is None
    assert await db.get(GroupAccountMembership, 1) is None
    assert await db.get(TelegramAccount, 2) is not None
    assert await db.get(GroupAccountMembership, 2) is not None
    assert await db.get(GroupQualificationAudit, 2) is not None
    assert await db.scalar(select(func.count()).select_from(Group)) == 1
