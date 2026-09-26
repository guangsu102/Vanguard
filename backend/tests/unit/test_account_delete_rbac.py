import pytest

from app.core.security import get_current_user
from app.main import app


@pytest.mark.asyncio
async def test_non_admin_cannot_delete_accounts(client):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 22,
        "username": "viewer",
        "role": "viewer",
    }
    try:
        single = await client.delete("/api/accounts/99999999")
        batch = await client.post("/api/accounts/batch-delete", json=[99999999])
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert single.status_code == 403
    assert single.json()["detail"] == "Admin access required"
    assert batch.status_code == 403
    assert batch.json()["detail"] == "Admin access required"
