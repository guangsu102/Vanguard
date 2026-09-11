from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

import app.modules.owned_group.member_observation as observation_module
from app.modules.owned_group.member_observation import (
    MemberObservationFact,
    extract_member_observation_facts,
    project_owned_group_member_observations,
    upsert_member_observation,
)
from app.modules.owned_group.models_extra import OwnedGroupMemberObservation


def _fact(
    *,
    update_id: int,
    event_type: str = "message",
    source_bot_account_id: int = 9,
    is_bot: bool = False,
) -> MemberObservationFact:
    return MemberObservationFact(
        group_asset_id=1,
        telegram_user_id=7001,
        is_bot=is_bot,
        username_snapshot="observed_user",
        display_name_snapshot="Observed User",
        presence_status="left" if event_type == "left_chat_member" else "present",
        event_type=event_type,  # type: ignore[arg-type]
        source_bot_account_id=source_bot_account_id,
        update_id=update_id,
        event_time=datetime(2026, 9, 11, 8, update_id % 60, tzinfo=UTC),
    )


def test_extracts_multiple_join_facts_and_rejects_guessed_is_bot() -> None:
    extraction = extract_member_observation_facts(
        {
            "date": 1_789_117_200,
            "chat": {"id": -1001},
            "from": {"id": 99, "is_bot": False},
            "new_chat_members": [
                {
                    "id": 7001,
                    "is_bot": False,
                    "username": "@member_one",
                    "first_name": "Member",
                    "last_name": "One",
                },
                {"id": 7002, "is_bot": True, "first_name": "Helper"},
                {"id": 7003, "is_bot": 0, "first_name": "Not Boolean"},
            ],
        },
        group_asset_id=5,
        source_bot_account_id=9,
        update_id=101,
        update_kind="message",
    )

    assert [fact.telegram_user_id for fact in extraction.facts] == [7001, 7002]
    assert {fact.event_type for fact in extraction.facts} == {"new_chat_member"}
    assert extraction.facts[0].username_snapshot == "member_one"
    assert extraction.facts[0].display_name_snapshot == "Member One"
    assert extraction.facts[0].event_time.tzinfo is UTC
    assert extraction.skipped_reasons == ("identity_type_invalid",)


@pytest.mark.parametrize("media_field", ["photo", "location", "document"])
def test_media_only_message_is_a_present_observation(media_field: str) -> None:
    extraction = extract_member_observation_facts(
        {
            "date": 1_789_117_200,
            "chat": {"id": -1001},
            "from": {"id": 7001, "is_bot": False},
            media_field: {"id": "public-media-shape"},
        },
        group_asset_id=5,
        source_bot_account_id=9,
        update_id=102,
        update_kind="message",
    )

    assert len(extraction.facts) == 1
    assert extraction.facts[0].event_type == "message"
    assert extraction.facts[0].presence_status == "present"


def test_edited_message_never_projects_even_with_service_fields() -> None:
    extraction = extract_member_observation_facts(
        {
            "date": 1_789_117_200,
            "from": {"id": 7001, "is_bot": False},
            "new_chat_members": [{"id": 7002, "is_bot": False}],
            "text": "edited body must not establish presence",
        },
        group_asset_id=5,
        source_bot_account_id=9,
        update_id=103,
        update_kind="edited_message",
    )

    assert extraction.facts == ()
    assert extraction.skipped_reasons == ("edited_message",)


@pytest.mark.parametrize(
    ("overrides", "expected_reason"),
    [
        ({"update_id": -1}, "update_id_invalid"),
        ({"update_id": True}, "update_id_invalid"),
        ({"date": "not-a-timestamp"}, "event_time_invalid"),
        ({"date": 10**30}, "event_time_invalid"),
    ],
)
def test_invalid_outer_update_fields_skip_projection(
    overrides: dict[str, object], expected_reason: str
) -> None:
    message: dict[str, object] = {
        "date": 1_789_117_200,
        "from": {"id": 7001, "is_bot": False},
        "text": "message",
    }
    update_id: object = 104
    if "date" in overrides:
        message["date"] = overrides["date"]
    if "update_id" in overrides:
        update_id = overrides["update_id"]

    extraction = extract_member_observation_facts(
        message,
        group_asset_id=5,
        source_bot_account_id=9,
        update_id=update_id,
        update_kind="message",
    )

    assert extraction.facts == ()
    assert extraction.skipped_reasons == (expected_reason,)


