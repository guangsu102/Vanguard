"""PostgreSQL CAS proof for concurrent Guardian observation updates.

Set ``VANGUARD_TEST_POSTGRES_URL`` to a disposable PostgreSQL database URL to
run this P0 integration gate.  The test creates and drops an isolated schema.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.account.models import TelegramAccount
from app.core.campaign.models import Campaign  # noqa: F401
from app.core.group.models import Group  # noqa: F401
from app.core.user.models import User  # noqa: F401
from app.modules.guardian.models import ManagedGroupBinding  # noqa: F401
from app.modules.owned_group.member_observation import (
    MemberObservationFact,
    upsert_member_observation,
)
from app.modules.owned_group.models import OwnedGroupAsset
from app.modules.owned_group.models_extra import OwnedGroupMemberObservation

POSTGRES_URL = os.getenv("VANGUARD_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="VANGUARD_TEST_POSTGRES_URL is required for the PostgreSQL CAS gate",
)


def _async_url(raw_url: str) -> str:
    url = make_url(raw_url)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+asyncpg")
    return url.render_as_string(hide_password=False)


def _fact(update_id: int) -> MemberObservationFact:
    return MemberObservationFact(
        group_asset_id=1,
        telegram_user_id=7001,
        is_bot=False,
        username_snapshot=None,
        display_name_snapshot=None,
        presence_status="present" if update_id == 102 else "left",
        event_type="message" if update_id == 102 else "left_chat_member",
        source_bot_account_id=9,
        update_id=update_id,
        event_time=datetime(2026, 9, 11, 8, 0, update_id - 100, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_concurrent_same_source_updates_keep_only_highest_update_id() -> None:
    assert POSTGRES_URL is not None
    assert TelegramAccount.__tablename__ == "telegram_account"
    assert OwnedGroupAsset.__tablename__ == "owned_group_assets"
    schema = f"stage5_observation_{uuid.uuid4().hex}"
    engine = create_async_engine(_async_url(POSTGRES_URL), pool_size=2, max_overflow=0)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    quoted_schema = f'"{schema}"'

    try:
        async with engine.begin() as connection:
            await connection.execute(text(f"CREATE SCHEMA {quoted_schema}"))
            await connection.execute(text(f"SET LOCAL search_path TO {quoted_schema}"))
            await connection.execute(
                text("CREATE TABLE owned_group_assets (id INTEGER PRIMARY KEY)")
            )
            await connection.execute(
                text("CREATE TABLE telegram_account (id INTEGER PRIMARY KEY)")
            )
            await connection.run_sync(OwnedGroupMemberObservation.__table__.create)
            await connection.execute(text("INSERT INTO owned_group_assets (id) VALUES (1)"))
            await connection.execute(text("INSERT INTO telegram_account (id) VALUES (9)"))

        async def write(update_id: int) -> str:
            async with session_factory.begin() as db:
                await db.execute(text(f"SET LOCAL search_path TO {quoted_schema}"))
                result = await upsert_member_observation(db, _fact(update_id))
                return result.status

        statuses = await asyncio.gather(write(101), write(102))
        assert set(statuses) <= {"inserted", "updated", "ignored_stale"}

        async with session_factory() as db:
            await db.execute(text(f"SET search_path TO {quoted_schema}"))
            row = await db.scalar(select(OwnedGroupMemberObservation))
            assert row is not None
            assert row.last_update_id == 102
            assert row.presence_status == "present"
            assert row.last_event_type == "message"
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text(f"DROP SCHEMA IF EXISTS {quoted_schema} CASCADE")
            )
        await engine.dispose()
