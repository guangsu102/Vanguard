from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException, status
from fastapi.routing import APIRoute

from app.celery import celery_app
from app.core.security import require_admin
from app.main import app
from app.modules.managed_bot_provision import tasks as task_module

api_module = importlib.import_module("app.api.managed_bot_provisions")


def _route(path: str, method: str) -> APIRoute:
    return next(
        route
        for route in api_module.router.routes
        if isinstance(route, APIRoute)
        and route.path == path
        and method in route.methods
    )


def test_managed_bot_provision_routes_are_admin_only_and_match_frontend_contract():
    capability = _route(
        "/managed-provisions/capability",
        "GET",
    )
    create = _route("/managed-provisions", "POST")
    detail = _route(
        "/managed-provisions/{operation_id:int}",
        "GET",
    )

    assert any(
        getattr(route, "original_router", None) is api_module.router
        and getattr(getattr(route, "include_context", None), "prefix", None)
        == "/api/guardian-bots"
        for route in app.routes
    )
    assert create.status_code == status.HTTP_202_ACCEPTED
    for route in (capability, create, detail):
        assert require_admin in {
            dependency.call for dependency in route.dependant.dependencies
        }


@pytest.mark.asyncio
async def test_create_endpoint_passes_safe_fields_and_dispatches_best_effort(
    monkeypatch,
):
    captured: dict[str, Any] = {}
    operation = SimpleNamespace(id=17)

    async def fake_create(_db, **kwargs):
        captured.update(kwargs)
        return operation, True

    monkeypatch.setattr(
        api_module,
        "create_managed_bot_provision",
        fake_create,
    )
    monkeypatch.setattr(
        api_module,
        "serialize_provision",
        lambda row: {"id": row.id, "status": "queued", "current_step": "preflight"},
    )
    monkeypatch.setattr(
        api_module,
        "_dispatch_tick_best_effort",
        lambda: captured.update(dispatched=True),
    )

    response = await api_module.create_managed_bot_provision_operation(
        api_module.ManagedBotProvisionCreate(
            owner_account_id=3,
            manager_bot_profile_id=5,
            display_name="Managed Guardian",
            username="managed_guardian_bot",
        ),
        idempotency_key="managed-provision-request-17",
        current_user={"id": 9, "role": "admin"},
        db=object(),
    )

    assert captured == {
        "owner_account_id": 3,
        "manager_bot_profile_id": 5,
        "display_name": "Managed Guardian",
        "username": "managed_guardian_bot",
        "idempotency_key": "managed-provision-request-17",
        "created_by_id": 9,
        "dispatched": True,
    }
    assert response["data"] == {
        "id": 17,
        "status": "queued",
        "current_step": "preflight",
    }
    assert "token" not in repr(response).lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("service_error", "expected_status", "expected_code"),
    [
        ("IDEMPOTENCY_KEY_CONFLICT", 409, "IDEMPOTENCY_KEY_CONFLICT"),
        ("USERNAME_INVALID", 422, "USERNAME_INVALID"),
        ("secret-token-value", 422, "PROVISION_REQUEST_INVALID"),
    ],
)
async def test_create_endpoint_maps_value_errors_without_echoing_details(
    monkeypatch,
    service_error,
    expected_status,
    expected_code,
):
    async def fake_create(_db, **_kwargs):
        raise ValueError(service_error)

    monkeypatch.setattr(
        api_module,
        "create_managed_bot_provision",
        fake_create,
    )

    with pytest.raises(HTTPException) as raised:
        await api_module.create_managed_bot_provision_operation(
            api_module.ManagedBotProvisionCreate(
                owner_account_id=3,
                manager_bot_profile_id=5,
                display_name="Managed Guardian",
                username="managed_guardian_bot",
            ),
            idempotency_key="managed-provision-request-17",
            current_user={"id": 9, "role": "admin"},
            db=object(),
        )

    assert raised.value.status_code == expected_status
    assert raised.value.detail["code"] == expected_code
    assert "secret-token-value" not in repr(raised.value.detail)


@pytest.mark.asyncio
async def test_get_missing_operation_returns_safe_404():
    class MissingSession:
        async def get(self, _model, _operation_id):
            return None

    with pytest.raises(HTTPException) as raised:
        await api_module.get_managed_bot_provision_operation(
            404,
            _current_user={"id": 9, "role": "admin"},
            db=MissingSession(),
        )

    assert raised.value.status_code == status.HTTP_404_NOT_FOUND
    assert raised.value.detail == {
        "code": "PROVISION_NOT_FOUND",
        "message": "Managed Bot 创建任务不存在",
    }


def test_celery_include_schedule_and_route_use_owned_group_queue():
    task_name = (
        "app.modules.managed_bot_provision.tasks.managed_bot_provision_tick"
    )
    schedule = celery_app.conf.beat_schedule[
        "managed-bot-provision-worker-every-15s"
    ]

    assert "app.modules.managed_bot_provision.tasks" in celery_app.conf.include
    assert schedule == {
        "task": task_name,
        "schedule": 15.0,
        "kwargs": {"limit": 5, "stale_after_seconds": 300},
        "options": {"queue": "owned_group"},
    }
    assert celery_app.conf.task_routes[task_name] == {"queue": "owned_group"}
    assert task_module.managed_bot_provision_tick.name == task_name


def test_celery_task_clamps_tick_inputs(monkeypatch):
    monkeypatch.setattr(
        task_module,
        "_run_tick_with_db",
        lambda **kwargs: kwargs,
    )
    monkeypatch.setattr(
        task_module,
        "_run_async",
        lambda value: value,
    )

    assert task_module.managed_bot_provision_tick.run(
        limit=999,
        stale_after_seconds=1,
    ) == {"limit": 20, "stale_after_seconds": 90}