def test_anonymous_sender_does_not_create_a_pseudo_user() -> None:
    extraction = extract_member_observation_facts(
        {
            "date": 1_789_117_200,
            "sender_chat": {"id": -1001},
            "text": "anonymous administrator",
        },
        group_asset_id=5,
        source_bot_account_id=9,
        update_id=105,
        update_kind="message",
    )

    assert extraction.facts == ()
    assert extraction.skipped_reasons == ("anonymous_sender",)


@pytest.mark.asyncio
async def test_atomic_upsert_rejects_replay_mismatch_and_identity_flip(test_db) -> None:
    inserted = await upsert_member_observation(
        test_db, _fact(update_id=101, event_type="new_chat_member")
    )
    stale = await upsert_member_observation(
        test_db, _fact(update_id=101, event_type="left_chat_member")
    )
    source_mismatch = await upsert_member_observation(
        test_db,
        _fact(
            update_id=200,
            event_type="left_chat_member",
            source_bot_account_id=10,
        ),
    )
    identity_conflict = await upsert_member_observation(
        test_db,
        _fact(update_id=201, event_type="left_chat_member", is_bot=True),
    )

    assert inserted.status == "inserted"
    assert stale.status == "ignored_stale"
    assert source_mismatch.status == "source_bot_mismatch"
    assert identity_conflict.status == "identity_type_conflict"

    row = await test_db.scalar(select(OwnedGroupMemberObservation))
    assert row is not None
    assert row.presence_status == "present"
    assert row.last_update_id == 101
    assert row.source_bot_account_id == 9
    assert row.is_bot is False


@pytest.mark.asyncio
async def test_later_leave_and_message_update_all_presence_fields_atomically(test_db) -> None:
    await upsert_member_observation(
        test_db, _fact(update_id=101, event_type="new_chat_member")
    )
    left = await upsert_member_observation(
        test_db, _fact(update_id=102, event_type="left_chat_member")
    )
    row = await test_db.scalar(select(OwnedGroupMemberObservation))
    assert row is not None
    joined_at = row.joined_at
    assert left.status == "updated"
    assert row.presence_status == "left"
    assert row.left_at is not None

    returned = await upsert_member_observation(test_db, _fact(update_id=103))
    await test_db.refresh(row)
    assert returned.status == "updated"
    assert row.presence_status == "present"
    assert row.left_at is None
    assert row.joined_at == joined_at
    assert row.last_event_type == "message"
    assert row.last_update_id == 103


@pytest.mark.asyncio
async def test_later_join_after_leave_refreshes_joined_at_and_clears_left_at(test_db) -> None:
    await upsert_member_observation(
        test_db, _fact(update_id=101, event_type="left_chat_member")
    )
    rejoined = await upsert_member_observation(
        test_db, _fact(update_id=102, event_type="new_chat_member")
    )

    row = await test_db.scalar(select(OwnedGroupMemberObservation))
    assert row is not None
    assert rejoined.status == "updated"
    assert row.presence_status == "present"
    assert row.joined_at is not None
    assert row.joined_at.replace(tzinfo=UTC) == _fact(
        update_id=102, event_type="new_chat_member"
    ).event_time
    assert row.left_at is None
    assert row.last_update_id == 102


def test_cas_statement_contains_all_atomic_guards() -> None:
    statement = observation_module._conditional_update_statement(
        _fact(update_id=128)
    )
    sql = " ".join(
        str(
            statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        ).split()
    )

    assert "source_bot_account_id = 9" in sql
    assert "is_bot = false" in sql
    assert "last_update_id < 128" in sql
    assert "presence_status='present'" in sql.replace(" ", "")
    assert "last_observed_at=" in sql.replace(" ", "")


