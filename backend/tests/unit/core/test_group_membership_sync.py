from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.account.models import AccountStatus, AccountType, TelegramAccount, TelegramAPIConfig
from app.core.database import Base
from app.core.group.auto_rating import calculate_joined_group_initial_rating
from app.core.group.membership_sync import _upsert_synced_group_membership
from app.core.group.models import Group, GroupAccountMembership, GroupLevel
from app.modules.acquisition.search.group_finder import is_joinable_group_info, telegram_chat_to_dict


TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _create_promoter(db_session: AsyncSession) -> TelegramAccount:
    config = TelegramAPIConfig(name="default", api_id="12345", api_hash="hash")
    db_session.add(config)
    await db_session.flush()

    account = TelegramAccount(
        phone="+10000000000",
        identifier="+10000000000",
        account_type=AccountType.PROMOTER,
        api_config_name="default",
        api_config_id=config.id,
        session_name="session_10000000000",
        session_string="session",
        status=AccountStatus.ONLINE,
    )
    db_session.add(account)
    await db_session.commit()
    await db_session.refresh(account)
    return account


@pytest.mark.asyncio
async def test_upsert_synced_group_membership_creates_group_and_membership(db_session: AsyncSession):
    account = await _create_promoter(db_session)

    membership = await _upsert_synced_group_membership(
        db_session,
        account_id=account.id,
        group_id=123456,
        title="Already Joined",
        username="already_joined",
        member_count=88,
    )
    await db_session.commit()

    group = (await db_session.execute(select(Group).where(Group.group_id == 123456))).scalar_one()
    assert group.title == "Already Joined"
    assert group.username == "already_joined"
    assert group.discovery_source == "account_dialog_sync"
    assert group.status == "active"
    assert group.level != GroupLevel.UNRATED
    assert float(group.level_score) > 0
    assert group.rule_score > 0
    assert group.history_score > 0

    assert membership.group_id == group.id
    assert membership.account_id == account.id
    assert membership.status == "joined"
    assert membership.join_method == "account_dialog_sync"


@pytest.mark.asyncio
async def test_upsert_synced_group_membership_does_not_override_manual_rating(db_session: AsyncSession):
    account = await _create_promoter(db_session)
    group = Group(
        group_id=456789,
        title="Manual",
        username="manual",
        member_count=500,
        level=GroupLevel.A,
        level_score=88,
        rule_score=88,
        admin_score=88,
        history_score=88,
        convert_score=88,
        activity_score=88,
    )
    db_session.add(group)
    await db_session.commit()

    await _upsert_synced_group_membership(
        db_session,
        account_id=account.id,
        group_id=456789,
        title="Manual Updated",
        username="manual",
        member_count=1000,
    )
    await db_session.commit()

    refreshed = (await db_session.execute(select(Group).where(Group.group_id == 456789))).scalar_one()
    assert refreshed.level == GroupLevel.A
    assert float(refreshed.level_score) == 88
    assert refreshed.rule_score == 88


def test_joined_group_auto_rating_treats_zero_members_as_low_confidence():
    rating = calculate_joined_group_initial_rating(
        member_count=0,
        username="public_group",
    )

    assert rating.rule_score <= 45
    assert rating.admin_score <= 25
    assert rating.history_score <= 20
    assert rating.convert_score == 0
    assert rating.activity_score == 0


@pytest.mark.asyncio
async def test_upsert_synced_group_membership_refreshes_existing_left_membership(db_session: AsyncSession):
    account = await _create_promoter(db_session)
    first = await _upsert_synced_group_membership(
        db_session,
        account_id=account.id,
        group_id=777,
        title="Old",
        username=None,
        member_count=0,
    )
    first.status = "left"
    await db_session.commit()

    refreshed = await _upsert_synced_group_membership(
        db_session,
        account_id=account.id,
        group_id=777,
        title="New Title",
        username="new_title",
        member_count=10,
    )
    await db_session.commit()

    assert refreshed.id == first.id
    assert refreshed.status == "joined"
    assert refreshed.left_at is None
    group = (await db_session.execute(select(Group).where(Group.group_id == 777))).scalar_one()
    assert group.title == "New Title"
    assert group.username == "new_title"


def test_dialog_filter_accepts_groups_and_rejects_broadcast_channels():
    megagroup = SimpleNamespace(
        id=1,
        title="Useful Group",
        username="useful",
        megagroup=True,
        broadcast=False,
        participants_count=30,
    )
    channel = SimpleNamespace(
        id=2,
        title="News Channel",
        username="news",
        megagroup=False,
        broadcast=True,
        participants_count=500,
    )

    assert is_joinable_group_info(telegram_chat_to_dict(megagroup)) is True
    assert is_joinable_group_info(telegram_chat_to_dict(channel)) is False


@pytest.mark.asyncio
async def test_upsert_synced_group_membership_is_idempotent(db_session: AsyncSession):
    account = await _create_promoter(db_session)

    await _upsert_synced_group_membership(
        db_session,
        account_id=account.id,
        group_id=888,
        title="Same",
        username="same",
        member_count=1,
    )
    await _upsert_synced_group_membership(
        db_session,
        account_id=account.id,
        group_id=888,
        title="Same",
        username="same",
        member_count=1,
    )
    await db_session.commit()

    rows = (
        await db_session.execute(
            select(GroupAccountMembership).join(Group).where(Group.group_id == 888)
        )
    ).scalars().all()
    assert len(rows) == 1
