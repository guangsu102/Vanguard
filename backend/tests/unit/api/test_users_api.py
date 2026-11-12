"""Unit tests for the users API router (pagination, detail, filters)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.users import router
from app.core.database import get_db
from app.core.security import get_current_user
from app.core.user.models import User, UserState


@asynccontextmanager
async def _client(test_db):
    app = FastAPI()
    app.include_router(router, prefix="/api/users")

    async def database_override():
        yield test_db

    app.dependency_overrides[get_db] = database_override
    app.dependency_overrides[get_current_user] = lambda: {"id": 1, "username": "tester"}
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client


async def _seed_users(test_db, count: int = 25) -> list[User]:
    """Seed users with telegram_id 1000+i and staggered created_at."""
    users = []
    base_time = datetime(2026, 1, 1, 12, 0, 0)
    for i in range(count):
        user = User(
            telegram_id=1000 + i,
            username=f"user_{i}",
            state=UserState.NEW if i % 2 == 0 else UserState.ACTIVE,
            warning_count=0,
            created_at=base_time + timedelta(days=i),
            updated_at=base_time + timedelta(days=i),
        )
        test_db.add(user)
        users.append(user)
    await test_db.commit()
    for user in users:
        await test_db.refresh(user)
    return users


async def test_list_users_page_pagination(test_db):
    """Page 2 must return different rows than page 1 (issue: pagination ignored)."""
    users = await _seed_users(test_db, 25)
    async with _client(test_db) as client:
        page1 = await client.get("/api/users", params={"page": 1, "pageSize": 20})
        assert page1.status_code == 200
        body1 = page1.json()
        assert body1["total"] == 25
        assert len(body1["data"]) == 20
        ids1 = [row["id"] for row in body1["data"]]

        page2 = await client.get("/api/users", params={"page": 2, "pageSize": 20})
        assert page2.status_code == 200
        body2 = page2.json()
        assert len(body2["data"]) == 5
        ids2 = [row["id"] for row in body2["data"]]

        assert not set(ids1) & set(ids2), "page 2 must not repeat page 1 rows"
        assert max(ids2) < min(ids1), "descending order must hold across pages"
        assert body2["page"] == 2
        assert body2["page_size"] == 20

    # Sanity: seeded max id must be on page 1
    assert users[-1].id in ids1


async def test_list_users_cursor_still_works(test_db):
    """Legacy cursor pagination must keep working."""
    await _seed_users(test_db, 25)
    async with _client(test_db) as client:
        first = await client.get("/api/users", params={"limit": 10})
        assert first.status_code == 200
        body = first.json()
        assert len(body["data"]) == 10
        assert body["has_more"] is True
        assert body["next_cursor"] is not None

        second = await client.get(
            "/api/users", params={"limit": 10, "cursor": body["next_cursor"]}
        )
        assert second.status_code == 200
        ids_second = {row["id"] for row in second.json()["data"]}
        ids_first = {row["id"] for row in body["data"]}
        assert not ids_first & ids_second


async def test_get_user_detail(test_db):
    """Detail endpoint returns the requested user (issue: detail 404/blank)."""
    users = await _seed_users(test_db, 3)
    target = users[1]
    async with _client(test_db) as client:
        resp = await client.get(f"/api/users/{target.id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == target.id
        assert body["telegram_id"] == target.telegram_id
        assert body["created_at"]

        missing = await client.get("/api/users/99999")
        assert missing.status_code == 404


async def test_list_users_state_and_time_filters(test_db):
    """state_filter and registered_from/to filters must restrict results."""
    await _seed_users(test_db, 10)
    async with _client(test_db) as client:
        active = await client.get(
            "/api/users", params={"page": 1, "page_size": 50, "state_filter": "active"}
        )
        assert active.status_code == 200
        rows = active.json()["data"]
        assert rows, "expected seeded active users"
        assert all(row["state"] == "active" for row in rows)

        ranged = await client.get(
            "/api/users",
            params={
                "page": 1,
                "page_size": 50,
                "registered_from": "2026-01-03",
                "registered_to": "2026-01-05",
            },
        )
        assert ranged.status_code == 200
        ranged_rows = ranged.json()["data"]
        assert ranged_rows, "expected users inside the registration window"
        for row in ranged_rows:
            assert "2026-01-03" <= row["created_at"] <= "2026-01-05T23:59:59"

        bad_state = await client.get("/api/users", params={"state_filter": "bogus"})
        assert bad_state.status_code == 400

        bad_date = await client.get("/api/users", params={"registered_from": "not-a-date"})
        assert bad_date.status_code == 400


async def test_search_by_telegram_id(test_db):
    """Search must match telegram_id as string."""
    await _seed_users(test_db, 5)
    async with _client(test_db) as client:
        resp = await client.get("/api/users", params={"search": "1002"})
        assert resp.status_code == 200
        rows = resp.json()["data"]
        assert len(rows) == 1
        assert rows[0]["telegram_id"] == 1002