@pytest.mark.asyncio
async def test_projector_uses_a_separate_committed_session(
    test_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    commits: list[str] = []

    @asynccontextmanager
    async def isolated_session() -> AsyncIterator[AsyncSession]:
        yield test_db
        await test_db.commit()
        commits.append("committed")

    monkeypatch.setattr(observation_module, "get_db_session", isolated_session)
    summary = await project_owned_group_member_observations(
        {
            "date": 1_789_117_200,
            "from": {"id": 7001, "is_bot": False},
            "text": "content is never persisted",
        },
        group_asset_id=1,
        core_group_id=2,
        source_bot_account_id=9,
        update_id=106,
        update_kind="message",
    )

    assert commits == ["committed"]
    assert [result.status for result in summary.results] == ["inserted"]
    row = await test_db.scalar(select(OwnedGroupMemberObservation))
    assert row is not None
    assert set(row.__table__.c.keys()).isdisjoint({"text", "message", "content"})


@pytest.mark.asyncio
async def test_projector_database_failure_is_reported_and_never_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def broken_session() -> AsyncIterator[AsyncSession]:
        raise RuntimeError("secret database diagnostics")
        yield  # pragma: no cover

    metric_log = MagicMock()
    monkeypatch.setattr(observation_module, "get_db_session", broken_session)
    monkeypatch.setattr(observation_module, "logger", metric_log)

    summary = await project_owned_group_member_observations(
        {
            "date": 1_789_117_200,
            "from": {"id": 7001, "is_bot": False, "username": "never-log-me"},
            "text": "never log this body",
        },
        group_asset_id=1,
        core_group_id=2,
        source_bot_account_id=9,
        update_id=107,
        update_kind="message",
    )

    assert summary.failed is True
    assert summary.failure_reason == "database_error"
    serialized_calls = repr(metric_log.method_calls)
    assert "never-log-me" not in serialized_calls
    assert "never log this body" not in serialized_calls
    assert "secret database diagnostics" not in serialized_calls


@pytest.mark.asyncio
async def test_projector_retries_one_transient_database_failure(
    test_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = 0

    @asynccontextmanager
    async def transient_session() -> AsyncIterator[AsyncSession]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OperationalError(None, None, RuntimeError("temporary"))
        yield test_db
        await test_db.commit()

    monkeypatch.setattr(observation_module, "get_db_session", transient_session)

    summary = await project_owned_group_member_observations(
        {
            "date": 1_789_117_200,
            "from": {"id": 7001, "is_bot": False},
            "text": "accepted on retry",
        },
        group_asset_id=1,
        core_group_id=2,
        source_bot_account_id=9,
        update_id=108,
        update_kind="message",
    )

    assert attempts == 2
    assert summary.failed is False
    assert [result.status for result in summary.results] == ["inserted"]


def test_model_and_migrations_match_the_stage_five_contract() -> None:
    table = OwnedGroupMemberObservation.__table__
    assert table.c.is_bot.default is None
    assert table.c.is_bot.server_default is None
    assert table.c.first_observed_at.type.timezone is True
    assert table.c.last_observed_at.type.timezone is True
    assert {index.name for index in table.indexes} == {
        "idx_owned_group_member_observations_presence",
        "idx_owned_group_member_observations_last_observed",
        "idx_owned_group_member_observations_telegram_user",
        "idx_owned_group_member_observations_source_bot",
    }

    repository_root = Path(__file__).resolve().parents[4]
    sql_migration = (
        repository_root / "migrations" / "052_add_owned_group_member_observations.sql"
    ).read_text(encoding="utf-8")
    alembic_migration = (
        repository_root
        / "migrations"
        / "versions"
        / "038_add_owned_group_member_observations.py"
    ).read_text(encoding="utf-8")
    assert "TIMESTAMP WITH TIME ZONE" in sql_migration
    assert "UPDATE owned_group_member_observations" not in sql_migration
    assert 'revision = "038_member_observations"' in alembic_migration
    assert 'down_revision = "037_message_persona_snapshot"' in alembic_migration
