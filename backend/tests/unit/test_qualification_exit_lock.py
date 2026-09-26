"""Offline PostgreSQL session-lock semantics around the full exit workflow."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.modules.acquisition import qualification_actions as actions


class FakePostgres:
    def __init__(self):
        self.owner = None
        self.connections = []
        self.lock_error = False
        self.unlock_error = False
        self.commit_error = False


class Connection:
    def __init__(self, server):
        self.server = server
        self.queries = []
        self.commits = 0
        self.invalidated = False
        self.closed = False

    async def scalar(self, statement, params):
        sql = str(statement)
        assert list(params.values()) == [20260922104118]
        assert "xact" not in sql
        self.queries.append(sql)
        if "pg_try_advisory_lock(" in sql:
            if self.server.owner is not None:
                return False
            self.server.owner = self
            if self.server.lock_error:
                raise ConnectionError("acquire response lost")
            return True
        assert "pg_advisory_unlock(" in sql
        if self.server.unlock_error:
            raise ConnectionError("unlock response lost")
        owned = self.server.owner is self
        if owned:
            self.server.owner = None
        return owned

    async def commit(self):
        assert self.server.owner is self
        self.commits += 1
        if self.server.commit_error:
            raise ConnectionError("acquire commit failed")
        # A session-level lock intentionally survives transaction commits.
        assert self.server.owner is self

    async def rollback(self):
        pass

    async def invalidate(self):
        self.invalidated = True
        if self.server.owner is self:
            self.server.owner = None

    async def close(self):
        self.closed = True
        # Returning a healthy connection to the pool must not be treated as
        # dropping the underlying PostgreSQL session/advisory lock.


class Engine:
    def __init__(self, server, dialect="postgresql"):
        self.server = server
        self.dialect = SimpleNamespace(name=dialect)

    async def connect(self):
        connection = Connection(self.server)
        self.server.connections.append(connection)
        return connection


def database(engine):
    return SimpleNamespace(bind=engine, get_bind=lambda: engine, commit=AsyncMock())


@pytest.fixture
def pg(monkeypatch):
    from app.modules.acquisition import adaptive_frequency
    # No adaptive exit is pending in this legacy reconciliation/lock fixture.
    monkeypatch.setattr(adaptive_frequency, "run_frequency_exits", AsyncMock(return_value={"processed": 0}))
    server = FakePostgres()
    engine = Engine(server)
    monkeypatch.setattr(actions, "get_settings", lambda: SimpleNamespace(APP_ENV="production"))
    monkeypatch.setattr(actions, "policy", AsyncMock(return_value={
        "enabled": True, "execute_exits": True, "account_ids": [2],
    }))
    monkeypatch.setattr(actions, "reconcile_stale_exit_memberships", AsyncMock(return_value={"checked": 0}))
    return server, engine


@pytest.mark.asyncio
async def test_two_runners_only_one_enters_body_even_after_commits(pg, monkeypatch):
    server, engine = pg
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    first_db = database(engine)

    async def body(actor, *, limit):
        calls.append(actor.db)
        assert server.owner is not None
        await actor.db.commit()
        await actor.db.commit()
        assert server.owner is not None
        entered.set()
        await release.wait()
        return {"processed": 1, "limit": limit}

    monkeypatch.setattr(actions, "_run_exits_serialized", body)
    first = asyncio.create_task(actions.run_exits(SimpleNamespace(db=first_db), limit=3))
    await asyncio.wait_for(entered.wait(), 1)
    try:
        other = await actions.run_exits(SimpleNamespace(db=database(engine)))
        assert other == {"processed": 0, "reason": "qualification_exit_busy"}
        assert calls == [first_db]
        assert first_db.commit.await_count == 2
        assert server.connections[0].commits == 1
        assert server.owner is server.connections[0]
    finally:
        release.set()
        result = await first
    assert result == {"processed": 1, "limit": 3, "membership_reconciliation": {"checked": 0}}
    assert server.owner is None
    assert all(connection.closed for connection in server.connections)
    assert sum("pg_advisory_unlock(" in query for connection in server.connections
               for query in connection.queries) == 1


@pytest.mark.asyncio
async def test_body_exception_releases_lock_and_propagates(pg, monkeypatch):
    server, engine = pg
    body = AsyncMock(side_effect=ValueError("assessment failed"))
    monkeypatch.setattr(actions, "_run_exits_serialized", body)
    with pytest.raises(ValueError, match="assessment failed"):
        await actions.run_exits(SimpleNamespace(db=database(engine)))
    assert server.owner is None
    assert server.connections[0].closed
    body.side_effect = None
    body.return_value = {"processed": 0}
    assert await actions.run_exits(SimpleNamespace(db=database(engine))) == {"processed": 0, "membership_reconciliation": {"checked": 0}}


@pytest.mark.asyncio
async def test_cancelled_body_releases_session_lock(pg, monkeypatch):
    server, engine = pg
    entered = asyncio.Event()

    async def body(actor, *, limit):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(actions, "_run_exits_serialized", body)
    task = asyncio.create_task(actions.run_exits(SimpleNamespace(db=database(engine))))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert server.owner is None
    assert server.connections[0].closed
    async with actions.exit_serialization_lock(database(engine)) as reason:
        assert reason is None


@pytest.mark.asyncio
async def test_unknown_lock_acquisition_fails_closed_and_drops_connection(pg, monkeypatch):
    server, engine = pg
    server.lock_error = True
    body = AsyncMock()
    monkeypatch.setattr(actions, "_run_exits_serialized", body)
    result = await actions.run_exits(SimpleNamespace(db=database(engine)))
    assert result == {"processed": 0, "reason": "qualification_exit_lock_unavailable"}
    body.assert_not_awaited()
    assert server.connections[0].invalidated
    assert server.connections[0].closed
    assert server.owner is None


@pytest.mark.asyncio
async def test_unlock_failure_invalidates_instead_of_pooling_held_lock(pg, monkeypatch):
    server, engine = pg
    server.unlock_error = True
    monkeypatch.setattr(actions, "_run_exits_serialized", AsyncMock(return_value={"processed": 1}))
    with pytest.raises(ConnectionError, match="unlock response lost"):
        await actions.run_exits(SimpleNamespace(db=database(engine)))
    assert server.connections[0].invalidated
    assert server.connections[0].closed
    assert server.owner is None


@pytest.mark.asyncio
@pytest.mark.parametrize("dialect", ["sqlite", "mysql"])
async def test_non_postgres_production_never_runs_without_distributed_lock(pg, monkeypatch, dialect):
    server, _ = pg
    body = AsyncMock()
    monkeypatch.setattr(actions, "_run_exits_serialized", body)
    result = await actions.run_exits(SimpleNamespace(db=database(Engine(server, dialect))))
    assert result == {"processed": 0, "reason": "qualification_exit_lock_unsupported"}
    body.assert_not_awaited()
    assert not server.connections


@pytest.mark.asyncio
async def test_paused_exits_do_not_acquire_a_lock(pg, monkeypatch):
    server, engine = pg
    monkeypatch.setattr(actions, "policy", AsyncMock(return_value={"enabled": True, "execute_exits": False}))
    body = AsyncMock()
    monkeypatch.setattr(actions, "_run_exits_serialized", body)
    assert await actions.run_exits(SimpleNamespace(db=database(engine))) == {"processed": 0, "reason": "exits_paused"}
    body.assert_not_awaited()
    assert not server.connections


@pytest.mark.asyncio
async def test_sqlite_test_lock_serializes_same_engine(pg, monkeypatch):
    server, _ = pg
    engine = Engine(server, "sqlite")
    monkeypatch.setattr(actions, "get_settings", lambda: SimpleNamespace(APP_ENV="test"))
    async with actions.exit_serialization_lock(database(engine)) as first:
        assert first is None
        async with actions.exit_serialization_lock(database(engine)) as second:
            assert second == "qualification_exit_busy"
    async with actions.exit_serialization_lock(database(engine)) as third:
        assert third is None
    assert not server.connections


@pytest.mark.asyncio
async def test_acquisition_commit_failure_invalidates_and_never_enters_body(pg, monkeypatch):
    server, engine = pg
    server.commit_error = True
    body = AsyncMock()
    monkeypatch.setattr(actions, "_run_exits_serialized", body)
    result = await actions.run_exits(SimpleNamespace(db=database(engine)))
    assert result == {"processed": 0, "reason": "qualification_exit_lock_unavailable"}
    body.assert_not_awaited()
    assert server.connections[0].commits == 1
    assert server.connections[0].invalidated
    assert server.connections[0].closed
    assert server.owner is None
