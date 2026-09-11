import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.settings import _public_settings
from app.core.automation_settings import (
    get_app_runtime_settings,
    patch_app_runtime_settings,
    save_app_runtime_settings,
)
from app.core.config import settings
from app.core.security import get_current_user
from app.main import app


def test_public_xboard_settings_always_use_environment_source():
    public = _public_settings(
        {
            "xboard": {
                "enabled": False,
                "apiKey": "legacy-value",
            }
        }
    )

    assert public["xboard"] == {
        "enabled": settings.VANGUARD_INTEGRATION_ENABLED,
        "callbackEnabled": settings.VANGUARD_CALLBACK_ENABLED,
        "protocol": "hmac",
        "source": "environment",
    }


@pytest.mark.asyncio
async def test_unrelated_runtime_patch_preserves_emergency_disable(test_db) -> None:
    await save_app_runtime_settings(
        test_db,
        {
            "ownedGroupMessaging": {"enabled": True, "dryRun": True},
            "aiReply": {"enabled": False},
        },
    )

    await patch_app_runtime_settings(
        test_db,
        {"ownedGroupMessaging": {"enabled": False}},
    )
    await patch_app_runtime_settings(test_db, {"aiReply": {"enabled": True}})

    current = await get_app_runtime_settings(test_db)
    assert current["ownedGroupMessaging"]["enabled"] is False
    assert current["aiReply"]["enabled"] is True


@pytest.mark.asyncio
async def test_postgres_runtime_patch_locks_before_read_merge_write() -> None:
    setting = SimpleNamespace(
        value=json.dumps(
            {
                "ownedGroupMessaging": {"enabled": False, "dryRun": True},
                "aiReply": {"enabled": False},
            }
        )
    )
    locked_result = SimpleNamespace(scalar_one_or_none=lambda: setting)
    db = AsyncMock()
    db.bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    db.add = MagicMock()
    db.execute.side_effect = [SimpleNamespace(), locked_result]

    saved = await patch_app_runtime_settings(db, {"aiReply": {"enabled": True}})

    calls = db.execute.await_args_list
    assert "pg_advisory_xact_lock" in str(calls[0].args[0])
    locked_select = calls[1].args[0]
    assert locked_select._for_update_arg is not None
    assert locked_select.get_execution_options()["populate_existing"] is True
    assert saved["ownedGroupMessaging"]["enabled"] is False
    assert saved["aiReply"]["enabled"] is True
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_admin_cannot_change_runtime_safety_switches(client):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 7002,
        "username": "settings-viewer",
        "role": "viewer",
    }

    response = await client.put(
        "/api/settings",
        json={
            "ownedGroupMessaging": {"enabled": True, "dryRun": False},
            "groupAiInteraction": {"enabled": True},
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Admin access required"


@pytest.mark.asyncio
async def test_non_admin_cannot_disable_account_risk_guard(client):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 7002,
        "username": "settings-viewer",
        "role": "viewer",
    }

    response = await client.put(
        "/api/automation/account-risk-guard",
        json={"enabled": False},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Admin access required"
