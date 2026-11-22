"""A read checkpoint must not commit flushed, Core or driver writes."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database_transaction_boundary as boundary
from app.core.database_transaction_boundary import (
    TransactionBoundaryBlocked,
    release_read_transaction,
)
from app.core.settings_models import SystemSetting


async def baseline(db):
    row = SystemSetting(key="boundary.source", value="original")
    db.add(row)
    await db.commit()
    return row


@pytest.mark.asyncio
async def test_proven_select_returns_the_connection(test_db):
    await test_db.scalar(select(SystemSetting).where(SystemSetting.key == "missing"))
    assert test_db.in_transaction()
    assert await release_read_transaction(test_db)
    assert not test_db.in_transaction()


@pytest.mark.asyncio
@pytest.mark.parametrize("write_kind", ["new", "dirty", "flushed", "deleted", "core", "text", "connection", "driver"])
async def test_pending_writes_are_neither_committed_nor_rolled_back(test_db, write_kind, monkeypatch):
    row = await baseline(test_db)
    if write_kind == "new":
        test_db.add(SystemSetting(key="boundary.pending", value="pending"))
    elif write_kind in {"dirty", "flushed"}:
        row.value = "pending"
        if write_kind == "flushed":
            await test_db.flush()
    elif write_kind == "deleted":
        await test_db.delete(row)
        await test_db.flush()
    elif write_kind == "core":
        await test_db.execute(update(SystemSetting).where(SystemSetting.key == row.key).values(value="pending"))
    elif write_kind == "text":
        await test_db.execute(text("UPDATE system_setting SET value='pending' WHERE key='boundary.source'"))
    else:
        connection = await test_db.connection()
        if write_kind == "connection":
            await connection.execute(update(SystemSetting).where(SystemSetting.key == row.key).values(value="pending"))
        else:
            await connection.exec_driver_sql("UPDATE system_setting SET value='pending' WHERE key='boundary.source'")
    commit, rollback = AsyncMock(wraps=test_db.commit), AsyncMock(wraps=test_db.rollback)
    monkeypatch.setattr(test_db, "commit", commit)
    monkeypatch.setattr(test_db, "rollback", rollback)
    with pytest.raises(TransactionBoundaryBlocked):
        await release_read_transaction(test_db, allow_untracked_read_only=True)
    commit.assert_not_awaited()
    rollback.assert_not_awaited()
    await test_db.rollback()
    assert (await test_db.get(SystemSetting, "boundary.source")).value == "original"


@pytest.mark.asyncio
async def test_rollback_clears_write_tracking_for_the_next_read(test_db):
    row = await baseline(test_db)
    await test_db.execute(update(SystemSetting).where(SystemSetting.key == row.key).values(value="pending"))
    await test_db.rollback()
    await test_db.scalar(select(SystemSetting.value).where(SystemSetting.key == "boundary.source"))
    assert await release_read_transaction(test_db)


@pytest.mark.asyncio
async def test_active_savepoint_is_not_a_release_boundary(test_db):
    await baseline(test_db)
    nested = await test_db.begin_nested()
    with pytest.raises(TransactionBoundaryBlocked, match="nested"):
        await release_read_transaction(test_db)
    assert nested.is_active
    await nested.rollback()
    assert await release_read_transaction(test_db)


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_savepoint", [False, True])
async def test_savepoint_preserves_only_unrolled_back_writes(test_db, commit_savepoint):
    row = await baseline(test_db)
    nested = await test_db.begin_nested()
    await test_db.execute(update(SystemSetting).where(SystemSetting.key == row.key).values(value="pending"))
    if commit_savepoint:
        await nested.commit()
        with pytest.raises(TransactionBoundaryBlocked):
            await release_read_transaction(test_db)
    else:
        await nested.rollback()
        assert await release_read_transaction(test_db)


@pytest.mark.asyncio
async def test_untracked_transaction_requires_explicit_read_ownership(test_db):
    await test_db.scalar(select(SystemSetting.value))
    boundary._transactions.pop(test_db.sync_session.get_transaction())
    with pytest.raises(TransactionBoundaryBlocked, match="untracked"):
        await release_read_transaction(test_db)
    assert await release_read_transaction(test_db, allow_untracked_read_only=True)


def test_read_only_controls_are_not_classified_as_writes():
    assert boundary._read_statement(text("SET TRANSACTION READ ONLY"))
    assert boundary._read_statement(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
    assert boundary._read_statement(text("SET LOCAL transaction_read_only = on"))
    assert not boundary._read_statement(text("WITH changed AS (UPDATE system_setting SET value='x') SELECT 1"))
    assert not boundary._read_statement(text("SET TRANSACTION READ ONLY; DELETE FROM system_setting"))


def test_a_select_with_a_writable_cte_is_not_a_read_snapshot():
    change = update(SystemSetting).values(value="changed").returning(SystemSetting.key).cte("changed")
    assert not boundary._read_statement(select(SystemSetting.key).add_cte(change))


@pytest.mark.asyncio
async def test_reused_connection_tracks_the_current_session_owner(test_db):
    await baseline(test_db)
    async with test_db.bind.connect() as connection:
        async with AsyncSession(connection) as first:
            await first.scalar(select(SystemSetting.key))
            assert await release_read_transaction(first)
        async with AsyncSession(connection) as second:
            await second.scalar(select(SystemSetting.key))
            await connection.execute(update(SystemSetting).values(value="new session write"))
            with pytest.raises(TransactionBoundaryBlocked, match="core_or_unknown"):
                await release_read_transaction(second)


@pytest.mark.asyncio
@pytest.mark.parametrize("flushed", [False, True])
async def test_scoped_object_allowlist_requires_the_specific_owned_fields(test_db, flushed):
    row = await baseline(test_db)
    row.value = "caller-owned change"
    if flushed:
        await test_db.flush()
    with pytest.raises(TransactionBoundaryBlocked, match="unowned_scope_field"):
        await release_read_transaction(
            test_db, allowed_objects=(row,), allowed_object_fields={row: frozenset({"description"})},
        )
    assert await release_read_transaction(
        test_db, allowed_objects=(row,), allowed_object_fields={row: frozenset({"value", "updated_at"})},
    )
